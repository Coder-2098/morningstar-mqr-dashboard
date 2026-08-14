from __future__ import annotations

import argparse
import sys

from mstar_mqr.common import DailyCellLimitExceeded, MorningstarAuthError, log
from mstar_mqr.datapoints import DEFAULT_TYPE_DATAPOINT_CANDIDATES, DEFAULT_VALUE_DATAPOINT
from mstar_mqr.pipeline import run_pipeline
from mstar_mqr.token_manager import ensure_md_auth_token, mark_daily_limit_exceeded


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Optimized Morningstar MQR launch-date pipeline for one domicile."
    )

    parser.add_argument("--domicile", required=True, help="Domicile country, e.g. France or South Korea")
    parser.add_argument("--universes", nargs="+", default=None, help="Morningstar universes. Default: FO FE. Use this only to intentionally add/override universes.")

    parser.add_argument(
        "--investment-list-file",
        default=None,
        help=(
            "Optional exact Morningstar saved list/export (.xlsx/.xls/.csv/.tsv/.txt). "
            "When provided, the pipeline uses those IDs instead of discovering a broad domicile universe. "
            "This is generic and works for AFS, Restricted, FCPE, or any other list name."
        ),
    )
    parser.add_argument(
        "--investment-list-sheet",
        dest="investment_list_sheets",
        nargs="+",
        default=None,
        help="Optional sheet name(s) from the investment-list workbook. Default: all sheets.",
    )
    parser.add_argument(
        "--investment-list-label",
        default=None,
        help="Optional generic label for the exact list. Default: input file name without extension.",
    )
    metadata_group = parser.add_mutually_exclusive_group()
    metadata_group.add_argument(
        "--enrich-list-metadata",
        dest="enrich_list_metadata",
        action="store_true",
        help="Optional: pull extra Morningstar name/domicile metadata for an exact list. Off by default because it consumes substantial cell quota.",
    )
    metadata_group.add_argument(
        "--no-enrich-list-metadata",
        dest="enrich_list_metadata",
        action="store_false",
        help="Skip the optional Morningstar metadata pull for an exact input list (default).",
    )
    parser.set_defaults(enrich_list_metadata=False)
    parser.add_argument(
        "--full-history",
        action="store_true",
        help=(
            "Continue through the full date range after finding the country launch month. "
            "Creates per-fund first quantitative dates, long history, and a formatted Excel workbook for country or exact-list runs."
        ),
    )
    parser.add_argument(
        "--no-comparable-workbook",
        action="store_true",
        help="With --full-history, skip the formatted Excel workbook export.",
    )
    parser.add_argument(
        "--output-template",
        choices=["professor", "clean", "custom"],
        default="professor",
        help=(
            "Excel output layout for any full-history run. "
            "Default: professor, which reproduces the four-row-per-fund Batch sheet layout. "
            "Use clean for a normal table or custom with --output-template-file."
        ),
    )
    parser.add_argument(
        "--output-template-file",
        default=None,
        help=(
            "Optional .xlsx workbook used when --output-template custom. Existing sheets are preserved; "
            "generated summary, first-date, and Batch sheets are added. A Batch_Template sheet is used as the style seed when present."
        ),
    )

    parser.add_argument(
        "--start-date",
        default="2017-01-01",
        help="Start date for scan. Default: 2017-01-01 per research brief. Use YYYY-MM-DD or 'auto' to override.",
    )
    parser.add_argument("--end-date", default="2024-12-31", help="End date for scan. Default: 2024-12-31")
    parser.add_argument(
        "--fallback-start-date",
        default="2017-01-01",
        help="Used only if --start-date auto cannot read Morningstar settings. Default: 2017-01-01.",
    )

    parser.add_argument("--max-datasets", type=int, default=60, help="Metadata datasets to scan for legacy quant fields. Default: 60")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers for metadata scanning. Default: 4")
    parser.add_argument(
        "--scan-batch-size",
        type=int,
        default=5000,
        help="Number of funds per one-month scan request. Default: 5000.",
    )

    parser.add_argument(
        "--medalist-value-id",
        default=DEFAULT_VALUE_DATAPOINT,
        help="Medalist rating value datapoint. Default: MMR00.",
    )
    parser.add_argument(
        "--type-datapoint-ids",
        nargs="+",
        default=DEFAULT_TYPE_DATAPOINT_CANDIDATES,
        help="Type/source datapoint candidates. Default: MMR08.",
    )
    legacy_group = parser.add_mutually_exclusive_group()
    legacy_group.add_argument(
        "--discover-legacy-quantitative",
        dest="include_legacy_quantitative",
        action="store_true",
        help=(
            "Advanced fallback: scan Morningstar metadata for older/pre-Medalist quantitative field IDs. "
            "Off by default because MMR00 and MMR08 are the known project fields."
        ),
    )
    legacy_group.add_argument(
        "--no-legacy-quantitative",
        dest="include_legacy_quantitative",
        action="store_false",
        help="Explicitly keep legacy/pre-Medalist field discovery off (default).",
    )
    parser.set_defaults(include_legacy_quantitative=False)

    parser.add_argument(
        "--include-human-scan",
        action="store_true",
        help="Optional: separately scan for earliest Human/Analyst rating month. Off by default because the main deliverable is MQR/Quantitative launch date.",
    )
    parser.add_argument(
        "--human-start-date",
        default="2008-01-01",
        help="Start date for optional human/analyst scan. Used only with --include-human-scan. Default: 2008-01-01.",
    )
    parser.add_argument(
        "--human-end-date",
        default="2024-12-31",
        help="End date for optional human/analyst scan. Used only with --include-human-scan. Default: 2024-12-31.",
    )

    parser.add_argument(
        "--no-reuse-existing",
        action="store_true",
        help="Force fresh pulls instead of reusing existing output files.",
    )
    parser.add_argument(
        "--no-token-prompt",
        action="store_true",
        help="Do not ask for token. Use existing MD_AUTH_TOKEN or clipboard only.",
    )
    parser.add_argument(
        "--no-browser-auth",
        action="store_true",
        help="Do not open/automate Analytics Lab for token refresh; use env/clipboard/manual only.",
    )
    parser.add_argument(
        "--no-auth-retry",
        action="store_true",
        help="Do not retry once with a fresh token if Morningstar rejects the first token.",
    )

    parser.add_argument(
        "--test-limit-funds",
        type=int,
        default=None,
        help="Test-only cap on number of funds. Useful when only a few cells remain.",
    )
    parser.add_argument(
        "--dedupe-column",
        default=None,
        help="Optional column in 02_investments.csv to dedupe share classes, e.g. masterportfolioid.",
    )
    parser.add_argument(
        "--no-dedupe",
        action="store_true",
        help="Disable automatic dedupe by master portfolio / portfolio columns.",
    )

    args = parser.parse_args()

    def _run_once() -> int:
        run_pipeline(
            domicile=args.domicile,
            universes=args.universes,
            start_date=args.start_date,
            end_date=args.end_date,
            fallback_start_date=args.fallback_start_date,
            max_datasets=args.max_datasets,
            workers=args.workers,
            scan_batch_size=args.scan_batch_size,
            medalist_value_id=args.medalist_value_id,
            type_datapoint_ids=args.type_datapoint_ids,
            include_legacy_quantitative=args.include_legacy_quantitative,
            include_human_scan=args.include_human_scan,
            human_start_date=args.human_start_date,
            human_end_date=args.human_end_date,
            reuse_existing=not args.no_reuse_existing,
            prompt_for_token=not args.no_token_prompt,
            test_limit_funds=args.test_limit_funds,
            dedupe_column=args.dedupe_column,
            disable_dedupe=args.no_dedupe,
            investment_list_file=args.investment_list_file,
            investment_list_sheets=args.investment_list_sheets,
            investment_list_label=args.investment_list_label,
            enrich_list_metadata=args.enrich_list_metadata,
            full_history=args.full_history,
            export_comparable_workbook=not args.no_comparable_workbook,
            output_template_mode=args.output_template,
            output_template_file=args.output_template_file,
        )
        return 0

    try:
        return _run_once()
    except DailyCellLimitExceeded:
        mark_daily_limit_exceeded()
        log("DAILY CELL LIMIT EXCEEDED")
        log("Progress has been saved. Re-run the same command after the 12AM UTC reset with a fresh Analytics Lab token.")
        return 2
    except MorningstarAuthError as exc:
        if args.no_auth_retry:
            log("AUTHENTICATION ERROR")
            log(str(exc))
            return 3
        log("AUTHENTICATION ERROR")
        log("Refreshing Analytics Lab token once and retrying...")
        try:
            ensure_md_auth_token(
                allow_prompt=not args.no_token_prompt,
                prefer_clipboard=True,
                force_refresh=True,
                use_browser=not args.no_browser_auth,
            )
            return _run_once()
        except MorningstarAuthError as second_exc:
            log("AUTHENTICATION ERROR")
            log("token expired or invalid even after refresh. Copy a fresh Analytics Lab token and rerun.")
            log(str(second_exc))
            return 3
        except DailyCellLimitExceeded:
            mark_daily_limit_exceeded()
            log("DAILY CELL LIMIT EXCEEDED")
            log("Progress has been saved. Re-run the same command after the 12AM UTC reset with a fresh Analytics Lab token.")
            return 2



if __name__ == "__main__":
    sys.exit(main())