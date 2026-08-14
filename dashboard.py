from __future__ import annotations

import contextlib
import os
import sys
import traceback
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import streamlit as st

from mstar_mqr.common import DailyCellLimitExceeded, MorningstarAuthError
from mstar_mqr.cloud_storage import (
    cloud_status,
    ensure_bucket_exists,
    restore_all_from_cloud,
    sync_local_roots_to_cloud,
)
from mstar_mqr.dashboard_utils import (
    COUNTRY_OPTIONS,
    COUNTRY_TO_DOMICILE,
    LiveLogBuffer,
    discover_runs,
    evidence_bundle_bytes,
    file_mime,
    format_file_size,
    get_progress_snapshot,
    list_downloadable_files,
    safe_read_csv,
    safe_read_json,
    save_uploaded_file,
)
from mstar_mqr.pipeline import run_pipeline
from mstar_mqr.token_manager import (
    AnalyticsLabLoginError,
    authenticate_with_credentials,
    get_token_status,
    invalidate_token,
    mark_daily_limit_exceeded,
)


PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)


def _apply_hosted_secrets() -> None:
    try:
        supabase_secrets = st.secrets.get("supabase", {})
        app_secrets = st.secrets.get("app", {})
    except Exception:
        supabase_secrets = {}
        app_secrets = {}

    mappings = {
        "SUPABASE_URL": supabase_secrets.get("url", ""),
        "SUPABASE_SECRET_KEY": supabase_secrets.get("secret_key", "") or supabase_secrets.get("service_role_key", ""),
        "SUPABASE_BUCKET": supabase_secrets.get("bucket", "mqr-data"),
        "MSTAR_HEADLESS_AUTH": "1" if bool(app_secrets.get("hosted", False)) else os.environ.get("MSTAR_HEADLESS_AUTH", "0"),
    }
    for key, value in mappings.items():
        if value not in (None, ""):
            os.environ[key] = str(value)


def _hosted_mode() -> bool:
    return os.environ.get("MSTAR_HEADLESS_AUTH", "0").strip().lower() in {"1", "true", "yes"}


_apply_hosted_secrets()

st.set_page_config(
    page_title="Morningstar MQR Research Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "cloud_restore_done" not in st.session_state:
    try:
        ensure_bucket_exists()
        restore_all_from_cloud()
    except Exception:
        pass
    st.session_state["cloud_restore_done"] = True

st.markdown(
    """
    <style>
      .block-container {padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1500px;}
      [data-testid="stSidebar"] {min-width: 320px; max-width: 360px;}
      .mqr-hero {
        padding: 1.35rem 1.5rem; border-radius: 18px;
        background: linear-gradient(135deg, #12233f 0%, #1c4670 58%, #236c77 100%);
        color: white; margin-bottom: 1.15rem;
        box-shadow: 0 12px 28px rgba(17, 38, 70, 0.17);
      }
      .mqr-hero h1 {margin: 0; font-size: 2rem;}
      .mqr-hero p {margin: .45rem 0 0; opacity: .9; font-size: 1.02rem;}
      .mqr-card {
        padding: 1rem 1.1rem; border: 1px solid rgba(49, 76, 107, .18);
        border-radius: 14px; background: rgba(248, 251, 254, .7); margin-bottom: .8rem;
      }
      .small-note {font-size: .86rem; opacity: .78;}
      .stButton > button {border-radius: 10px; font-weight: 650;}
      .stDownloadButton > button {border-radius: 10px;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="mqr-hero">
      <h1>Morningstar MQR Research Dashboard</h1>
      <p>Run, resume, compare, and export country-level Morningstar Quantitative Rating evidence without using Terminal commands.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if st.session_state.get("auth_flash_error"):
    st.error(st.session_state.pop("auth_flash_error"))


def _status_label(status: Dict[str, Any]) -> tuple[str, str]:
    if status.get("valid") and status.get("quota_limited"):
        return "Connected · daily quota reached", "warning"
    if status.get("valid"):
        return "Connected · verified by Morningstar", "success"
    reason = str(status.get("reason", "missing"))
    if reason == "daily_limit_reset_has_passed":
        return "Fresh token required after quota reset", "warning"
    if reason in {"expired", "token_age_near_24_hours"}:
        return "Token expired", "warning"
    return "Not connected", "error"


with st.sidebar:
    st.header("Analytics Lab connection")
    token_status = get_token_status()
    label, level = _status_label(token_status)
    if level == "success":
        st.success(label)
    elif level == "warning":
        st.warning(label)
    else:
        st.error(label)

    if token_status.get("expires_at_utc"):
        st.caption(f"Token expiry: {token_status['expires_at_utc']}")
    if token_status.get("daily_limit_reset_after_utc"):
        st.caption(f"Daily quota reset: {token_status['daily_limit_reset_after_utc']}")

    with st.form("analytics_login", clear_on_submit=False):
        username = st.text_input(
            "Morningstar username/email",
            value=os.environ.get("MSTAR_USERNAME", ""),
            autocomplete="username",
        )
        password = st.text_input(
            "Morningstar password",
            type="password",
            autocomplete="current-password",
            help="Used only for the current authentication attempt. It is not written to disk.",
        )
        connect = st.form_submit_button("Connect / refresh token", use_container_width=True)

    if connect:
        if _hosted_mode():
            st.info("Connecting through the hosted server. Analytics Lab login and token capture run automatically in a headless browser.")
        else:
            st.info("A browser may open. Complete institutional MFA only if requested. After JupyterLab loads, the dashboard automatically opens Analytics Lab → Copy Authentication Token.")
        try:
            with st.spinner("Connecting to Morningstar Analytics Lab…"):
                authenticate_with_credentials(
                    username=username,
                    password=password,
                    use_browser=True,
                    headless=_hosted_mode(),
                    timeout_seconds=120,
                )
            st.success("Analytics Lab connected. No token copy/paste was required.")
            st.rerun()
        except AnalyticsLabLoginError as exc:
            invalidate_token(str(exc))
            st.session_state["auth_flash_error"] = str(exc)
            st.rerun()
        except Exception as exc:
            invalidate_token(str(exc))
            st.session_state["auth_flash_error"] = f"Could not connect to Analytics Lab: {exc}"
            st.rerun()

    st.divider()
    storage = cloud_status()
    if storage.get("enabled"):
        st.success(f"Shared cloud storage · {storage.get('bucket')}")
    elif _hosted_mode():
        st.error("Shared storage is not configured. Add Supabase secrets in Streamlit settings.")
    else:
        st.caption("Shared cloud storage is optional for local use.")

    st.divider()
    st.subheader("Research defaults")
    st.caption("Mutual funds + ETFs")
    st.code("FO + FE", language="text")
    st.caption("Quantitative scan window")
    st.code("2017-01-01 → 2024-12-31", language="text")
    st.caption("Human/Analyst scan")
    st.code("Optional and off", language="text")

run_tab, results_tab, help_tab = st.tabs(["Run or resume", "Progress and results", "Methodology and handoff"])


with run_tab:
    st.subheader("Configure a run")
    st.caption("The same form starts a new run or resumes saved batch progress. Existing output is reused by default.")

    mode = st.radio(
        "Input mode",
        ["Country universe", "Exact fund list / saved group"],
        horizontal=True,
        help=(
            "Country universe searches all FO mutual funds and FE ETFs for the selected domicile. "
            "Exact fund list uses every Morningstar ID from an uploaded workbook or CSV. Use it for AFS, Restricted, FCPE, a professor sample, or any other specific group."
        ),
    )

    col_country, col_dates = st.columns([1, 1.35])
    with col_country:
        country_choice = st.selectbox("Country", COUNTRY_OPTIONS, index=0)
        if country_choice == "Other":
            domicile = st.text_input("Country/domicile value", placeholder="e.g., Luxembourg").strip()
        else:
            domicile = COUNTRY_TO_DOMICILE[country_choice]
        st.caption(f"Pipeline domicile key: `{domicile or '—'}`")

    with col_dates:
        date_col1, date_col2 = st.columns(2)
        with date_col1:
            start_date = st.date_input("Start date", value=date(2017, 1, 1), min_value=date(1990, 1, 1))
        with date_col2:
            end_date = st.date_input("End date", value=date(2024, 12, 31), min_value=date(1990, 1, 1))

    uploaded_file = None
    list_label = ""
    sheet_names_text = ""
    full_history = False
    export_comparable = False
    enrich_metadata = False
    output_template_mode = "professor"
    custom_template_upload = None

    if mode == "Exact fund list / saved group":
        st.markdown("#### Exact fund-list input")
        list_col1, list_col2 = st.columns([1.3, 1])
        with list_col1:
            uploaded_file = st.file_uploader(
                "Upload a Morningstar saved list/export",
                type=["xlsx", "xls", "xlsm", "csv", "tsv", "txt"],
                help="The dashboard automatically identifies the Morningstar investment-ID column and reads all sheets by default.",
            )
        with list_col2:
            list_label = st.text_input(
                "List label (optional)",
                placeholder="e.g., AFS, Restricted, FCPE",
                help="Generic label only. It is not tied to a specific country.",
            )
            sheet_names_text = st.text_input(
                "Sheet names (optional)",
                placeholder="Leave blank for all sheets; or Sheet1, Sheet2",
            )

        enrich_metadata = st.checkbox(
            "Pull extra Morningstar metadata",
            value=False,
            help="Off by default because it consumes substantial cell quota. Source-list columns are preserved without it.",
        )

    # Output choices are independent of fund selection. They apply equally to
    # a normal FO/FE country universe and to an uploaded exact fund list.
    st.markdown("#### Output and history")
    h1, h2 = st.columns(2)
    with h1:
        full_history = st.checkbox(
            "Continue collecting monthly history after the launch month (optional)",
            value=False,
            help=(
                "OFF (recommended): stop immediately when the first MQR month is found. "
                "ON: after the launch result is already found, continue collecting later months through the end date."
            ),
        )
        if not full_history:
            st.caption("Launch search will stop immediately at the first qualifying month.")
        else:
            st.warning("Post-launch history is ON, so the run will continue after the launch month until the selected end date.")
    with h2:
        export_comparable = st.checkbox(
            "Create formatted Excel result",
            value=True,
            help=(
                "Creates the selected Excel output from the data already collected. "
                "With post-launch history OFF, the workbook contains the launch month only and the API scan still stops immediately."
            ),
        )

    if export_comparable:
        st.markdown("#### Excel output template")
        template_choice = st.selectbox(
            "Output template",
            [
                "Professor format (default)",
                "Clean table format",
                "Upload custom Excel template",
            ],
            help=(
                "The template controls only the final Excel layout. It does not change the country, funds, "
                "rating fields, or API queries. Professor format is the default for every run."
            ),
        )
        output_template_mode = {
            "Professor format (default)": "professor",
            "Clean table format": "clean",
            "Upload custom Excel template": "custom",
        }[template_choice]
        if output_template_mode == "custom":
            custom_template_upload = st.file_uploader(
                "Upload custom output template (.xlsx)",
                type=["xlsx"],
                key="custom_output_template",
                help=(
                    "Existing sheets are preserved. The dashboard adds Generated_Summary, First_Quant_Date, "
                    "and Batch sheets. If the workbook has a sheet named Batch_Template, its styles and widths "
                    "are used for every generated Batch sheet. Placeholders such as {{COUNTRY}}, {{LIST_LABEL}}, "
                    "{{FIRST_QUANT_MONTH}}, and {{FUNDS_AT_LAUNCH}} are replaced automatically."
                ),
            )

    with st.expander("Advanced settings", expanded=False):
        adv1, adv2, adv3 = st.columns(3)
        with adv1:
            universes = st.multiselect(
                "Morningstar universes",
                ["FO", "FE", "VA", "VL", "LP"],
                default=["FO", "FE"],
                help="FO and FE are the research default. Select additional universes only for a deliberate supplemental run.",
            )
            batch_size = st.select_slider(
                "Batch size",
                options=[100, 250, 500, 1000, 2500, 5000],
                value=5000,
            )
        with adv2:
            include_legacy = st.checkbox(
                "Search for legacy/pre-Medalist quantitative fields",
                value=False,
                help=(
                    "Advanced fallback only. It scans Morningstar dataset metadata for older field IDs used before "
                    "the current MMR00/MMR08 Medalist fields. It does not mean old or stale ratings."
                ),
            )
            reuse_existing = st.checkbox("Resume/reuse existing files", value=True)
            test_limit = st.number_input(
                "Testing only: limit investment IDs (0 = all)",
                min_value=0,
                max_value=1000000,
                value=0,
                step=10,
                help=(
                    "Uses only the first N investment IDs to confirm that login, fields, and outputs work while "
                    "spending less quota. It is not a complete research result. Leave 0 for the real run."
                ),
            )
        with adv3:
            include_human = st.checkbox(
                "Optional Human/Analyst scan",
                value=False,
                help="Completely optional and separate from the required MQR result.",
            )
            if include_human:
                human_start = st.date_input("Human scan start", value=date(2008, 1, 1))
                human_end = st.date_input("Human scan end", value=date(2024, 12, 31))
            else:
                human_start = date(2008, 1, 1)
                human_end = date(2024, 12, 31)

    run_clicked = st.button("Run / resume pipeline", type="primary", use_container_width=True)

    if run_clicked:
        current_token = get_token_status()
        validation_errors = []
        if not current_token.get("valid"):
            validation_errors.append("Connect to Analytics Lab in the sidebar first.")
        if not domicile:
            validation_errors.append("Enter a country/domicile.")
        if start_date > end_date:
            validation_errors.append("Start date must be on or before end date.")
        if not universes and mode == "Country universe":
            validation_errors.append("Select at least one Morningstar universe.")
        if mode == "Exact fund list / saved group" and uploaded_file is None:
            validation_errors.append("Upload the exact Morningstar fund list/export.")
        if output_template_mode == "custom" and custom_template_upload is None:
            validation_errors.append("Upload the custom Excel output template.")

        if validation_errors:
            for error in validation_errors:
                st.error(error)
        else:
            input_path: Optional[Path] = None
            custom_template_path: Optional[Path] = None
            sheet_names = None
            if uploaded_file is not None:
                input_path = save_uploaded_file(uploaded_file)
                sheet_names = [s.strip() for s in sheet_names_text.split(",") if s.strip()] or None
                st.info(f"Using uploaded fund list: `{input_path.name}`")
            if custom_template_upload is not None:
                custom_template_path = save_uploaded_file(custom_template_upload)
                st.info(f"Using custom output template: `{custom_template_path.name}`")

            log_placeholder = st.empty()
            live_log = LiveLogBuffer(log_placeholder)
            status_box = st.status("Running Morningstar pipeline…", expanded=True)

            try:
                with contextlib.redirect_stdout(live_log), contextlib.redirect_stderr(live_log):
                    result = run_pipeline(
                        domicile=domicile,
                        universes=list(universes),
                        start_date=start_date.isoformat(),
                        end_date=end_date.isoformat(),
                        fallback_start_date="2017-01-01",
                        scan_batch_size=int(batch_size),
                        include_legacy_quantitative=bool(include_legacy),
                        include_human_scan=bool(include_human),
                        human_start_date=human_start.isoformat(),
                        human_end_date=human_end.isoformat(),
                        reuse_existing=bool(reuse_existing),
                        prompt_for_token=False,
                        test_limit_funds=int(test_limit) if int(test_limit) > 0 else None,
                        disable_dedupe=(mode == "Exact fund list / saved group"),
                        investment_list_file=str(input_path) if input_path else None,
                        investment_list_sheets=sheet_names,
                        investment_list_label=list_label or None,
                        enrich_list_metadata=bool(enrich_metadata),
                        full_history=bool(full_history),
                        export_comparable_workbook=bool(export_comparable),
                        output_template_mode=output_template_mode,
                        output_template_file=(
                            str(custom_template_path) if custom_template_path else None
                        ),
                    )
                try:
                    sync_local_roots_to_cloud()
                except Exception:
                    pass
                status_box.update(label="Pipeline complete", state="complete", expanded=False)
                st.success("Run completed. Results are available in the Progress and results tab.")
                st.dataframe(result, use_container_width=True, hide_index=True)
            except DailyCellLimitExceeded:
                mark_daily_limit_exceeded()
                status_box.update(label="Daily Morningstar cell limit reached", state="error", expanded=True)
                reset = get_token_status().get("daily_limit_reset_after_utc", "")
                st.warning(
                    "Progress is checkpointed. After the daily reset, reconnect in the sidebar and click Run / resume again."
                    + (f" Reset recorded for: {reset}." if reset else "")
                )
            except MorningstarAuthError as exc:
                invalidate_token(str(exc))
                status_box.update(label="Authentication failed", state="error", expanded=True)
                st.session_state["auth_flash_error"] = (
                    f"Morningstar rejected the token: {exc}. The rejected value was cleared; reconnect in the sidebar."
                )
                st.rerun()
            except Exception as exc:
                status_box.update(label="Pipeline stopped", state="error", expanded=True)
                st.error(str(exc))
                with st.expander("Technical details"):
                    st.code(traceback.format_exc(), language="text")


with results_tab:
    st.subheader("Saved runs")
    if cloud_status().get("enabled"):
        if st.button("Refresh shared results", use_container_width=False):
            with st.spinner("Refreshing saved runs from shared storage…"):
                restore_all_from_cloud()
            st.success("Shared results refreshed.")
    runs = discover_runs()
    if not runs:
        st.info("No saved runs were found yet. Start one from the Run or resume tab.")
    else:
        runs_df = pd.DataFrame(runs)
        display_cols = [
            "domicile",
            "list_label",
            "mode",
            "status",
            "updated",
            "quantitative_month",
            "funds_at_launch",
        ]
        st.dataframe(runs_df[display_cols], use_container_width=True, hide_index=True)

        run_map = {
            f"{item['domicile']}"
            + (f" · {item['list_label']}" if item.get("list_label") else "")
            + f" · {item['updated'] or item['run_key']}": item
            for item in runs
        }
        selected_label = st.selectbox("Open a saved run", list(run_map.keys()))
        selected = run_map[selected_label]
        run_dir = Path(selected["path"])
        progress = get_progress_snapshot(run_dir)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Status", str(progress.get("status", "unknown")))
        m2.metric("Investment IDs", str(progress.get("investment_ids_used", "—")))
        m3.metric("Months completed", str(progress.get("months_completed", 0)))
        m4.metric("Latest month", str(progress.get("last_completed_month", "—") or "—"))

        summary_path = run_dir / "FINAL_MQR_LAUNCH_RESULT.csv"
        if not summary_path.exists():
            summary_path = run_dir / "08_domicile_launch_candidate.csv"
        summary = safe_read_csv(summary_path)
        if not summary.empty:
            st.markdown("#### Launch result")
            st.dataframe(summary, use_container_width=True, hide_index=True)
        else:
            st.info("The final launch summary has not been produced yet. The run can be resumed from the dashboard.")

        scan_path = run_dir / "MONTHLY_MQR_SCAN_SUMMARY.csv"
        if not scan_path.exists():
            scan_path = run_dir / "10_month_scan_log.csv"
        scan_log = safe_read_csv(scan_path)
        if not scan_log.empty:
            st.markdown("#### Scan progress")
            st.dataframe(scan_log.tail(100), use_container_width=True, hide_index=True)

        st.markdown("#### Download evidence")
        files = list_downloadable_files(run_dir)
        if files:
            download_cols = st.columns(2)
            for index, path in enumerate(files):
                with download_cols[index % 2]:
                    friendly_download_labels = {
                        "FINAL_MQR_LAUNCH_RESULT.csv": "Final launch result",
                        "FUNDS_WITH_MQR_AT_LAUNCH.csv": "Funds rated in the launch month",
                        "MONTHLY_MQR_SCAN_SUMMARY.csv": "Month-by-month scan summary",
                        "MQR_VALUES_AT_LAUNCH.csv": "Rating values in the launch month",
                        "ALL_RATING_ROWS_AT_LAUNCH.csv": "All rating rows at launch",
                        "MQR_HISTORY_ALL_MONTHS.csv": "Monthly MQR history",
                        "FIRST_MQR_DATE_BY_FUND.csv": "First MQR date for each fund",
                        "MQR_RESULTS_PROFESSOR_FORMAT.xlsx": "Final Excel result — Professor format",
                        "MQR_RESULTS_CLEAN_TABLE.xlsx": "Final Excel result — Clean table",
                        "MQR_RESULTS_CUSTOM_TEMPLATE.xlsx": "Final Excel result — Custom template",
                    }
                    display_name = friendly_download_labels.get(path.name, path.name)
                    st.download_button(
                        label=f"{display_name} · {format_file_size(path)}",
                        data=path.read_bytes(),
                        file_name=path.name,
                        mime=file_mime(path),
                        key=f"download_{selected['run_key']}_{path.name}",
                        use_container_width=True,
                    )

            st.download_button(
                "Download professor evidence bundle (.zip)",
                data=evidence_bundle_bytes(run_dir),
                file_name=f"mqr_evidence_{selected['run_key'].replace('/', '_')}.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )


with help_tab:
    st.subheader("What the dashboard does")
    st.markdown(
        """
        1. **Connects to Morningstar Analytics Lab** using the username/password entered in the sidebar. Locally it can use a visible browser; on the hosted app it uses a server-side headless Chromium session and captures the token automatically.
        2. **Builds the research universe** either from the selected country’s FO mutual funds and FE ETFs, or from any uploaded exact Morningstar saved list.
        3. **Finds the first machine-rated month** by scanning `MMR08` for `Quantitative`, then pulling nonblank `MMR00` ratings only for those funds.
        4. **Checkpoints every batch**, so clicking Run / resume after a quota reset continues from saved progress.
        5. **Stops immediately at the first MQR month by default** and exports clearly named professor-ready evidence. Optional post-launch history runs only when explicitly enabled.
        """
    )

    st.markdown("#### Field guide")
    field_guide = pd.DataFrame(
        [
            ["Investment ID / SecId", "Morningstar fund code such as F00000V557"],
            ["MMR00", "Historical Morningstar Medalist Rating value: Gold, Silver, Bronze, Neutral, or Negative"],
            ["MMR08", "Rating type/source used to distinguish Quantitative from Human/Analyst"],
            ["FO", "Open-end / mutual-fund universe"],
            ["FE", "ETF universe"],
            ["Exact list label", "A name used to organize the uploaded fund group, such as AFS, Restricted, FCPE, or Professor Sample. It does not filter the IDs."],
            ["Legacy/pre-Medalist fields", "Optional metadata search for older Morningstar quantitative field IDs used before MMR00/MMR08; off by default"],
            ["Test ID limit", "Testing-only sample size. Zero means use every ID and produce a complete result"],
            ["Auto datapoint settings", "CLI-only start-date option. Reads Morningstar field metadata; it is not used by the dashboard's fixed date picker"],
        ],
        columns=["Field", "Meaning"],
    )
    st.dataframe(field_guide, use_container_width=True, hide_index=True)

    st.markdown("#### Standard operating procedure")
    st.markdown(
        """
        - Open the dashboard launcher.
        - Connect to Analytics Lab in the sidebar.
        - Choose **Country universe** for the main FO/FE country result, or **Exact fund list / saved group** to reproduce a professor-provided list.
        - Keep Human/Analyst scanning off unless separately requested.
        - Click **Run / resume pipeline**.
        - If the quota is reached, return after reset, reconnect, and click the same button. Do not remove the output folder.
        - Download the final summary, comparable workbook, or complete evidence bundle from **Progress and results**.
        """
    )

    st.info(
        "Credentials and raw authentication tokens are not stored in Supabase. Shared storage contains only uploaded inputs, checkpoints, and generated research outputs."
    )
