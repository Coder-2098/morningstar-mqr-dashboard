# Morningstar MQR Research Dashboard

This project identifies the first country-level month when Morningstar Quantitative Rating appeared for mutual funds and ETFs. The primary operating interface is now a Streamlit dashboard; a professor or future researcher does not need to construct Terminal commands.

## Dashboard-only workflow

- **macOS:** double-click `launch_dashboard.command`
- **Windows:** double-click `start_dashboard_windows.bat`
- **Linux:** run `./start_dashboard.sh`

The launcher creates the virtual environment on first use, installs dependencies and Chromium, and opens the dashboard. From the dashboard, users can connect to Analytics Lab, upload exact saved lists, run/resume country scans, monitor checkpoints, and download evidence bundles. See `DASHBOARD_GUIDE.md`.

### Research defaults

- FO mutual funds and FE ETFs
- Quantitative scan: 2017-01-01 through 2024-12-31
- Human/Analyst scan: optional and off
- Exact-list metadata enrichment: optional and off because it consumes substantial cell quota
- Exact saved-list mode is generic across AFS, Restricted, FCPE, or any other list label

---

# Morningstar MQR Launch-Date Pipeline

This project identifies the first month when Morningstar Quantitative Rating (MQR) appears for funds in a country.

The default research run uses:

- `FO`: open-end / mutual funds
- `FE`: ETFs
- start date: `2017-01-01`
- end date: `2024-12-31`
- `MMR08`: rating type/source
- `MMR00`: Medalist rating value
- quantitative-only output by default

A valid MQR row requires both:

1. `MMR08` indicates `Quantitative`
2. `MMR00` has a nonblank rating such as Gold, Silver, Bronze, Neutral, or Negative

Human/Analyst history is optional and is not scanned by default.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

## Normal country run

```bash
python3 run_mqr.py --domicile aus --scan-batch-size 5000
```

This uses the default `FO FE` universes and the default 2017-01 through 2024-12 date range.

## Exact saved-list mode: generic, not country-specific

Use `--investment-list-file` with **any Morningstar saved list or export** containing investment IDs. It is not limited to South Korea or AFS. The file can represent AFS, Restricted, FCPE, a custom professor list, or any future list name.

Supported formats:

- `.xlsx`, `.xls`, `.xlsm`
- `.csv`, `.tsv`, `.txt`

The importer automatically finds common Morningstar ID columns, including `Id`, `SecId`, `Morningstar SecId`, `Investment ID`, and `MSTAR Function`. All workbook sheets are included by default.

Example using the South Korea sample list:

```bash
python3 run_mqr.py \
  --domicile korea \
  --investment-list-file "$HOME/Downloads/South_Korea/Raw Files/SK_AFS_Base.xlsx" \
  --start-date 2017-09 \
  --scan-batch-size 5000
```

The input file name becomes the generic list label automatically. To use a cleaner label:

```bash
python3 run_mqr.py \
  --domicile korea \
  --investment-list-file "$HOME/Downloads/South_Korea/Raw Files/SK_AFS_Base.xlsx" \
  --investment-list-label "AFS" \
  --start-date 2017-09 \
  --scan-batch-size 5000
```

To use only selected workbook sheets:

```bash
--investment-list-sheet Sheet1 Sheet3
```

Exact-list outputs are separated from broad country outputs:

```text
output/<country>/lists/<generic-list-label>/
```

This prevents an exact AFS/custom-list run from overwriting the broad FO/FE country run.

## Professor-comparable full history

The normal run stops after it finds the first quantitative month. Use `--full-history` only when you need every fund's first quantitative date and a workbook comparable to the sample Morningstar Medalist sheet.

```bash
python3 run_mqr.py \
  --domicile korea \
  --investment-list-file "$HOME/Downloads/South_Korea/Raw Files/SK_AFS_Base.xlsx" \
  --investment-list-label "AFS" \
  --start-date 2017-09 \
  --end-date 2024-12 \
  --full-history \
  --scan-batch-size 5000
```

`--full-history` is resumable but can require multiple daily quota windows for large lists.

It produces:

```text
00_input_list_audit.csv
02_investments_from_input_list.csv
02_investments.csv
02b_investments_used_for_pull.csv
08_domicile_launch_candidate.csv
09_earliest_quantitative_month_funds.csv
10_month_scan_log.csv
13_quantitative_history_long.csv
14_first_quantitative_date_by_fund.csv
15_medalist_history_comparable.xlsx
```

The comparable workbook contains:

- the exact input investment IDs
- the generic input-list label
- source file, source sheet, and source row
- Morningstar name and domicile metadata when available
- two rows per fund:
  - `Historical_Morningstar_Medalist_Rating|MMR00`
  - `Morningstar_Medalist_Rating_Type|MMR08`
- monthly `YYYY-MM` columns
- a `First_Quant_Date` sheet with one row for every input fund
- blank first dates and `Not observed in requested range` for funds that never receive a quantitative rating in the selected period

To save quota, skip metadata enrichment with:

```bash
--no-enrich-list-metadata
```

To create CSV outputs without the comparable Excel workbook:

```bash
--no-comparable-workbook
```

## Optional Human/Analyst scan

Human/Analyst history is supporting analysis only and is off by default.

```bash
python3 run_mqr.py \
  --domicile aus \
  --include-human-scan \
  --human-start-date 2008-01 \
  --human-end-date 2024-12
```

## Resume after the daily limit

Do not delete the output directory. After the quota reset, rerun the exact same command. Existing investment files and completed monthly batches are reused.

## Authentication

The token manager checks the current `MD_AUTH_TOKEN`, its JWT expiry, and the stored 24-hour state. If a fresh token is needed, the normal command can request Morningstar credentials and try the Analytics Lab browser flow.

Do not pass `--no-token-prompt` when credential-driven refresh is desired.

```bash
python3 run_mqr.py --domicile aus --scan-batch-size 5000
```


### Token capture behavior

When the browser displays “The authentication token is copied to the clipboard,” the dashboard captures that exact value automatically. Analytics Lab may use a standard base64url-signed JWT. The dashboard does not decide that a token is valid from its shape alone; it verifies it with a small live Morningstar request before showing **Connected**.

---

## August 2026 clarification and output-template update

### Research fields

- `MMR00` is the historical Morningstar Medalist Rating value: Gold, Silver, Bronze, Neutral, or Negative.
- `MMR08` is the Medalist Rating type/source.
- The required MQR observation is counted only when `MMR08` is Quantitative **and** `MMR00` is nonblank.
- The optional Human/Analyst scan classifies MMR08 values containing Analyst, Human, Manager Research, or Qualitative as human/analyst. It is off by default.

### What legacy/pre-Medalist discovery means

The optional legacy-field search does not mean old, stale, or expired ratings. It scans Morningstar dataset metadata for older quantitative field IDs that may have been used before the current Medalist fields `MMR00` and `MMR08`. It is an advanced fallback and is now off by default.

### Testing-only investment-ID limit

A nonzero test limit uses only the first N investment IDs. This is useful to verify authentication, datapoints, and file creation without spending the quota required for a complete run. A capped test is not a research result. Use zero for all IDs.

### Auto datapoint settings

The dashboard uses explicit dates and does not use auto start-date resolution. The CLI accepts `--start-date auto`; only then does the pipeline call Morningstar datapoint settings and save `00_datapoint_settings.csv`. Those settings describe field configuration and availability in the current environment. They do not prove a country's historical launch date.

### Why old FO/FE files contained `Name YYYY-MM-DD` columns

Older country-universe requests included the historical Name datapoint. Morningstar expanded it into one name column for many monthly dates. Those columns are name snapshots, not funds and not MQR observations. They are unnecessary for the launch-date analysis.

New country runs request only the domicile datapoint during universe discovery. When an older FO/FE file is reused, the pipeline keeps at most one latest nonblank `Name` value and removes the dated Name columns automatically.

### Excel output templates

For full-history exact-list runs, the default Excel output is now **Professor format**. It reproduces the original structure:

- `Batch_1`, `Batch_2`, etc.
- 4,000 investment IDs per Batch sheet, matching the original six-sheet partition for a 22,338-ID list.
- Four rows per investment ID:
  1. MMR00 ID/datapoint/date row
  2. MMR00 values row
  3. MMR08 ID/datapoint/date row
  4. MMR08 values row

The output remains `15_medalist_history_comparable.xlsx`.

Other choices:

- **Clean table format:** normal headers, Summary, First_Quant_Date, and two rows per ID.
- **Custom Excel template:** preserves the uploaded workbook and adds `Generated_Summary`, `First_Quant_Date`, and generated `Batch_*` sheets. If the uploaded workbook contains `Batch_Template`, its styles and column widths are copied to each generated Batch sheet. Supported placeholders include `{{COUNTRY}}`, `{{LIST_LABEL}}`, `{{FIRST_QUANT_MONTH}}`, `{{FUNDS_AT_LAUNCH}}`, and `{{TOTAL_INPUT_IDS}}`.

Changing the output template does not call Morningstar again. It reformats already cached history.
