from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import morningstar_data as md

from .common import classify_and_raise, find_id_col, log, save_df, slug, unique_strings
from .investments import base_datapoints


ID_COLUMN_CANDIDATES = [
    "Id",
    "ID",
    "id",
    "SecId",
    "secid",
    "SECID",
    "Morningstar SecId",
    "Morningstar Security ID",
    "Security Id",
    "Security ID",
    "SecurityID",
    "Investment Id",
    "Investment ID",
    "investment_id",
    "MSTAR Function",
]

# Morningstar identifiers commonly begin with F or 0P, but the fallback is
# intentionally broader so the importer also works with other valid Direct IDs.
LIKELY_MORNINGSTAR_ID = re.compile(r"^(?:F[A-Z0-9]{8,}|0P[A-Z0-9]{7,}|[A-Z0-9]{8,20})$", re.I)
BLANK_TEXT = {"", "nan", "none", "null", "n/a", "--", "-"}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean_id(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in BLANK_TEXT:
        return ""
    # Excel occasionally turns an ID into a float-looking string.
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _normalized_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _find_explicit_id_column(df: pd.DataFrame) -> Optional[str]:
    normalized = {_normalized_name(col): str(col) for col in df.columns}
    for candidate in ID_COLUMN_CANDIDATES:
        key = _normalized_name(candidate)
        if key in normalized:
            return normalized[key]
    return None


def _id_score(series: pd.Series) -> Tuple[int, float, int]:
    cleaned = series.map(_clean_id)
    cleaned = cleaned[cleaned != ""]
    if cleaned.empty:
        return (0, 0.0, 0)
    matches = cleaned.str.match(LIKELY_MORNINGSTAR_ID)
    return (int(matches.sum()), float(matches.mean()), int(cleaned.nunique()))


def find_input_id_column(df: pd.DataFrame) -> str:
    """
    Find the Morningstar investment-ID column in a generic CSV/Excel list.

    It first checks standard column names. If none are present, it scores every
    column by how many values look like Morningstar IDs. This supports files such
    as SK_AFS_Base.xlsx where the first column is named "MSTAR Function".
    """
    explicit = _find_explicit_id_column(df)
    if explicit is not None:
        return explicit

    best_col: Optional[str] = None
    best_score = (0, 0.0, 0)
    for col in df.columns:
        score = _id_score(df[col])
        if score > best_score:
            best_col = str(col)
            best_score = score

    if best_col is None or best_score[0] == 0:
        raise RuntimeError(
            "Could not identify a Morningstar investment-ID column. "
            f"Columns found: {list(df.columns)}"
        )
    return best_col


def _read_csv_like(path: Path) -> Dict[str, pd.DataFrame]:
    suffix = path.suffix.lower()
    sep = "\t" if suffix in {".tsv", ".txt"} else ","
    return {"data": pd.read_csv(path, dtype=object, sep=sep)}


def _read_excel(path: Path, sheet_names: Optional[Sequence[str]]) -> Dict[str, pd.DataFrame]:
    workbook = pd.ExcelFile(path)
    requested = list(sheet_names or workbook.sheet_names)
    missing = [name for name in requested if name not in workbook.sheet_names]
    if missing:
        raise RuntimeError(
            f"Requested sheet(s) not found in {path.name}: {missing}. "
            f"Available sheets: {workbook.sheet_names}"
        )
    return {
        name: pd.read_excel(path, sheet_name=name, dtype=object)
        for name in requested
    }


def read_investment_list_file(
    file_path: str,
    sheet_names: Optional[Sequence[str]] = None,
) -> Dict[str, pd.DataFrame]:
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise RuntimeError(f"Investment-list file not found: {path}")

    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        return _read_excel(path, sheet_names)
    if suffix in {".csv", ".tsv", ".txt"}:
        return _read_csv_like(path)
    raise RuntimeError(
        f"Unsupported investment-list format: {suffix}. "
        "Use .xlsx, .xls, .xlsm, .csv, .tsv, or .txt."
    )


def load_investment_list(
    file_path: str,
    output_dir: Path,
    list_label: Optional[str] = None,
    sheet_names: Optional[Sequence[str]] = None,
    reuse_existing: bool = True,
) -> pd.DataFrame:
    """
    Load any Morningstar saved list/export containing investment IDs.

    This is generic: it is not tied to South Korea, AFS, or any country. The
    file name becomes the list label unless --investment-list-label is supplied.
    All source sheets are included by default.
    """
    path = Path(file_path).expanduser().resolve()
    label = (list_label or path.stem).strip()
    final_file = output_dir / "02_investments.csv"
    audit_file = output_dir / "00_input_list_audit.csv"
    source_sha256 = _file_sha256(path)

    if reuse_existing and final_file.exists() and audit_file.exists():
        try:
            audit_existing = pd.read_csv(audit_file, dtype=object)
            hashes = set(audit_existing.get("source_sha256", pd.Series(dtype=object)).dropna().astype(str))
            existing = pd.read_csv(final_file, dtype=object)
            if not existing.empty and source_sha256 in hashes:
                log(f"Reusing exact investment list: {final_file}")
                return existing
            if not existing.empty:
                log("Input-list file changed; rebuilding exact-list inputs instead of reusing stale files.")
        except Exception:
            pass

    sheets = read_investment_list_file(str(path), sheet_names)
    frames: List[pd.DataFrame] = []
    audits: List[dict] = []

    for sheet_name, raw in sheets.items():
        if raw.empty:
            audits.append({
                "source_file": path.name,
                "source_sha256": source_sha256,
                "source_sheet": sheet_name,
                "id_column": "",
                "input_rows": 0,
                "valid_ids": 0,
                "unique_ids": 0,
            })
            continue

        id_col = find_input_id_column(raw)
        clean_ids = raw[id_col].map(_clean_id)
        valid_mask = clean_ids.str.match(LIKELY_MORNINGSTAR_ID, na=False)

        canonical = raw.loc[valid_mask].copy()
        canonical.insert(0, "Id", clean_ids.loc[valid_mask].values)
        canonical["input_list_label"] = label
        canonical["input_source_file"] = path.name
        canonical["input_source_path"] = str(path)
        canonical["input_source_sha256"] = source_sha256
        canonical["input_source_sheet"] = sheet_name
        canonical["input_source_row"] = canonical.index.astype(int) + 2  # Excel/CSV row incl. header
        canonical["input_id_column"] = id_col
        canonical["query_universe"] = "INPUT_LIST"

        frames.append(canonical)
        audits.append({
            "source_file": path.name,
            "source_sha256": source_sha256,
            "source_sheet": sheet_name,
            "id_column": id_col,
            "input_rows": len(raw),
            "valid_ids": int(valid_mask.sum()),
            "unique_ids": canonical["Id"].nunique(),
        })

    if not frames:
        save_df(pd.DataFrame(audits), audit_file)
        raise RuntimeError(f"No Morningstar investment IDs were found in {path}")

    combined = pd.concat(frames, ignore_index=True)
    before = len(combined)
    combined = combined.drop_duplicates(subset=["Id"], keep="first").reset_index(drop=True)

    audits.append({
        "source_file": path.name,
        "source_sha256": source_sha256,
        "source_sheet": "ALL",
        "id_column": "",
        "input_rows": before,
        "valid_ids": before,
        "unique_ids": len(combined),
        "duplicates_removed": before - len(combined),
        "list_label": label,
    })

    save_df(pd.DataFrame(audits), audit_file)
    save_df(combined, output_dir / "02_investments_from_input_list.csv")
    save_df(combined, final_file)
    log(f"Loaded exact input list '{label}': {len(combined):,} unique investment IDs")
    return combined


def _load_metadata_batch(path: Path) -> Optional[pd.DataFrame]:
    if not path.exists():
        return None
    try:
        data = pd.read_csv(path, dtype=object)
        return data if not data.empty else None
    except Exception:
        return None


def enrich_input_list_metadata(
    investments: pd.DataFrame,
    output_dir: Path,
    batch_size: int = 5000,
    reuse_existing: bool = True,
) -> pd.DataFrame:
    """
    Add Morningstar name and domicile metadata to an exact input list.

    Each batch is checkpointed. This is optional because it uses cells, but it
    makes the final evidence easier to compare and present to a professor.
    """
    id_col = find_id_col(investments)
    ids = unique_strings(investments[id_col].tolist())
    batch_dir = output_dir / "02a_input_list_metadata_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    frames: List[pd.DataFrame] = []

    for batch_no, start in enumerate(range(0, len(ids), batch_size), start=1):
        batch = ids[start : start + batch_size]
        batch_file = batch_dir / f"batch_{batch_no:05d}.csv"
        existing = _load_metadata_batch(batch_file) if reuse_existing else None
        if existing is not None:
            log(f"Reusing input-list metadata batch {batch_no}")
            frames.append(existing)
            continue

        try:
            raw = md.direct.get_investment_data(
                investments=batch,
                data_points=base_datapoints(),
                display_name=True,
            )
        except Exception as exc:
            classify_and_raise(exc)
            raise

        save_df(raw, batch_file)
        if not raw.empty:
            frames.append(raw)

    if not frames:
        log("No metadata returned for the input list; continuing with source-list columns only.")
        return investments

    metadata = pd.concat(frames, ignore_index=True)
    metadata_id_col = find_id_col(metadata)
    metadata = metadata.drop_duplicates(subset=[metadata_id_col]).copy()
    metadata = metadata.rename(columns={metadata_id_col: "_metadata_investment_id"})
    save_df(metadata, output_dir / "02a_input_list_metadata.csv")

    enriched = investments.copy()
    enriched["_merge_investment_id"] = enriched[id_col].astype(str).str.strip()
    enriched = enriched.merge(
        metadata,
        left_on="_merge_investment_id",
        right_on="_metadata_investment_id",
        how="left",
        suffixes=("", "_morningstar"),
    )
    enriched = enriched.drop(columns=["_merge_investment_id", "_metadata_investment_id"], errors="ignore")
    save_df(enriched, output_dir / "02_investments.csv")
    return enriched


def resolved_list_label(file_path: str, list_label: Optional[str]) -> str:
    return (list_label or Path(file_path).expanduser().stem).strip()


def list_output_dir(base_output_dir: Path, file_path: str, list_label: Optional[str]) -> Path:
    label = resolved_list_label(file_path, list_label)
    out = base_output_dir / "lists" / slug(label)
    out.mkdir(parents=True, exist_ok=True)
    return out
