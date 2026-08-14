## 2026-07-30 — JupyterLab menu automation fix

- Automatically opens the JupyterLab **Analytics Lab** top menu and clicks **Copy Authentication Token**.
- Handles Analytics Lab opening in a second browser tab.
- Reduces the dashboard connection timeout to 120 seconds and fails clearly instead of appearing to hang indefinitely.

# Dashboard release

- Added a Streamlit dashboard for authentication, country runs, exact-list uploads, resume, progress tracking, results review, and downloads.
- Added macOS, Windows, and shell launchers that bootstrap the virtual environment and browser automatically.
- Added automatic Analytics Lab token capture after username/password login; a visible browser supports institutional MFA while token copying remains automatic.
- Added professor evidence-bundle downloads.
- Changed exact-list metadata enrichment to off by default to protect the Morningstar daily cell quota.
- Kept FO + FE, 2017-01-01 through 2024-12-31, and quantitative-only as the research defaults.

# Changelog

## Generic exact-list and comparison release

- Added generic `--investment-list-file` support for Excel/CSV saved lists.
- Added automatic Morningstar ID-column detection, including `MSTAR Function`.
- Added optional `--investment-list-sheet` and `--investment-list-label`.
- Exact-list runs now use `output/<country>/lists/<label>/` and never overwrite broad country runs.
- Added checkpointed input-list metadata enrichment.
- Added `--full-history` for all requested months rather than stopping at country launch.
- Added `13_quantitative_history_long.csv`.
- Added `14_first_quantitative_date_by_fund.csv` with every input fund, including never-observed funds.
- Added `15_medalist_history_comparable.xlsx` with two rows per fund for MMR00 and MMR08.
- Added `--no-comparable-workbook` and `--no-enrich-list-metadata`.
- Added support for `YYYY-MM` date arguments such as `--start-date 2017-09`.
- Human/Analyst scan remains optional and off by default.
- Removed a duplicate MMR00 request that was previously used only to build the combined evidence file.

## 2026-07-30 — Analytics Lab clipboard/JWT fix

- Fixed a loop where the JupyterLab menu successfully copied the authentication token but the dashboard remained on Connecting.
- Accepts the standard base64url-signed JWT copied by Analytics Lab as a candidate token; live Morningstar validation remains the source of truth.
- Added direct browser clipboard interception plus browser and macOS clipboard fallbacks.
- The dashboard still reports Connected only after a successful live `morningstar_data` request (or an authenticated daily-quota response).

## 2026-08-05

- Made Professor-format Excel output the default for full-history exact-list runs.
- Added Clean and Custom Excel output-template modes.
- Added custom-template placeholders and optional `Batch_Template` style reuse.
- Corrected the misleading clean-workbook summary count by separating total input IDs from IDs with quantitative history.
- Turned legacy/pre-Medalist datapoint discovery off by default and clarified its meaning.
- Clarified the test-only investment-ID cap in the dashboard.
- Expanded Human/Analyst MMR08 classification to include Qualitative labels.
- Removed the historical Name datapoint from new FO/FE universe discovery.
- Added automatic cleanup of old `Name YYYY-MM-DD` columns when existing FO/FE files are reused.
