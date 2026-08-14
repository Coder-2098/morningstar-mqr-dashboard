from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from .common import slug


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "output"
INPUT_ROOT = PROJECT_ROOT / "inputs"


COUNTRY_OPTIONS = [
    "Australia",
    "Canada",
    "France",
    "Germany",
    "Japan",
    "Netherlands",
    "South Korea",
    "Sweden",
    "United Kingdom",
    "United States",
    "Other",
]

COUNTRY_TO_DOMICILE = {
    "Australia": "aus",
    "Canada": "canada",
    "France": "france",
    "Germany": "germany",
    "Japan": "japan",
    "Netherlands": "netherlands",
    "South Korea": "korea",
    "Sweden": "sweden",
    "United Kingdom": "uk",
    "United States": "usa",
}


class LiveLogBuffer(io.TextIOBase):
    """Collect print output and optionally push it to a Streamlit placeholder."""

    def __init__(self, placeholder: Any = None, max_lines: int = 350) -> None:
        self.placeholder = placeholder
        self.max_lines = max_lines
        self._partial = ""
        self.lines: List[str] = []

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not text:
            return 0
        combined = self._partial + str(text)
        parts = combined.split("\n")
        self._partial = parts.pop()
        self.lines.extend(parts)
        if len(self.lines) > self.max_lines:
            self.lines = self.lines[-self.max_lines :]
        self.render()
        return len(text)

    def flush(self) -> None:
        self.render()

    def render(self) -> None:
        if self.placeholder is None:
            return
        display = self.lines + ([self._partial] if self._partial else [])
        try:
            self.placeholder.code("\n".join(display[-self.max_lines :]), language="text")
        except Exception:
            pass

    def text(self) -> str:
        display = self.lines + ([self._partial] if self._partial else [])
        return "\n".join(display)


def date_to_string(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def save_uploaded_file(uploaded_file: Any) -> Path:
    """Save an uploaded list deterministically so reruns resume the same output."""
    INPUT_ROOT.mkdir(parents=True, exist_ok=True)
    raw = uploaded_file.getvalue()
    digest = hashlib.sha256(raw).hexdigest()[:10]
    original = Path(uploaded_file.name)
    clean_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", original.stem).strip("_") or "investment_list"
    suffix = original.suffix.lower()
    path = INPUT_ROOT / f"{clean_stem}_{digest}{suffix}"
    if not path.exists() or path.read_bytes() != raw:
        path.write_bytes(raw)
    try:
        from .cloud_storage import sync_file_to_cloud

        sync_file_to_cloud(path)
    except Exception:
        pass
    return path


def safe_read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def safe_read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def discover_runs(output_root: Path = OUTPUT_ROOT) -> List[Dict[str, Any]]:
    if not output_root.exists():
        return []

    found: List[Dict[str, Any]] = []
    for state_path in output_root.rglob("run_state.json"):
        run_dir = state_path.parent
        state = safe_read_json(state_path)
        summary_path = run_dir / "FINAL_MQR_LAUNCH_RESULT.csv"
        if not summary_path.exists():
            summary_path = run_dir / "08_domicile_launch_candidate.csv"
        summary = safe_read_csv(summary_path)
        row: Dict[str, Any] = summary.iloc[0].to_dict() if not summary.empty else {}
        relative = run_dir.relative_to(output_root)
        parts = relative.parts
        domicile = parts[0] if parts else str(state.get("domicile", ""))
        list_label = ""
        if len(parts) >= 3 and parts[1] == "lists":
            list_label = parts[2]

        found.append(
            {
                "run_key": str(relative),
                "domicile": str(row.get("domicile_requested") or state.get("domicile") or domicile),
                "list_label": str(row.get("input_list_label") or state.get("investment_list_label") or list_label),
                "mode": str(row.get("input_mode") or state.get("input_mode") or ""),
                "status": str(state.get("status", "unknown")),
                "updated": str(state.get("updated_at_local", "")),
                "quantitative_month": str(row.get("earliest_observed_quantitative_month", "")),
                "funds_at_launch": row.get("funds_at_earliest_quantitative_month", ""),
                "path": str(run_dir),
            }
        )

    found.sort(key=lambda item: item.get("updated", ""), reverse=True)
    return found


def list_downloadable_files(run_dir: Path) -> List[Path]:
    """Return clear professor-facing files; use legacy names only as fallback."""
    friendly = [
        "FINAL_MQR_LAUNCH_RESULT.csv",
        "FUNDS_WITH_MQR_AT_LAUNCH.csv",
        "MONTHLY_MQR_SCAN_SUMMARY.csv",
        "MQR_VALUES_AT_LAUNCH.csv",
        "ALL_RATING_ROWS_AT_LAUNCH.csv",
        "MQR_HISTORY_ALL_MONTHS.csv",
        "FIRST_MQR_DATE_BY_FUND.csv",
        "MQR_RESULTS_PROFESSOR_FORMAT.xlsx",
        "MQR_RESULTS_CLEAN_TABLE.xlsx",
        "MQR_RESULTS_CUSTOM_TEMPLATE.xlsx",
        "00_input_list_audit.csv",
        "02_investments.csv",
        "run_state.json",
    ]
    paths = [run_dir / name for name in friendly if (run_dir / name).exists()]

    # Older runs may not yet have friendly aliases. Show the old file only when
    # the corresponding friendly file is absent.
    fallbacks = [
        ("FINAL_MQR_LAUNCH_RESULT.csv", "08_domicile_launch_candidate.csv"),
        ("FUNDS_WITH_MQR_AT_LAUNCH.csv", "09_earliest_quantitative_month_funds.csv"),
        ("MONTHLY_MQR_SCAN_SUMMARY.csv", "10_month_scan_log.csv"),
        ("MQR_VALUES_AT_LAUNCH.csv", "11_first_quantitative_month_values.csv"),
        ("ALL_RATING_ROWS_AT_LAUNCH.csv", "12_first_observed_month_all_ratings.csv"),
        ("MQR_HISTORY_ALL_MONTHS.csv", "13_quantitative_history_long.csv"),
        ("FIRST_MQR_DATE_BY_FUND.csv", "14_first_quantitative_date_by_fund.csv"),
        ("MQR_RESULTS_PROFESSOR_FORMAT.xlsx", "15_medalist_history_comparable.xlsx"),
    ]
    for friendly_name, legacy_name in fallbacks:
        if not (run_dir / friendly_name).exists() and (run_dir / legacy_name).exists():
            paths.append(run_dir / legacy_name)

    return paths


def evidence_bundle_bytes(run_dir: Path) -> bytes:
    buffer = io.BytesIO()
    legacy_to_friendly = {
        "08_domicile_launch_candidate.csv": "FINAL_MQR_LAUNCH_RESULT.csv",
        "09_earliest_quantitative_month_funds.csv": "FUNDS_WITH_MQR_AT_LAUNCH.csv",
        "10_month_scan_log.csv": "MONTHLY_MQR_SCAN_SUMMARY.csv",
        "11_first_quantitative_month_values.csv": "MQR_VALUES_AT_LAUNCH.csv",
        "12_first_observed_month_all_ratings.csv": "ALL_RATING_ROWS_AT_LAUNCH.csv",
        "13_quantitative_history_long.csv": "MQR_HISTORY_ALL_MONTHS.csv",
        "14_first_quantitative_date_by_fund.csv": "FIRST_MQR_DATE_BY_FUND.csv",
        "15_medalist_history_comparable.xlsx": "MQR_RESULTS_PROFESSOR_FORMAT.xlsx",
    }
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in run_dir.rglob("*"):
            if not path.is_file():
                continue
            if any("batches" in part for part in path.relative_to(run_dir).parts):
                continue
            friendly_name = legacy_to_friendly.get(path.name)
            if friendly_name and (run_dir / friendly_name).exists():
                continue
            archive.write(path, arcname=path.relative_to(run_dir))
    return buffer.getvalue()


def file_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "text/csv"
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".json":
        return "application/json"
    return "application/octet-stream"


def format_file_size(path: Path) -> str:
    size = path.stat().st_size
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def get_progress_snapshot(run_dir: Path) -> Dict[str, Any]:
    state = safe_read_json(run_dir / "run_state.json")
    log_df = safe_read_csv(run_dir / "10_month_scan_log.csv")
    months_done = 0
    last_month = ""
    if not log_df.empty and "month" in log_df.columns:
        months = sorted(log_df["month"].dropna().astype(str).unique().tolist())
        months_done = len(months)
        last_month = months[-1] if months else ""

    type_batches = len(list((run_dir / "10_month_scan_type_batches").rglob("batch_*.csv"))) if (run_dir / "10_month_scan_type_batches").exists() else 0
    value_batches = len(list((run_dir / "10_month_scan_value_for_quant_batches").rglob("batch_*.csv"))) if (run_dir / "10_month_scan_value_for_quant_batches").exists() else 0
    return {
        "status": state.get("status", "unknown"),
        "months_completed": months_done,
        "last_completed_month": last_month,
        "type_batches_saved": type_batches,
        "value_batches_saved": value_batches,
        "investment_ids_used": state.get("investment_ids_used", ""),
        "updated_at_local": state.get("updated_at_local", ""),
    }
