from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import morningstar_data as md

from .common import classify_and_raise, log, save_df
from .output_templates import export_history_workbook


BLANK_VALUES = {"", "nan", "NaN", "None", "none", "NULL", "null", "--", "-", "N/A", "n/a", "-N/A", "-n/a"}

# Clear, professor-facing output names. The older numbered filenames are still
# written as compatibility aliases so existing runs and scripts do not break.
FINAL_RESULT_FILE = "FINAL_MQR_LAUNCH_RESULT.csv"
FUNDS_AT_LAUNCH_FILE = "FUNDS_WITH_MQR_AT_LAUNCH.csv"
MONTHLY_SCAN_FILE = "MONTHLY_MQR_SCAN_SUMMARY.csv"
VALUES_AT_LAUNCH_FILE = "MQR_VALUES_AT_LAUNCH.csv"
ALL_RATINGS_AT_LAUNCH_FILE = "ALL_RATING_ROWS_AT_LAUNCH.csv"
HISTORY_LONG_FILE = "MQR_HISTORY_ALL_MONTHS.csv"
FIRST_DATE_BY_FUND_FILE = "FIRST_MQR_DATE_BY_FUND.csv"


def clean_token_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text in BLANK_VALUES:
        return ""
    return text


def is_quantitative_text(value: object) -> bool:
    text = clean_token_text(value).lower()
    if not text:
        return False
    if "quant" in text or "computer" in text or "algorithm" in text:
        return True
    if text in {"q", "quant", "quantitative"}:
        return True
    return False


def is_human_text(value: object) -> bool:
    text = clean_token_text(value).lower()
    if not text:
        return False
    if is_quantitative_text(text):
        return False
    if (
        "analyst" in text
        or "human" in text
        or "manager research" in text
        or "qualitative" in text
    ):
        return True
    if text in {"a", "analyst", "human", "qualitative"}:
        return True
    return False


def rating_type_category(value: object) -> str:
    if is_quantitative_text(value):
        return "Quantitative"
    if is_human_text(value):
        return "Human/Analyst"
    text = clean_token_text(value)
    return "Other" if text else "Blank"


def unique_strings(values: List[object]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        text = clean_token_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def chunk_list(values: List[str], batch_size: int) -> List[List[str]]:
    return [values[i : i + batch_size] for i in range(0, len(values), batch_size)]


def month_range(start_date: str, end_date: str) -> List[Tuple[str, str, str]]:
    starts = pd.date_range(
        pd.to_datetime(start_date).to_period("M").to_timestamp(),
        pd.to_datetime(end_date).to_period("M").to_timestamp(),
        freq="MS",
    )
    months = []
    for start in starts:
        end = start + pd.offsets.MonthEnd(0)
        months.append((start.strftime("%Y-%m"), start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")))
    return months


def ts_datapoint(datapoint_id: str, start_date: str, end_date: str) -> List[dict]:
    return [{"datapointId": datapoint_id, "isTsdp": True, "startDate": start_date, "endDate": end_date}]


def safe_slug(value: str) -> str:
    return str(value).strip().lower().replace("/", "_").replace(" ", "_")


def pull_raw_month_batch(
    investment_ids: List[str],
    datapoint_id: str,
    month_start: str,
    month_end: str,
    max_retries: int = 3,
) -> pd.DataFrame:
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            return md.direct.get_investment_data(
                investments=investment_ids,
                data_points=ts_datapoint(datapoint_id, month_start, month_end),
                display_name=True,
            )
        except Exception as exc:  # Morningstar raises several custom exception types.
            last_exc = exc
            text = str(exc).lower()
            name = type(exc).__name__
            if "daily" in text or "limit" in text or "jwt" in text or "auth" in text or "forbidden" in text or "unauthorized" in text:
                classify_and_raise(exc)
                raise
            if "MdApiTaskFailure" in name or "unexpected error occurred" in text:
                wait_seconds = attempt * 10
                log(f"Morningstar task failed for {datapoint_id} {month_start} to {month_end}; retry {attempt}/{max_retries} after {wait_seconds}s")
                time.sleep(wait_seconds)
                continue
            classify_and_raise(exc)
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Unknown Morningstar pull failure")


def pull_cached_month_batch(
    investment_ids: List[str],
    datapoint_id: str,
    month_key: str,
    month_start: str,
    month_end: str,
    output_dir: Path,
    folder_name: str,
    batch_no: int,
) -> pd.DataFrame:
    folder = output_dir / folder_name / safe_slug(datapoint_id) / month_key
    folder.mkdir(parents=True, exist_ok=True)
    batch_file = folder / f"batch_{batch_no:05d}.csv"
    if batch_file.exists():
        log(f"Skipping existing {folder_name} {datapoint_id} {month_key} batch {batch_no}")
        return pd.read_csv(batch_file)
    raw = pull_raw_month_batch(investment_ids, datapoint_id, month_start, month_end)
    save_df(raw, batch_file)
    return raw


def find_id_column(df: pd.DataFrame) -> str:
    candidates = ["investment_id", "Investment Id", "Investment ID", "Id", "ID", "id", "SecId", "secid", "Security Id", "SecurityID"]
    lower_map = {str(col).lower(): col for col in df.columns}
    for candidate in candidates:
        if candidate in df.columns:
            return candidate
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    return df.columns[0]


def find_date_column(df: pd.DataFrame) -> Optional[str]:
    for col in df.columns:
        name = str(col).lower()
        if "date" in name or "as of" in name:
            return col
    return None


def find_value_column(df: pd.DataFrame, datapoint_id: str) -> Optional[str]:
    if datapoint_id in df.columns:
        return datapoint_id
    dp_lower = datapoint_id.lower()
    for col in df.columns:
        if dp_lower in str(col).lower():
            return col
    for col in df.columns:
        if str(col).lower() in {"value", "datapointvalue", "data point value"}:
            return col
    id_col = find_id_column(df)
    date_col = find_date_column(df)
    ignore = {str(id_col).lower()}
    if date_col is not None:
        ignore.add(str(date_col).lower())
    ignore_terms = {"name", "investment name", "security name", "query_universe", "query_status", "query_operator", "query_value"}
    possible = [col for col in df.columns if str(col).lower() not in ignore and str(col).lower() not in ignore_terms]
    return possible[-1] if possible else None


def non_blank_values(raw: pd.DataFrame, datapoint_id: str) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["investment_id", "date", "value", "datapoint_id"])
    id_col = find_id_column(raw)
    date_col = find_date_column(raw)
    value_col = find_value_column(raw, datapoint_id)
    if value_col is None:
        return pd.DataFrame(columns=["investment_id", "date", "value", "datapoint_id"])
    out = pd.DataFrame()
    out["investment_id"] = raw[id_col].astype(str)
    out["date"] = raw[date_col].astype(str) if date_col is not None else ""
    out["value"] = raw[value_col].apply(clean_token_text)
    out["datapoint_id"] = datapoint_id
    out = out[out["value"] != ""].copy()
    return out.reset_index(drop=True)


def add_type_category(type_rows: pd.DataFrame) -> pd.DataFrame:
    if type_rows.empty:
        out = type_rows.copy()
        out["rating_type_category"] = []
        return out
    out = type_rows.copy()
    out["rating_type_category"] = out["value"].apply(rating_type_category)
    return out


def merge_value_and_type(
    value_rows: pd.DataFrame,
    type_rows: pd.DataFrame,
    medalist_value_id: str,
    type_id: Optional[str],
    match_source: str,
) -> pd.DataFrame:
    if value_rows.empty or type_rows.empty:
        return pd.DataFrame()
    type_small = type_rows[["investment_id", "value", "rating_type_category"]].copy()
    type_small = type_small.rename(columns={"value": "rating_type"})
    value_small = value_rows[["investment_id", "date", "value"]].copy()
    value_small = value_small.rename(columns={"value": "rating"})
    merged = value_small.merge(type_small, on="investment_id", how="inner")
    if merged.empty:
        return merged
    merged["rating_datapoint_id"] = medalist_value_id
    merged["type_datapoint_id"] = type_id or ""
    merged["match_source"] = match_source
    return merged


def direct_legacy_rows(value_rows: pd.DataFrame, legacy_id: str) -> pd.DataFrame:
    if value_rows.empty:
        return pd.DataFrame()
    out = value_rows[["investment_id", "date", "value"]].copy()
    out = out.rename(columns={"value": "rating"})
    out["rating_type"] = "Quantitative"
    out["rating_type_category"] = "Quantitative"
    out["rating_datapoint_id"] = legacy_id
    out["type_datapoint_id"] = ""
    out["match_source"] = "legacy_quantitative_datapoint"
    return out


def _pull_values_for_candidates(
    candidate_funds: List[str],
    medalist_value_id: str,
    month_key: str,
    month_start: str,
    month_end: str,
    output_dir: Path,
    folder_name: str,
    scan_batch_size: int,
) -> pd.DataFrame:
    if not candidate_funds:
        return pd.DataFrame()
    frames: List[pd.DataFrame] = []
    for batch_no, batch in enumerate(chunk_list(candidate_funds, scan_batch_size), start=1):
        raw = pull_cached_month_batch(
            investment_ids=batch,
            datapoint_id=medalist_value_id,
            month_key=month_key,
            month_start=month_start,
            month_end=month_end,
            output_dir=output_dir,
            folder_name=folder_name,
            batch_no=batch_no,
        )
        values = non_blank_values(raw, medalist_value_id)
        if not values.empty:
            frames.append(values)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def scan_month_for_ratings(
    investment_ids: List[str],
    month_key: str,
    month_start: str,
    month_end: str,
    output_dir: Path,
    medalist_value_id: str,
    type_datapoint_ids: List[str],
    legacy_value_ids: List[str],
    scan_batch_size: int,
    need_quantitative: bool = True,
    need_human: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, object]]:
    log(f"Scanning month {month_key} for Medalist rating types")
    batches = chunk_list(investment_ids, scan_batch_size)
    used_type_id: Optional[str] = None
    type_values = pd.DataFrame()

    for type_id in type_datapoint_ids:
        type_frames: List[pd.DataFrame] = []
        for batch_no, batch in enumerate(batches, start=1):
            raw = pull_cached_month_batch(
                investment_ids=batch,
                datapoint_id=type_id,
                month_key=month_key,
                month_start=month_start,
                month_end=month_end,
                output_dir=output_dir,
                folder_name="10_month_scan_type_batches",
                batch_no=batch_no,
            )
            values = non_blank_values(raw, type_id)
            if not values.empty:
                type_frames.append(values)
        if type_frames:
            type_values = add_type_category(pd.concat(type_frames, ignore_index=True))
            used_type_id = type_id
            break

    quantitative_type_values = type_values[type_values.get("rating_type_category", pd.Series(dtype=str)) == "Quantitative"].copy() if not type_values.empty else pd.DataFrame()
    human_type_values = type_values[type_values.get("rating_type_category", pd.Series(dtype=str)) == "Human/Analyst"].copy() if (need_human and not type_values.empty) else pd.DataFrame()

    quant_rows = pd.DataFrame()
    human_rows = pd.DataFrame()
    all_rows = pd.DataFrame()

    if need_quantitative and not quantitative_type_values.empty:
        quant_funds = unique_strings(quantitative_type_values["investment_id"].tolist())
        quant_values = _pull_values_for_candidates(
            quant_funds, medalist_value_id, month_key, month_start, month_end, output_dir, "10_month_scan_value_for_quant_batches", scan_batch_size
        )
        quant_rows = merge_value_and_type(quant_values, quantitative_type_values, medalist_value_id, used_type_id, "MMR00_plus_quantitative_type")

    if need_human and not human_type_values.empty:
        human_funds = unique_strings(human_type_values["investment_id"].tolist())
        human_values = _pull_values_for_candidates(
            human_funds, medalist_value_id, month_key, month_start, month_end, output_dir, "10_month_scan_value_for_human_batches", scan_batch_size
        )
        human_rows = merge_value_and_type(human_values, human_type_values, medalist_value_id, used_type_id, "MMR00_plus_human_type")

    # Reuse the already-pulled MMR00 rows. Do not make a second identical value
    # request merely to create the combined evidence file.
    requested_rows: List[pd.DataFrame] = []
    if need_quantitative and not quant_rows.empty:
        requested_rows.append(quant_rows)
    if need_human and not human_rows.empty:
        requested_rows.append(human_rows)
    all_rows = pd.concat(requested_rows, ignore_index=True) if requested_rows else pd.DataFrame()

    legacy_rows = pd.DataFrame()
    legacy_nonblank = 0
    if need_quantitative:
        legacy_frames: List[pd.DataFrame] = []
        for legacy_id in legacy_value_ids:
            for batch_no, batch in enumerate(batches, start=1):
                raw = pull_cached_month_batch(
                    investment_ids=batch,
                    datapoint_id=legacy_id,
                    month_key=month_key,
                    month_start=month_start,
                    month_end=month_end,
                    output_dir=output_dir,
                    folder_name="10_month_scan_legacy_batches",
                    batch_no=batch_no,
                )
                values = non_blank_values(raw, legacy_id)
                if not values.empty:
                    legacy_frames.append(direct_legacy_rows(values, legacy_id))
        if legacy_frames:
            legacy_rows = pd.concat(legacy_frames, ignore_index=True)
            legacy_nonblank = len(legacy_rows)
            quant_rows = pd.concat([quant_rows, legacy_rows], ignore_index=True) if not quant_rows.empty else legacy_rows
            all_rows = pd.concat([all_rows, legacy_rows], ignore_index=True) if not all_rows.empty else legacy_rows

    audit = {
        "month": month_key,
        "funds_scanned": len(investment_ids),
        "type_datapoint_used": used_type_id or "",
        "type_nonblank_rows": 0 if type_values.empty else len(type_values),
        "type_quantitative_rows": 0 if quantitative_type_values.empty else len(quantitative_type_values),
        "funds_with_quantitative_type": 0 if quantitative_type_values.empty else quantitative_type_values["investment_id"].nunique(),
        "type_human_rows": 0 if human_type_values.empty else len(human_type_values),
        "funds_with_human_type": 0 if human_type_values.empty else human_type_values["investment_id"].nunique(),
        "mmr00_quantitative_rows": 0 if quant_rows.empty else len(quant_rows),
        "funds_with_quantitative_mmr00_value": 0 if quant_rows.empty else quant_rows["investment_id"].nunique(),
        "mmr00_human_rows": 0 if human_rows.empty else len(human_rows),
        "funds_with_human_mmr00_value": 0 if human_rows.empty else human_rows["investment_id"].nunique(),
        "legacy_nonblank_rows": legacy_nonblank,
        "all_known_type_rating_rows": 0 if all_rows.empty else len(all_rows),
    }
    return quant_rows, human_rows, all_rows, audit


def save_scan_log(output_dir: Path, scan_rows: List[Dict[str, object]]) -> None:
    if scan_rows:
        rows = pd.DataFrame(scan_rows).sort_values("month") if "month" in pd.DataFrame(scan_rows).columns else pd.DataFrame(scan_rows)
        save_df(rows, output_dir / MONTHLY_SCAN_FILE)
        save_df(rows, output_dir / "10_month_scan_log.csv")



def attach_investment_metadata(rows: pd.DataFrame, investment_metadata: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Attach exact-list/source metadata and fund name fields to evidence rows."""
    if rows.empty or investment_metadata is None or investment_metadata.empty:
        return rows

    metadata = investment_metadata.copy()
    if "_query_investment_id" in metadata.columns:
        metadata_id_col = "_query_investment_id"
    else:
        metadata_id_col = find_id_column(metadata)

    metadata["_metadata_join_id"] = metadata[metadata_id_col].astype(str).str.strip()
    metadata = metadata.drop_duplicates(subset=["_metadata_join_id"]).copy()

    # Preserve every useful source-list field, but avoid duplicate technical columns.
    drop_cols = {metadata_id_col, "_query_investment_id", "_dedupe_key", "_metadata_join_id"}
    keep_cols = [c for c in metadata.columns if c not in drop_cols]
    metadata = metadata[["_metadata_join_id"] + keep_cols]

    out = rows.copy()
    out["investment_id"] = out["investment_id"].astype(str).str.strip()
    out = out.merge(
        metadata,
        left_on="investment_id",
        right_on="_metadata_join_id",
        how="left",
        suffixes=("", "_investment"),
    )
    return out.drop(columns=["_metadata_join_id"], errors="ignore")


def _history_month_path(output_dir: Path, month_key: str) -> Path:
    return output_dir / "13_quantitative_history_months" / f"{month_key}.csv"


def save_quantitative_history_month(
    output_dir: Path,
    domicile: str,
    month_key: str,
    rows: pd.DataFrame,
    investment_metadata: Optional[pd.DataFrame],
) -> None:
    """Checkpoint normalized quantitative history one month at a time."""
    if rows.empty:
        return
    out = attach_investment_metadata(rows, investment_metadata)
    out["month"] = month_key
    out["domicile_requested"] = domicile
    save_df(out, _history_month_path(output_dir, month_key))


def combine_quantitative_history(output_dir: Path) -> pd.DataFrame:
    month_dir = output_dir / "13_quantitative_history_months"
    files = sorted(month_dir.glob("*.csv")) if month_dir.exists() else []
    frames: List[pd.DataFrame] = []
    for path in files:
        try:
            frame = pd.read_csv(path, dtype=object)
        except Exception:
            continue
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    history = pd.concat(frames, ignore_index=True).drop_duplicates()
    if "month" in history.columns:
        history = history.sort_values(["month", "investment_id"]).reset_index(drop=True)
    save_df(history, output_dir / HISTORY_LONG_FILE)
    save_df(history, output_dir / "13_quantitative_history_long.csv")
    return history


def save_first_quantitative_date_by_fund(
    output_dir: Path,
    history: pd.DataFrame,
    investment_metadata: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Create one row for every input fund.

    Funds with no quantitative observation in the requested range remain in the
    file with a blank first date and status=Not observed in requested range.
    """
    observed = pd.DataFrame()
    if not history.empty:
        ordered = history.copy()
        ordered["month"] = ordered["month"].astype(str)
        ordered = ordered.sort_values(["investment_id", "month"]).copy()
        observed = ordered.drop_duplicates(subset=["investment_id"], keep="first").copy()
        observed = observed.rename(columns={
            "month": "first_observed_quantitative_month",
            "rating": "first_observed_quantitative_rating",
            "rating_type": "first_observed_rating_type",
        })

    if investment_metadata is not None and not investment_metadata.empty:
        metadata = investment_metadata.copy()
        metadata_id_col = "_query_investment_id" if "_query_investment_id" in metadata.columns else find_id_column(metadata)
        metadata["investment_id"] = metadata[metadata_id_col].astype(str).str.strip()
        metadata = metadata.drop_duplicates(subset=["investment_id"]).copy()
        metadata = metadata.drop(columns=[metadata_id_col, "_query_investment_id", "_dedupe_key"], errors="ignore")

        if observed.empty:
            first = metadata
            first["first_observed_quantitative_month"] = ""
            first["first_observed_quantitative_rating"] = ""
            first["first_observed_rating_type"] = ""
        else:
            observed_keep = observed.drop(columns=[c for c in metadata.columns if c in observed.columns and c != "investment_id"], errors="ignore")
            first = metadata.merge(observed_keep, on="investment_id", how="left")
    else:
        first = observed

    if first.empty:
        return first

    first["quantitative_observed_in_requested_range"] = (
        first.get("first_observed_quantitative_month", pd.Series(index=first.index, dtype=object))
        .fillna("")
        .astype(str)
        .ne("")
    )
    first["first_date_status"] = first["quantitative_observed_in_requested_range"].map(
        {True: "Observed", False: "Not observed in requested range"}
    )
    first = first.sort_values(["quantitative_observed_in_requested_range", "investment_id"], ascending=[False, True])
    save_df(first, output_dir / FIRST_DATE_BY_FUND_FILE)
    save_df(first, output_dir / "14_first_quantitative_date_by_fund.csv")
    return first.reset_index(drop=True)


def export_comparable_history_workbook(
    output_dir: Path,
    history: pd.DataFrame,
    first_by_fund: pd.DataFrame,
    list_label: str = "",
    investment_metadata: Optional[pd.DataFrame] = None,
    template_mode: str = "professor",
    custom_template_file: Optional[str] = None,
    domicile: str = "",
    quantitative_month: str = "",
    funds_at_launch: int = 0,
) -> Optional[Path]:
    """Create the formatted full-history workbook using the selected template."""
    return export_history_workbook(
        output_dir=output_dir,
        history=history,
        first_by_fund=first_by_fund,
        list_label=list_label,
        investment_metadata=investment_metadata,
        template_mode=template_mode,
        custom_template_file=custom_template_file,
        domicile=domicile,
        quantitative_month=quantitative_month,
        funds_at_launch=funds_at_launch,
    )

def save_category_outputs(
    output_dir: Path,
    domicile: str,
    month_key: str,
    rows: pd.DataFrame,
    category: str,
    investment_metadata: Optional[pd.DataFrame] = None,
) -> None:
    if rows.empty:
        return
    rows = attach_investment_metadata(rows, investment_metadata)
    rows["first_observed_month"] = month_key
    rows["domicile_requested"] = domicile
    if category == "quantitative":
        rows["first_observed_quantitative_month"] = month_key
        # Friendly names shown in the dashboard and professor evidence bundle.
        save_df(rows, output_dir / VALUES_AT_LAUNCH_FILE)
        save_df(rows, output_dir / FUNDS_AT_LAUNCH_FILE)
        # Backward-compatible filenames used by earlier script versions.
        save_df(rows, output_dir / "11_first_quantitative_month_values.csv")
        save_df(rows, output_dir / "09_earliest_quantitative_month_funds.csv")
        save_df(rows, output_dir / "11_first_non_blank_month_values.csv")
        save_df(rows, output_dir / "09_earliest_month_funds.csv")
    elif category == "human":
        rows["first_observed_human_month"] = month_key
        save_df(rows, output_dir / "11b_first_human_month_values.csv")
        save_df(rows, output_dir / "09b_earliest_human_month_funds.csv")


def save_all_ratings_output(output_dir: Path, domicile: str, rows: pd.DataFrame, label: str) -> None:
    if rows.empty:
        return
    out = rows.copy()
    out["domicile_requested"] = domicile
    out["evidence_month_label"] = label
    path = output_dir / ALL_RATINGS_AT_LAUNCH_FILE
    if path.exists():
        old = pd.read_csv(path)
        out = pd.concat([old, out], ignore_index=True).drop_duplicates()
    save_df(out, path)
    save_df(out, output_dir / "12_first_observed_month_all_ratings.csv")


def save_summary(
    domicile: str,
    output_dir: Path,
    quantitative_month: str,
    quantitative_rows: pd.DataFrame,
    human_month: str,
    human_rows: pd.DataFrame,
    medalist_value_id: str,
    type_datapoint_ids: List[str],
    include_human_scan: bool = False,
    list_label: str = "",
) -> pd.DataFrame:
    """
    Save the country-level summary.

    Default output is quantitative/MQR-only because that is the research deliverable.
    Human/Analyst fields are included only when --include-human-scan is used.
    """
    row = {
        "domicile_requested": domicile,
        "input_mode": "exact_investment_list" if list_label else "domicile_fo_fe_search",
        "input_list_label": list_label,
        "earliest_observed_quantitative_month": quantitative_month,
        "funds_at_earliest_quantitative_month": 0 if quantitative_rows.empty else quantitative_rows["investment_id"].nunique(),
        "rating_datapoint_id": medalist_value_id,
        "type_datapoint_id": ", ".join(type_datapoint_ids),
        "source_method": "Morningstar Analytics Lab morningstar_data; monthly MMR08 type/source scan, then MMR00 value pull for quantitative typed funds",
        "confidence_level": "High" if quantitative_month else "Low",
        "interpretation_warning": (
            "Earliest observed month in Morningstar historical data where MMR08/type source indicates Quantitative "
            "and MMR00 is nonblank. This is not necessarily a public launch announcement date."
        ),
    }

    if include_human_scan:
        row["earliest_observed_human_month"] = human_month
        row["funds_at_earliest_human_month"] = 0 if human_rows.empty else human_rows["investment_id"].nunique()
        row["human_scan_note"] = "Optional supporting analysis; not part of the main MQR launch-date deliverable."

    summary = pd.DataFrame([row])
    save_df(summary, output_dir / FINAL_RESULT_FILE)
    save_df(summary, output_dir / "08_domicile_launch_candidate.csv")
    return summary

def run_launch_scan(
    domicile: str,
    investment_ids: List[str],
    output_dir: Path,
    start_date: str,
    end_date: str,
    medalist_value_id: str = "MMR00",
    type_datapoint_ids: Optional[List[str]] = None,
    legacy_value_ids: Optional[List[str]] = None,
    scan_batch_size: int = 5000,
    reuse_existing: bool = True,
    collect_human: bool = False,
    human_start_date: Optional[str] = None,
    human_end_date: Optional[str] = None,
    investment_metadata: Optional[pd.DataFrame] = None,
    full_history: bool = False,
    export_comparable_workbook: bool = False,
    list_label: str = "",
    output_template_mode: str = "professor",
    output_template_file: Optional[str] = None,
    **kwargs,
) -> pd.DataFrame:
    """
    Main launch scan.

    Default behavior finds only the first Quantitative/MQR month.
    Human/Analyst scan is optional and runs separately only when collect_human=True.
    """
    if type_datapoint_ids is None:
        type_datapoint_ids = ["MMR08"]
    if legacy_value_ids is None:
        legacy_value_ids = []

    friendly_final_file = output_dir / FINAL_RESULT_FILE
    legacy_final_file = output_dir / "08_domicile_launch_candidate.csv"
    final_file = friendly_final_file if friendly_final_file.exists() else legacy_final_file
    history_file = output_dir / HISTORY_LONG_FILE
    # A short launch-date run can return the existing final result immediately.
    # Full-history runs must continue so the requested Excel template can be
    # generated or regenerated from the cached month files without API rework.
    # Return immediately only when no new formatted output is requested. If a
    # workbook is requested, the cached monthly files are reused to regenerate it.
    if reuse_existing and final_file.exists() and not full_history and not export_comparable_workbook:
        existing = pd.read_csv(final_file)
        has_human = "earliest_observed_human_month" in existing.columns
        if not collect_human or has_human:
            log(f"Reusing existing final launch file: {final_file}")
            return existing

    scan_rows: List[Dict[str, object]] = []
    existing_log = output_dir / "10_month_scan_log.csv"
    if reuse_existing and existing_log.exists():
        try:
            scan_rows.extend(pd.read_csv(existing_log).to_dict("records"))
        except Exception:
            pass

    quantitative_month = ""
    quantitative_rows = pd.DataFrame()
    human_month = ""
    human_rows = pd.DataFrame()

    # Primary required scan: find the first MQR/Quantitative month.
    # This discovery loop ALWAYS stops immediately when the launch month is found.
    requested_months = month_range(start_date, end_date)
    launch_index: Optional[int] = None

    for month_index, (month_key, month_start, month_end) in enumerate(requested_months):
        q_rows, _, all_rows, audit = scan_month_for_ratings(
            investment_ids=investment_ids,
            month_key=month_key,
            month_start=month_start,
            month_end=month_end,
            output_dir=output_dir,
            medalist_value_id=medalist_value_id,
            type_datapoint_ids=type_datapoint_ids,
            legacy_value_ids=legacy_value_ids,
            scan_batch_size=scan_batch_size,
            need_quantitative=True,
            need_human=False,
        )
        audit["scan_target"] = "launch_discovery"

        scan_rows = [
            row
            for row in scan_rows
            if not (
                row.get("month") == month_key
                and row.get("scan_target") == audit["scan_target"]
            )
        ]
        scan_rows.append(audit)
        save_scan_log(output_dir, scan_rows)

        if not q_rows.empty:
            quantitative_month = month_key
            quantitative_rows = q_rows
            launch_index = month_index
            log(f"Found first quantitative rating month: {month_key}")
            log(f"Launch discovery stopped at {month_key}; later months are not searched for the launch date.")
            save_category_outputs(
                output_dir, domicile, month_key, q_rows, "quantitative", investment_metadata
            )
            save_all_ratings_output(
                output_dir,
                domicile,
                attach_investment_metadata(all_rows, investment_metadata),
                f"quantitative_first_month_{month_key}",
            )
            # Always checkpoint the launch month so a formatted workbook can be
            # created without scanning through the selected end date.
            save_quantitative_history_month(
                output_dir, domicile, month_key, q_rows, investment_metadata
            )
            break

    # Optional history phase. This is NOT part of launch discovery and runs only
    # when the user explicitly enables post-launch monthly history.
    if full_history and quantitative_month and launch_index is not None:
        remaining_months = requested_months[launch_index + 1 :]
        if remaining_months:
            log(
                "Optional post-launch history is enabled. "
                f"Collecting {remaining_months[0][0]} through {remaining_months[-1][0]}."
            )
        for month_key, month_start, month_end in remaining_months:
            q_rows, _, _, audit = scan_month_for_ratings(
                investment_ids=investment_ids,
                month_key=month_key,
                month_start=month_start,
                month_end=month_end,
                output_dir=output_dir,
                medalist_value_id=medalist_value_id,
                type_datapoint_ids=type_datapoint_ids,
                legacy_value_ids=legacy_value_ids,
                scan_batch_size=scan_batch_size,
                need_quantitative=True,
                need_human=False,
            )
            audit["scan_target"] = "optional_post_launch_history"
            scan_rows = [
                row
                for row in scan_rows
                if not (
                    row.get("month") == month_key
                    and row.get("scan_target") == audit["scan_target"]
                )
            ]
            scan_rows.append(audit)
            save_scan_log(output_dir, scan_rows)
            if not q_rows.empty:
                save_quantitative_history_month(
                    output_dir, domicile, month_key, q_rows, investment_metadata
                )

    # Optional supporting scan: first Human/Analyst month. This is separate because
    # analyst ratings can start much earlier than MQR and are not part of the main deliverable.
    if collect_human:
        h_start = human_start_date or start_date
        h_end = human_end_date or end_date
        log(f"Starting optional human/analyst scan: {h_start} to {h_end}")

        for month_key, month_start, month_end in month_range(h_start, h_end):
            if human_month:
                break

            _, h_rows, all_rows, audit = scan_month_for_ratings(
                investment_ids=investment_ids,
                month_key=month_key,
                month_start=month_start,
                month_end=month_end,
                output_dir=output_dir,
                medalist_value_id=medalist_value_id,
                type_datapoint_ids=type_datapoint_ids,
                legacy_value_ids=[],
                scan_batch_size=scan_batch_size,
                need_quantitative=False,
                need_human=True,
            )
            audit["scan_target"] = "human_optional"

            scan_rows = [row for row in scan_rows if not (row.get("month") == month_key and row.get("scan_target") == audit["scan_target"])]
            scan_rows.append(audit)
            save_scan_log(output_dir, scan_rows)

            if not h_rows.empty:
                human_month = month_key
                human_rows = h_rows
                log(f"Found first human/analyst rating month: {month_key}")
                save_category_outputs(output_dir, domicile, month_key, h_rows, "human", investment_metadata)
                save_all_ratings_output(output_dir, domicile, attach_investment_metadata(all_rows, investment_metadata), f"human_first_month_{month_key}")
                break

    # Build per-fund/output files from whatever history is available. With
    # post-launch history off, this contains only the launch month and therefore
    # does not trigger any additional Morningstar calls.
    if quantitative_month and (full_history or export_comparable_workbook):
        history = combine_quantitative_history(output_dir)
        first_by_fund = save_first_quantitative_date_by_fund(
            output_dir, history, investment_metadata
        )
        if export_comparable_workbook:
            export_comparable_history_workbook(
                output_dir=output_dir,
                history=history,
                first_by_fund=first_by_fund,
                list_label=list_label,
                investment_metadata=investment_metadata,
                template_mode=output_template_mode,
                custom_template_file=output_template_file,
                domicile=domicile,
                quantitative_month=quantitative_month,
                funds_at_launch=(
                    0
                    if quantitative_rows.empty
                    else quantitative_rows["investment_id"].nunique()
                ),
            )

    return save_summary(
        domicile=domicile,
        output_dir=output_dir,
        quantitative_month=quantitative_month,
        quantitative_rows=quantitative_rows,
        human_month=human_month,
        human_rows=human_rows,
        medalist_value_id=medalist_value_id,
        type_datapoint_ids=type_datapoint_ids,
        include_human_scan=collect_human,
        list_label=list_label,
    )
