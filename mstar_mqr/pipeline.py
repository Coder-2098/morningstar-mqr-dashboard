from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import pandas as pd

from .common import ensure_token, find_col, find_id_col, log, make_output_dir, save_df, unique_strings, update_state
from .datapoints import (
    DEFAULT_TYPE_DATAPOINT_CANDIDATES,
    DEFAULT_VALUE_DATAPOINT,
    datapoint_settings,
    discover_quantitative_datapoints,
    earliest_settings_start_date,
    legacy_quantitative_value_ids,
    type_datapoint_candidates,
)
from .investment_lists import (
    enrich_input_list_metadata,
    list_output_dir,
    load_investment_list,
    resolved_list_label,
)
from .investments import pull_investments_by_domicile
from .launch_scan import run_launch_scan


DEDUPE_COLUMN_CANDIDATES = [
    "masterportfolioid",
    "masterPortfolioId",
    "Master Portfolio ID",
    "MasterPortfolioId",
    "portfolioid",
    "PortfolioId",
    "Portfolio ID",
    "fundid",
    "FundId",
    "Fund ID",
]


def normalize_date_bound(value: str, is_end: bool = False) -> str:
    """Accept YYYY-MM or YYYY-MM-DD and return a complete date."""
    text = str(value).strip()
    if text.lower() == "auto":
        return "auto"
    if len(text) == 7 and text[4] == "-":
        period = pd.Period(text, freq="M")
        timestamp = period.end_time if is_end else period.start_time
        return timestamp.strftime("%Y-%m-%d")
    try:
        return pd.Timestamp(text).strftime("%Y-%m-%d")
    except Exception as exc:
        raise RuntimeError(
            f"Invalid date {value!r}. Use YYYY-MM or YYYY-MM-DD."
        ) from exc


def choose_investments_for_pull(
    investments: pd.DataFrame,
    output_dir: Path,
    dedupe_column: Optional[str] = None,
    disable_dedupe: bool = False,
    test_limit_funds: Optional[int] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    """Select the investment IDs sent to Morningstar and retain their metadata."""
    id_col = find_id_col(investments)
    selected = investments.copy()
    selected["_query_investment_id"] = selected[id_col].astype(str).str.strip()
    selected = selected[selected["_query_investment_id"] != ""].copy()

    dedupe_used = None
    if not disable_dedupe:
        if dedupe_column:
            if dedupe_column not in selected.columns:
                raise RuntimeError(
                    f"Requested --dedupe-column {dedupe_column!r}, but it is not in 02_investments.csv. "
                    f"Available columns: {list(selected.columns)}"
                )
            dedupe_used = dedupe_column
        else:
            dedupe_used = find_col(selected, DEDUPE_COLUMN_CANDIDATES)

        if dedupe_used:
            before = len(selected)
            key = selected[dedupe_used].astype(str).str.strip()
            key = key.mask(key.isin(["", "nan", "NaN", "None", "none"]), selected["_query_investment_id"])
            selected["_dedupe_key"] = key
            selected = selected.drop_duplicates(subset=["_dedupe_key"]).copy()
            log(f"Deduped using {dedupe_used}: {before:,} rows -> {len(selected):,} representatives")
        else:
            log("No master portfolio / portfolio dedupe column found. Using unique investment IDs.")

    selected = selected.drop_duplicates(subset=["_query_investment_id"]).copy()

    if test_limit_funds is not None:
        selected = selected.head(test_limit_funds).copy()
        log(f"TEST MODE: limited to first {len(selected)} investment IDs.")

    save_df(selected, output_dir / "02b_investments_used_for_pull.csv")
    investment_ids = unique_strings(selected["_query_investment_id"].tolist())
    if not investment_ids:
        raise RuntimeError("No investment IDs found after dedupe/test filtering.")

    update_state(output_dir, investment_ids_used=len(investment_ids), dedupe_column_used=dedupe_used)
    return selected, investment_ids


def resolve_start_date(
    requested_start_date: str,
    output_dir: Path,
    datapoint_ids_for_settings: List[str],
    fallback_start_date: str,
    reuse_existing: bool,
) -> str:
    """Use Morningstar settings only when the user explicitly requests auto."""
    if requested_start_date.lower() != "auto":
        return normalize_date_bound(requested_start_date, is_end=False)

    settings = datapoint_settings(datapoint_ids_for_settings, output_dir, reuse_existing=reuse_existing)
    resolved = earliest_settings_start_date(settings, fallback=fallback_start_date)
    resolved = normalize_date_bound(resolved, is_end=False)
    log(f"Auto start date resolved to {resolved}")
    update_state(output_dir, auto_start_date=resolved, fallback_start_date=fallback_start_date)
    return resolved


def run_pipeline(
    domicile: str,
    universes: Optional[List[str]] = None,
    start_date: str = "2017-01-01",
    end_date: Optional[str] = "2024-12-31",
    fallback_start_date: str = "2017-01-01",
    max_datasets: int = 60,
    workers: int = 4,
    scan_batch_size: int = 5000,
    medalist_value_id: str = DEFAULT_VALUE_DATAPOINT,
    type_datapoint_ids: Optional[List[str]] = None,
    include_legacy_quantitative: bool = False,
    include_human_scan: bool = False,
    human_start_date: str = "2008-01-01",
    human_end_date: Optional[str] = "2024-12-31",
    reuse_existing: bool = True,
    prompt_for_token: bool = True,
    test_limit_funds: Optional[int] = None,
    dedupe_column: Optional[str] = None,
    disable_dedupe: bool = False,
    investment_list_file: Optional[str] = None,
    investment_list_sheets: Optional[Sequence[str]] = None,
    investment_list_label: Optional[str] = None,
    enrich_list_metadata: bool = False,
    full_history: bool = False,
    export_comparable_workbook: bool = True,
    output_template_mode: str = "professor",
    output_template_file: Optional[str] = None,
) -> pd.DataFrame:
    """
    Main orchestration for one country or one exact saved investment list.

    Country mode discovers FO/FE investments by domicile. Exact-list mode accepts
    any CSV/Excel saved list containing Morningstar IDs and is generic across
    countries and Morningstar sub-universe labels such as AFS, Restricted, FCPE,
    or any future list name.
    """
    ensure_token(prompt_for_token=prompt_for_token)

    if universes is None:
        universes = ["FO", "FE"]
    if end_date is None:
        end_date = "2024-12-31"
    if human_end_date is None:
        human_end_date = "2024-12-31"
    if type_datapoint_ids is None:
        type_datapoint_ids = DEFAULT_TYPE_DATAPOINT_CANDIDATES.copy()

    normalized_end_date = normalize_date_bound(end_date, is_end=True)
    normalized_human_start = normalize_date_bound(human_start_date, is_end=False)
    normalized_human_end = normalize_date_bound(human_end_date, is_end=True)

    base_output_dir = make_output_dir(domicile)
    list_label = ""
    if investment_list_file:
        list_label = resolved_list_label(investment_list_file, investment_list_label)
        output_dir = list_output_dir(base_output_dir, investment_list_file, investment_list_label)
    else:
        output_dir = base_output_dir

    log("=" * 80)
    log("Starting optimized Morningstar MQR launch-date pipeline")
    log(f"Domicile: {domicile}")
    log(f"Universes: {universes}")
    if investment_list_file:
        log(f"Exact investment-list file: {Path(investment_list_file).expanduser()}")
        log(f"Generic list label: {list_label}")
        log(f"Input sheets: {list(investment_list_sheets) if investment_list_sheets else 'all sheets'}")
    log(f"Requested start date: {start_date}")
    log(f"End date: {normalized_end_date}")
    log(f"Output folder: {output_dir}")
    log(f"Medalist value datapoint: {medalist_value_id}")
    log(f"Type/source datapoint candidates: {type_datapoint_ids}")
    log(f"Include legacy quantitative discovery: {include_legacy_quantitative}")
    log(f"Full per-fund history mode: {full_history}")
    if full_history and export_comparable_workbook:
        log(f"Excel output template: {output_template_mode}")
        if output_template_file:
            log(f"Custom output template file: {Path(output_template_file).expanduser()}")
    log(f"Optional human/analyst scan: {include_human_scan}")
    if include_human_scan:
        log(f"Human/analyst scan date range: {normalized_human_start} to {normalized_human_end}")
    log(f"Scan batch size: {scan_batch_size}")
    log("=" * 80)

    if investment_list_file:
        investments = load_investment_list(
            file_path=investment_list_file,
            output_dir=output_dir,
            list_label=investment_list_label,
            sheet_names=investment_list_sheets,
            reuse_existing=reuse_existing,
        )
        if enrich_list_metadata:
            investments = enrich_input_list_metadata(
                investments=investments,
                output_dir=output_dir,
                batch_size=scan_batch_size,
                reuse_existing=reuse_existing,
            )
    else:
        investments = pull_investments_by_domicile(
            domicile=domicile,
            output_dir=output_dir,
            universes=universes,
            reuse_existing=reuse_existing,
        )

    selected_investments, investment_ids = choose_investments_for_pull(
        investments=investments,
        output_dir=output_dir,
        dedupe_column=dedupe_column,
        disable_dedupe=disable_dedupe,
        test_limit_funds=test_limit_funds,
    )

    legacy_ids: List[str] = []
    discovered_type_ids: List[str] = []
    if include_legacy_quantitative:
        candidates = discover_quantitative_datapoints(
            output_dir=output_dir,
            universes=universes,
            max_datasets=max_datasets,
            workers=workers,
            reuse_existing=reuse_existing,
        )
        legacy_ids = legacy_quantitative_value_ids(candidates)
        discovered_type_ids = type_datapoint_candidates(candidates)
        if legacy_ids:
            log(f"Legacy quantitative value datapoints discovered: {legacy_ids}")
        if discovered_type_ids:
            type_datapoint_ids = unique_strings(type_datapoint_ids + discovered_type_ids)

    settings_ids = unique_strings([medalist_value_id] + type_datapoint_ids + legacy_ids)
    resolved_start_date = resolve_start_date(
        requested_start_date=start_date,
        output_dir=output_dir,
        datapoint_ids_for_settings=settings_ids,
        fallback_start_date=fallback_start_date,
        reuse_existing=reuse_existing,
    )

    update_state(
        output_dir,
        status="running_launch_scan",
        domicile=domicile,
        universes=universes,
        input_mode="exact_investment_list" if investment_list_file else "domicile_search",
        investment_list_file=str(Path(investment_list_file).expanduser().resolve()) if investment_list_file else "",
        investment_list_label=list_label,
        investment_list_sheets=list(investment_list_sheets or []),
        medalist_value_id=medalist_value_id,
        type_datapoint_ids=type_datapoint_ids,
        legacy_quantitative_value_ids=legacy_ids,
        start_date=resolved_start_date,
        end_date=normalized_end_date,
        full_history=full_history,
        export_comparable_workbook=bool(export_comparable_workbook),
        output_template_mode=output_template_mode,
        output_template_file=(
            str(Path(output_template_file).expanduser().resolve())
            if output_template_file
            else ""
        ),
        human_scan_enabled=include_human_scan,
        human_start_date=normalized_human_start if include_human_scan else "",
        human_end_date=normalized_human_end if include_human_scan else "",
    )

    result = run_launch_scan(
        domicile=domicile,
        investment_ids=investment_ids,
        investment_metadata=selected_investments,
        output_dir=output_dir,
        start_date=resolved_start_date,
        end_date=normalized_end_date,
        medalist_value_id=medalist_value_id,
        type_datapoint_ids=type_datapoint_ids,
        legacy_value_ids=legacy_ids,
        scan_batch_size=scan_batch_size,
        reuse_existing=reuse_existing,
        collect_human=include_human_scan,
        human_start_date=normalized_human_start,
        human_end_date=normalized_human_end,
        full_history=full_history,
        export_comparable_workbook=bool(export_comparable_workbook),
        list_label=list_label,
        output_template_mode=output_template_mode,
        output_template_file=output_template_file,
    )

    update_state(output_dir, status="complete")
    log("=" * 80)
    log("Pipeline complete.")
    log(f"Final result: {output_dir / 'FINAL_MQR_LAUNCH_RESULT.csv'}")
    if full_history or export_comparable_workbook:
        log(f"Per-fund first-date file: {output_dir / 'FIRST_MQR_DATE_BY_FUND.csv'}")
    if export_comparable_workbook:
        log("Formatted workbook: see MQR_RESULTS_*.xlsx in the output folder")
    log("=" * 80)
    return result
