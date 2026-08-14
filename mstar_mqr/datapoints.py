from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import morningstar_data as md

from .common import classify_and_raise, find_col, log, read_df_if_exists, save_df, slug, unique_strings


# MMR00 is the current Morningstar Medalist Rating value stream.
DEFAULT_VALUE_DATAPOINT = "MMR00"

# Morningstar installations may expose the type/source datapoint as MMR8 or MMR08.
DEFAULT_TYPE_DATAPOINT_CANDIDATES = ["MMR08"]


DATASET_KEYWORDS = [
    "medalist",
    "quantitative",
    "analyst rating",
    "manager research",
    "rating",
]

DATAPOINT_KEYWORDS = [
    "quantitative",
    "analyst rating - quantitative",
    "quantitative rating",
    "medalist rating type",
    "rating type",
]

BAD_KEYWORDS = [
    "equity",
    "stock",
    "sustainability",
    "esg",
    "credit",
    "risk",
    "performance",
    "star rating",
    "low carbon",
]


def dataset_score(name: str) -> int:
    """Score dataset names so we scan likely rating datasets first."""
    text = str(name).lower()
    score = 0
    if "medalist" in text:
        score += 100
    if "quantitative" in text:
        score += 100
    if "analyst rating" in text:
        score += 80
    if "manager research" in text:
        score += 50
    if "rating" in text:
        score += 40
    for bad in BAD_KEYWORDS:
        if bad in text:
            score -= 100
    return score


def datapoint_score(row: pd.Series) -> int:
    """Score datapoint rows; legacy quantitative fields should rank highest."""
    text = " ".join(str(row.get(c, "")) for c in row.index).lower()
    score = 0
    if "analyst rating - quantitative" in text:
        score += 180
    if "quantitative rating" in text:
        score += 160
    if "quantitative" in text:
        score += 120
    if "medalist" in text and "type" in text:
        score += 80
    if "rating type" in text:
        score += 60
    if "rating" in text:
        score += 20
    for bad in BAD_KEYWORDS:
        if bad in text:
            score -= 120
    return score


def get_dataset_id_col(df: pd.DataFrame) -> str:
    col = find_col(df, ["datasetId", "id", "Id", "ID"])
    if not col:
        raise RuntimeError(f"Could not find dataset id column. Columns: {list(df.columns)}")
    return col


def get_dataset_name_col(df: pd.DataFrame) -> str:
    col = find_col(df, ["name", "Name", "datasetName", "Dataset Name"])
    if not col:
        raise RuntimeError(f"Could not find dataset name column. Columns: {list(df.columns)}")
    return col


def get_all_datasets(output_dir: Path, universes: List[str], reuse_existing: bool = True) -> pd.DataFrame:
    """Fetch Morningstar/user datasets once and cache them."""
    path = output_dir / "03_all_datasets.csv"
    if reuse_existing:
        existing = read_df_if_exists(path)
        if existing is not None and not existing.empty:
            log(f"Reusing dataset list: {path}")
            return existing

    log("Fetching Morningstar/user datasets")
    frames: List[pd.DataFrame] = []

    try:
        user_ds = md.direct.user_items.get_data_sets()
        if user_ds is not None and not user_ds.empty:
            user_ds = user_ds.copy()
            user_ds["dataset_group"] = "user_or_shared"
            frames.append(user_ds)
    except Exception as exc:
        classify_and_raise(exc)
        log(f"Could not fetch user datasets: {type(exc).__name__}: {exc}")

    for universe in universes:
        try:
            ds = md.direct.lookup.get_morningstar_data_sets(universe=universe)
            if ds is not None and not ds.empty:
                ds = ds.copy()
                ds["dataset_group"] = f"morningstar_{universe}"
                frames.append(ds)
        except Exception as exc:
            classify_and_raise(exc)
            log(f"Could not fetch Morningstar datasets for universe={universe}: {type(exc).__name__}: {exc}")

    if not frames:
        raise RuntimeError("No datasets found. Cannot discover legacy quantitative datapoints.")

    datasets = pd.concat(frames, ignore_index=True)
    id_col = get_dataset_id_col(datasets)
    datasets = datasets.drop_duplicates(subset=[id_col]).reset_index(drop=True)
    save_df(datasets, path)
    return datasets


def filter_target_datasets(datasets: pd.DataFrame, max_datasets: int) -> pd.DataFrame:
    """Keep only datasets whose names look relevant to ratings."""
    name_col = get_dataset_name_col(datasets)
    targeted = datasets.copy()
    targeted["dataset_name_score"] = targeted[name_col].apply(dataset_score)
    targeted = targeted[targeted["dataset_name_score"] > 0].copy()
    targeted = targeted.sort_values("dataset_name_score", ascending=False)
    if targeted.empty:
        targeted = datasets.head(max_datasets).copy()
        targeted["dataset_name_score"] = 0
    else:
        targeted = targeted.head(max_datasets).copy()
    return targeted


def fetch_dataset_details(dataset_id: str, cache_dir: Path) -> Optional[pd.DataFrame]:
    """Fetch details for one dataset and cache them by dataset id."""
    cache_file = cache_dir / f"{slug(dataset_id)}.csv"
    if cache_file.exists():
        try:
            return pd.read_csv(cache_file)
        except Exception:
            pass

    try:
        details = md.direct.user_items.get_data_set_details(dataset_id)
        if details is not None and not details.empty:
            details.to_csv(cache_file, index=False)
        return details
    except Exception as exc:
        classify_and_raise(exc)
        log(f"Dataset details failed for {dataset_id}: {type(exc).__name__}: {exc}")
        return None


def find_candidate_rows(details: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Find datapoints that look like legacy quantitative rating or type/source fields."""
    if details is None or details.empty or "datapointId" not in details.columns:
        return pd.DataFrame()

    text = details.astype(str).agg(" ".join, axis=1).str.lower()
    mask = pd.Series(False, index=details.index)
    for keyword in DATAPOINT_KEYWORDS:
        mask = mask | text.str.contains(keyword, na=False)

    hits = details.loc[mask].copy()
    if hits.empty:
        return pd.DataFrame()

    hits["candidate_score"] = hits.apply(datapoint_score, axis=1)
    hits = hits[hits["candidate_score"] > 0].copy()
    return hits


def discover_quantitative_datapoints(
    output_dir: Path,
    universes: List[str],
    max_datasets: int = 60,
    workers: int = 4,
    reuse_existing: bool = True,
) -> pd.DataFrame:
    """
    Discover old quantitative-related datapoints from Morningstar dataset metadata.

    This is metadata discovery, not the expensive fund-month history pull.
    """
    path = output_dir / "04_quantitative_datapoint_candidates.csv"
    if reuse_existing:
        existing = read_df_if_exists(path)
        if existing is not None and not existing.empty:
            log(f"Reusing quantitative datapoint candidates: {path}")
            return existing

    datasets = get_all_datasets(output_dir, universes, reuse_existing=reuse_existing)
    targeted = filter_target_datasets(datasets, max_datasets=max_datasets)
    save_df(targeted, output_dir / "03b_targeted_datasets.csv")

    id_col = get_dataset_id_col(targeted)
    name_col = get_dataset_name_col(targeted)
    cache_dir = output_dir / "dataset_detail_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    frames: List[pd.DataFrame] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {}
        for _, row in targeted.iterrows():
            dataset_id = str(row[id_col])
            future = executor.submit(fetch_dataset_details, dataset_id, cache_dir)
            future_map[future] = {
                "dataset_id": dataset_id,
                "dataset_name": str(row[name_col]),
                "dataset_name_score": row.get("dataset_name_score", 0),
            }

        total = len(future_map)
        for i, future in enumerate(as_completed(future_map), start=1):
            meta = future_map[future]
            log(f"Scanned dataset {i}/{total}: {meta['dataset_name']}")
            details = future.result()
            hits = find_candidate_rows(details)
            if not hits.empty:
                hits["source_dataset_id"] = meta["dataset_id"]
                hits["source_dataset_name"] = meta["dataset_name"]
                hits["source_dataset_name_score"] = meta["dataset_name_score"]
                frames.append(hits)

    if not frames:
        empty = pd.DataFrame(columns=["datapointId", "candidate_score"])
        save_df(empty, path)
        return empty

    candidates = pd.concat(frames, ignore_index=True)
    candidates = candidates.drop_duplicates(subset=["datapointId"], keep="first")
    candidates = candidates.sort_values(["candidate_score", "source_dataset_name_score"], ascending=[False, False])
    save_df(candidates, path)
    return candidates


def legacy_quantitative_value_ids(candidates: pd.DataFrame, max_ids: int = 3) -> List[str]:
    """
    Return likely old quantitative value datapoints.

    We exclude type/source/disclosure rows because those are not rating values.
    """
    if candidates is None or candidates.empty or "datapointId" not in candidates.columns:
        return []

    rows = candidates.copy()
    text = rows.astype(str).agg(" ".join, axis=1).str.lower()
    keep = text.str.contains("quantitative", na=False)
    bad = text.str.contains("type|source|disclosure|input data date|date", na=False)
    rows = rows[keep & ~bad].copy()
    rows = rows.sort_values("candidate_score", ascending=False)
    return unique_strings(rows["datapointId"].head(max_ids).tolist())


def type_datapoint_candidates(candidates: pd.DataFrame) -> List[str]:
    """Return MMR type/source datapoint candidates, with known IDs first."""
    ids: List[str] = DEFAULT_TYPE_DATAPOINT_CANDIDATES.copy()
    if candidates is not None and not candidates.empty and "datapointId" in candidates.columns:
        text = candidates.astype(str).agg(" ".join, axis=1).str.lower()
        rows = candidates[text.str.contains("type|source", na=False)].copy()
        ids.extend(rows["datapointId"].dropna().astype(str).tolist())
    return unique_strings(ids)


def datapoint_settings(data_point_ids: List[str], output_dir: Path, reuse_existing: bool = True) -> pd.DataFrame:
    """Fetch settings one datapoint at a time so one bad ID does not kill the run."""
    path = output_dir / "00_datapoint_settings.csv"
    if reuse_existing:
        existing = read_df_if_exists(path)
        if existing is not None and not existing.empty:
            known = set(existing.get("datapointId", pd.Series(dtype=str)).astype(str).tolist())
            if set(data_point_ids).issubset(known):
                return existing

    frames: List[pd.DataFrame] = []
    for dp_id in unique_strings(data_point_ids):
        try:
            settings = md.direct.get_data_point_settings(data_point_ids=[dp_id])
            if settings is not None and not settings.empty:
                frames.append(settings)
        except Exception as exc:
            classify_and_raise(exc)
            log(f"Could not fetch settings for datapoint={dp_id}: {type(exc).__name__}: {exc}")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["datapointId"], keep="first") if "datapointId" in df.columns else df
    save_df(df, path)
    return df


def earliest_settings_start_date(settings: pd.DataFrame, fallback: str) -> str:
    """Use Morningstar datapoint settings to avoid hardcoding a 2017 start date."""
    if settings is None or settings.empty:
        return fallback

    date_cols = [c for c in ["startDate", "floatStart"] if c in settings.columns]
    dates: List[pd.Timestamp] = []
    for col in date_cols:
        parsed = pd.to_datetime(settings[col], errors="coerce")
        dates.extend([d for d in parsed.dropna().tolist()])

    if not dates:
        return fallback
    return min(dates).date().isoformat()
