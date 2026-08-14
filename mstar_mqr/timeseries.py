from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
import morningstar_data as md

from .common import (
    DailyCellLimitExceeded,
    MorningstarAuthError,
    classify_and_raise,
    find_col,
    find_id_col,
    log,
    normalize_blanks,
    save_df,
    slug,
)


def get_long_ts_format():
    """Return Morningstar's long time-series enum if this package version exposes it."""
    enum = md.direct.data_type.TimeSeriesFormat
    for attr in ["LONG", "Long", "long"]:
        if hasattr(enum, attr):
            return getattr(enum, attr)
    try:
        return enum("Long")
    except Exception:
        return None


def ts_datapoint(datapoint_id: str, start_date: str, end_date: str) -> List[dict]:
    """Morningstar time-series datapoint request for one datapoint/date window."""
    return [
        {
            "datapointId": datapoint_id,
            "isTsdp": True,
            "startDate": start_date,
            "endDate": end_date,
        }
    ]


def pull_time_series(
    investment_ids: List[str],
    datapoint_id: str,
    start_date: str,
    end_date: str,
) -> Optional[pd.DataFrame]:
    """Pull one datapoint for a list of investments and date window."""
    kwargs = {
        "investments": investment_ids,
        "data_points": ts_datapoint(datapoint_id, start_date, end_date),
        "display_name": True,
    }
    long_format = get_long_ts_format()
    if long_format is not None:
        kwargs["time_series_format"] = long_format

    try:
        return md.direct.get_investment_data(**kwargs)
    except Exception as exc:
        classify_and_raise(exc)
        return None


def identify_long_columns(df: pd.DataFrame) -> Optional[Tuple[str, str, str]]:
    """Find investment/date/value columns in Morningstar's long time-series output."""
    id_col = find_id_col(df)
    date_col = find_col(df, ["Date", "date", "As Of Date", "asOfDate", "End Date"])
    if not date_col:
        for col in df.columns:
            if "date" in col.lower():
                date_col = col
                break
    if not date_col:
        return None

    ignore = {
        id_col.lower(),
        date_col.lower(),
        "name",
        "investment name",
        "secid",
        "security id",
    }
    value_cols = [c for c in df.columns if c.lower() not in ignore]
    if not value_cols:
        return None
    return id_col, date_col, value_cols[-1]


def standardize_history(raw: pd.DataFrame, datapoint_id: str) -> pd.DataFrame:
    """
    Convert Morningstar long/wide output into investment_id/date/value/datapoint_id.
    """
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["investment_id", "date", "value", "datapoint_id"])

    cols = identify_long_columns(raw)
    if cols:
        id_col, date_col, value_col = cols
        out = raw[[id_col, date_col, value_col]].copy()
        out = out.rename(columns={id_col: "investment_id", date_col: "date", value_col: "value"})
        out["datapoint_id"] = datapoint_id
        return out

    # Fallback for wide output where date columns are headers.
    id_col = find_id_col(raw)
    date_like_cols = []
    for col in raw.columns:
        parsed = pd.to_datetime(str(col), errors="coerce")
        if not pd.isna(parsed):
            date_like_cols.append(col)
    if not date_like_cols:
        raise RuntimeError(f"Could not standardize Morningstar output. Columns: {list(raw.columns)}")

    out = raw.melt(id_vars=[id_col], value_vars=date_like_cols, var_name="date", value_name="value")
    out = out.rename(columns={id_col: "investment_id"})
    out["datapoint_id"] = datapoint_id
    return out


def non_blank_values(raw: pd.DataFrame, datapoint_id: str) -> pd.DataFrame:
    """Standardize and keep only non-blank rows."""
    hist = standardize_history(raw, datapoint_id)
    hist["date"] = pd.to_datetime(hist["date"], errors="coerce")
    hist["value"] = normalize_blanks(hist["value"])
    hist = hist[hist["date"].notna() & hist["value"].notna()].copy()
    if hist.empty:
        return pd.DataFrame(columns=["investment_id", "date", "value", "datapoint_id"])
    hist["date"] = hist["date"].dt.date.astype(str)
    hist["investment_id"] = hist["investment_id"].astype(str)
    hist["datapoint_id"] = datapoint_id
    return hist[["investment_id", "date", "value", "datapoint_id"]]


def batch_file(output_dir: Path, folder: str, datapoint_id: str, month_key: str, batch_no: int) -> Path:
    """Path for a cached raw batch."""
    return output_dir / folder / slug(datapoint_id) / month_key / f"batch_{batch_no:05d}.csv"


def pull_cached_month_batch(
    investment_ids: List[str],
    datapoint_id: str,
    month_key: str,
    month_start: str,
    month_end: str,
    output_dir: Path,
    folder: str,
    batch_no: int,
) -> pd.DataFrame:
    """
    Pull or reuse one month/batch/datapoint raw file.

    Completed files are never re-requested, which is what makes resume safe.
    """
    path = batch_file(output_dir, folder, datapoint_id, month_key, batch_no)
    if path.exists():
        log(f"Skipping existing {folder} {datapoint_id} {month_key} batch {batch_no}")
        try:
            return pd.read_csv(path)
        except Exception:
            pass

    raw = pull_time_series(investment_ids, datapoint_id, month_start, month_end)
    if raw is None:
        raw = pd.DataFrame()
    save_df(raw, path)
    return raw
