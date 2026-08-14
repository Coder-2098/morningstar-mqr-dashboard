# Cloud deployment

This build is prepared for Streamlit Community Cloud + private Supabase Storage.

- Streamlit hosts the dashboard.
- Supabase stores `inputs/` and `output/` persistently.
- Every CSV/JSON checkpoint is uploaded as it is saved.
- Final Excel files and uploaded input lists are also persisted.
- The app restores shared files from Supabase when a new session starts.
- Morningstar authentication runs with server-side headless Chromium when `[app] hosted = true`.

Do not commit `.streamlit/secrets.toml`, `output/`, `inputs/`, `.mstar_token_state.json`, or private reference workbooks to GitHub.
