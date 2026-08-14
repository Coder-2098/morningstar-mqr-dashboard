# Morningstar MQR Dashboard Guide

## Open the dashboard

### macOS
Double-click `launch_dashboard.command`.

The first launch creates `.venv`, installs the required packages and Chromium, and then opens the dashboard in a browser. Later launches reuse the installed environment.

### Windows
Double-click `start_dashboard_windows.bat`.

### Linux or Terminal fallback
Run `./start_dashboard.sh`.

## Daily workflow

1. In the left sidebar, enter the Morningstar username/email and password and click **Connect / refresh token**.
2. If institutional MFA appears in the opened browser, complete it. The dashboard automatically clicks **Copy Authentication Token** and captures the token.
3. In **Run or resume**, choose:
   - **Country universe** for FO mutual funds + FE ETFs, or
   - **Exact saved list / comparison** for any uploaded Morningstar list such as AFS, Restricted, or FCPE.
4. Click **Run / resume pipeline**.
5. If the daily limit is reached, return after the reset, reconnect, and click the same button. Saved batch files are reused.
6. In **Progress and results**, download the summary, evidence files, comparable workbook, or professor evidence bundle.

## Defaults

- Universes: FO + FE
- Quantitative scan: 2017-01-01 through 2024-12-31
- Human/Analyst scan: optional and off
- Exact-list metadata enrichment: off because it consumes substantial cell quota
- Exact-list full history: on in the dashboard so per-fund dates and the comparison workbook can be created

## Security

The dashboard does not write the Morningstar password or raw token to disk. The token-state file stores only a token hash and timing information.


### Token capture behavior

When the browser displays “The authentication token is copied to the clipboard,” the dashboard captures that exact value automatically. Analytics Lab may use a standard base64url-signed JWT. The dashboard does not decide that a token is valid from its shape alone; it verifies it with a small live Morningstar request before showing **Connected**.

---

## Field and option guide

### Search for legacy/pre-Medalist quantitative fields

Leave this off for the main project. The known fields are MMR00 and MMR08. Turn it on only when investigating whether an older Morningstar field ID contains pre-Medalist quantitative history.

### Optional Human/Analyst scan

This is a separate supporting analysis. The dashboard reads MMR08 and treats values containing Analyst, Human, Manager Research, or Qualitative as human/analyst. It then requires a nonblank MMR00 value. Because human ratings can predate MQR by many years, this scan has its own date range and is off by default.

### Testing only: limit investment IDs

- `0`: use every ID; complete result.
- Any positive number: use only the first N IDs; technical smoke test only.

Do not present a capped result as a country launch result.

### Output template

For exact-list full-history runs:

1. **Professor format (default)** — same four-row-per-fund Batch layout as the original workbook.
2. **Clean table format** — standard tabular workbook with headers.
3. **Upload custom Excel template** — preserves the uploaded workbook and adds generated output sheets. A `Batch_Template` sheet is used as a formatting seed when present.

### Dated Name columns

Columns such as `Name 2024-01-31` are historical snapshots of the investment name. They are not rating dates. New FO/FE universe files no longer request them, and reused old files are cleaned automatically.
