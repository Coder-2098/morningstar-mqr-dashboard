from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import morningstar_data as md

from .common import (
    DailyCellLimitExceeded,
    find_col,
    find_id_col,
    is_daily_limit_error,
    log,
    normalize_blanks,
    read_df_if_exists,
    save_df,
    slug,
    update_state,
)


def get_long_ts_format():
    enum = md.direct.data_type.TimeSeriesFormat

    if hasattr(enum, "LONG"):
        return enum.LONG
    if hasattr(enum, "Long"):
        return enum.Long

    try:
        return enum("Long")
    except Exception:
        return None


def ts_datapoint(datapoint_id: str, start_date: str, end_date: str) -> List[Dict]:
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
        if is_daily_limit_error(exc):
            raise DailyCellLimitExceeded(str(exc)) from exc
        log(f"Time-series pull failed for {datapoint_id}: {type(exc).__name__}: {exc}")
        return None


def identify_long_columns(df: pd.DataFrame) -> Optional[Tuple[str, str, str]]:
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


def standardize_history(df: pd.DataFrame, datapoint_id: str) -> pd.DataFrame:
    cols = identify_long_columns(df)

    if cols:
        id_col, date_col, value_col = cols
        out = df[[id_col, date_col, value_col]].copy()
        out = out.rename(columns={id_col: "investment_id", date_col: "date", value_col: "value"})
        out["datapoint_id"] = datapoint_id
        return out

    id_col = find_id_col(df)

    date_like_cols = []
    for col in df.columns:
        parsed = pd.to_datetime(col, errors="coerce")
        if not pd.isna(parsed):
            date_like_cols.append(col)

    if not date_like_cols:
        raise RuntimeError(f"Could not standardize history. Columns: {list(df.columns)}")

    out = df.melt(id_vars=[id_col], value_vars=date_like_cols, var_name="date", value_name="value")
    out = out.rename(columns={id_col: "investment_id"})
    out["datapoint_id"] = datapoint_id
    return out


def summarize_candidate(df: Optional[pd.DataFrame], datapoint_id: str, candidate_score: int) -> Dict:
    if df is None or df.empty:
        return {
            "datapoint_id": datapoint_id,
            "candidate_score": candidate_score,
            "non_blank_rows": 0,
            "unique_funds": 0,
            "earliest_date": None,
        }

    try:
        hist = standardize_history(df, datapoint_id)
    except Exception:
        return {
            "datapoint_id": datapoint_id,
            "candidate_score": candidate_score,
            "non_blank_rows": 0,
            "unique_funds": 0,
            "earliest_date": None,
        }

    hist["date"] = pd.to_datetime(hist["date"], errors="coerce")
    hist["value"] = normalize_blanks(hist["value"])
    hist = hist[hist["date"].notna() & hist["value"].notna()].copy()

    if hist.empty:
        return {
            "datapoint_id": datapoint_id,
            "candidate_score": candidate_score,
            "non_blank_rows": 0,
            "unique_funds": 0,
            "earliest_date": None,
        }

    return {
        "datapoint_id": datapoint_id,
        "candidate_score": candidate_score,
        "non_blank_rows": len(hist),
        "unique_funds": hist["investment_id"].nunique(),
        "earliest_date": str(hist["date"].min().date()),
    }


def test_rating_candidates(
    candidates: pd.DataFrame,
    investment_ids: List[str],
    output_dir: Path,
    start_date: str,
    end_date: str,
    top_n: int = 10,
    sample_size: int = 30,
    reuse_existing: bool = True,
) -> List[str]:
    output_file = output_dir / "05_candidate_test_results.csv"
    if reuse_existing:
        existing = read_df_if_exists(output_file)
        if existing is not None and not existing.empty:
            usable = existing[existing["non_blank_rows"] > 0]
            if not usable.empty:
                selected = usable.sort_values(
                    ["unique_funds", "non_blank_rows", "candidate_score"],
                    ascending=[False, False, False],
                )["datapoint_id"].head(3).tolist()
                log(f"Reusing selected datapoints from 05_candidate_test_results.csv: {selected}")
                return selected

    log("Testing rating candidates on sample funds")

    sample_ids = investment_ids[:sample_size]
    results: List[Dict] = []

    test_rows = candidates.head(top_n).copy()
    sample_dir = output_dir / "sample_candidate_outputs"
    sample_dir.mkdir(parents=True, exist_ok=True)

    for _, row in test_rows.iterrows():
        datapoint_id = str(row["datapointId"])
        score = int(row.get("candidate_score", 0))

        log(f"Testing datapoint={datapoint_id} on {len(sample_ids)} funds")

        try:
            df = pull_time_series(sample_ids, datapoint_id, start_date, end_date)
        except DailyCellLimitExceeded as exc:
            save_df(pd.DataFrame(results), output_file)
            update_state(
                output_dir,
                status="daily_limit_exceeded_during_candidate_testing",
                last_error=str(exc),
                resume_hint="Run the same command after the 12AM UTC reset with a fresh Analytics Lab token.",
            )
            raise

        if df is not None and not df.empty:
            save_df(df, sample_dir / f"sample_{slug(datapoint_id)}.csv")

        results.append(summarize_candidate(df, datapoint_id, score))

    results_df = pd.DataFrame(results)
    results_df = results_df.sort_values(
        ["unique_funds", "non_blank_rows", "candidate_score"],
        ascending=[False, False, False],
    )

    save_df(results_df, output_file)

    usable = results_df[results_df["non_blank_rows"] > 0].copy()
    if usable.empty:
        raise RuntimeError(
            "No candidate datapoint returned historical rating values. "
            "Check 04_rating_datapoint_candidates.csv and 05_candidate_test_results.csv."
        )

    selected = usable["datapoint_id"].head(3).tolist()
    update_state(output_dir, candidate_testing_done=True, selected_datapoints=selected)
    log(f"Selected datapoints for full pull: {selected}")
    return selected


def batch_file_path(output_dir: Path, datapoint_id: str, batch_no: int) -> Path:
    return output_dir / "06_history_batches" / slug(datapoint_id) / f"batch_{batch_no:05d}.csv"


def pull_full_history_resumable(
    investment_ids: List[str],
    datapoint_id: str,
    output_dir: Path,
    start_date: str,
    end_date: str,
    batch_size: int = 100,
) -> pd.DataFrame:
    log(f"Pulling full history for datapoint={datapoint_id} with resumable batches")

    total_batches = (len(investment_ids) + batch_size - 1) // batch_size
    batch_dir = output_dir / "06_history_batches" / slug(datapoint_id)
    batch_dir.mkdir(parents=True, exist_ok=True)

    for batch_no, start in enumerate(range(0, len(investment_ids), batch_size), start=1):
        path = batch_file_path(output_dir, datapoint_id, batch_no)

        if path.exists():
            log(f"Skipping existing batch {batch_no}/{total_batches}: {path}")
            continue

        batch = investment_ids[start : start + batch_size]
        log(f"{datapoint_id}: pulling batch {batch_no}/{total_batches}, funds={len(batch)}")

        try:
            df = pull_time_series(batch, datapoint_id, start_date, end_date)
        except DailyCellLimitExceeded as exc:
            update_state(
                output_dir,
                status="daily_limit_exceeded_during_full_history",
                datapoint_id=datapoint_id,
                next_batch=batch_no,
                total_batches=total_batches,
                batch_size=batch_size,
                last_error=str(exc),
                resume_hint="Run the same command after the 12AM UTC reset with a fresh Analytics Lab token. Completed batches will be skipped.",
            )
            raise

        if df is None:
            df = pd.DataFrame()
        save_df(df, path)

    frames: List[pd.DataFrame] = []
    for batch_no in range(1, total_batches + 1):
        path = batch_file_path(output_dir, datapoint_id, batch_no)
        if path.exists():
            try:
                df = pd.read_csv(path)
                if not df.empty:
                    frames.append(df)
            except Exception as exc:
                log(f"Could not read batch file {path}: {type(exc).__name__}: {exc}")

    if not frames:
        raise RuntimeError(f"No history data was collected for datapoint={datapoint_id}.")

    result = pd.concat(frames, ignore_index=True)
    save_df(result, output_dir / f"06_full_history_{slug(datapoint_id)}.csv")
    update_state(output_dir, full_history_done=True, datapoint_id=datapoint_id)
    return result


def first_rating_by_fund(history: pd.DataFrame, datapoint_id: str) -> pd.DataFrame:
    hist = standardize_history(history, datapoint_id)
    hist["date"] = pd.to_datetime(hist["date"], errors="coerce")
    hist["value"] = normalize_blanks(hist["value"])
    hist = hist[hist["date"].notna() & hist["value"].notna()].copy()

    if hist.empty:
        return pd.DataFrame()

    hist = hist.sort_values("date")
    first = hist.groupby("investment_id", as_index=False).first()

    first = first.rename(columns={"date": "first_rating_date", "value": "first_rating_value"})
    first["first_rating_month"] = pd.to_datetime(first["first_rating_date"]).dt.to_period("M").astype(str)
    first["datapoint_id"] = datapoint_id
    return first


def create_launch_outputs(
    domicile: str,
    selected_datapoints: List[str],
    investment_ids: List[str],
    output_dir: Path,
    start_date: str,
    end_date: str,
    batch_size: int = 100,
) -> pd.DataFrame:
    first_frames: List[pd.DataFrame] = []

    for datapoint_id in selected_datapoints:
        history = pull_full_history_resumable(
            investment_ids=investment_ids,
            datapoint_id=datapoint_id,
            output_dir=output_dir,
            start_date=start_date,
            end_date=end_date,
            batch_size=batch_size,
        )

        first = first_rating_by_fund(history, datapoint_id)
        if not first.empty:
            first_frames.append(first)

    if not first_frames:
        raise RuntimeError("No first-rating records found after full history pull.")

    all_first = pd.concat(first_frames, ignore_index=True)
    save_df(all_first, output_dir / "07_first_rating_by_fund.csv")

    launch = all_first.sort_values("first_rating_date").head(1).copy()
    launch["domicile_requested"] = domicile
    save_df(launch, output_dir / "08_domicile_launch_candidate.csv")

    update_state(
        output_dir,
        status="complete",
        launch_file=str(output_dir / "08_domicile_launch_candidate.csv"),
    )

    log("Launch candidate:")
    print(launch.to_string(index=False))
    return launch
