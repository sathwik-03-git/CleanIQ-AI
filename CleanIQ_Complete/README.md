# CleanIQ — Data Quality Studio

A deployable CSV/Excel cleaning website with dataset profiling, manual cleaning operations, AI recommendations, preview, and CSV/XLSX export.

## Features
- Upload CSV, XLSX, and XLSM files (default 250 MB upload limit).
- Inspect row/column counts, missing cells, exact duplicates, column types, and potential numeric outliers.
- Manual operations: remove duplicates, fill numeric blanks with median, fill blanks with mode, standardize text whitespace, drop rows/columns with missing values, and detect outliers without changing them.
- Recommendation endpoint: uses Gemini when `GEMINI_API_KEY` works; otherwise generates local profile-based recommendations, so the cleaning workflow does not depend on Gemini.
- Review checkboxes before applying operations.
- Download cleaned CSV or Excel.
- Temporary in-memory session data and temporary uploaded files; data is not a durable database.

## Run locally (Windows PowerShell)
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app:app --reload
```
Open http://127.0.0.1:8000

Set a working Gemini key in your local `.env` only if you want Gemini-powered text generation. The app has a local recommendation fallback if Gemini is unavailable. Never commit `.env`.

## Deploy
1. Push this folder to a GitHub repository.
2. In Render, create a new Web Service and connect the repository.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
5. Add `GEMINI_API_KEY` in Render's Environment settings (optional) and set `MAX_UPLOAD_MB` if needed.
6. Deploy; Render will give you a public HTTPS URL.

## Important scaling notes
This version streams incoming upload bytes to disk, but pandas loads the selected dataset into memory for profiling and cleaning. Actual capacity therefore depends on the server's RAM and the dataset's width/dtypes; a 250 MB upload limit is not a promise that every 250 MB file fits into memory. For very large production workloads, use a memory-optimized worker (for example Polars/Dask), background jobs, persistent object storage, authentication, and per-user quotas. Free hosting plans may sleep or impose memory limits. Do not use this demo deployment for confidential data without access controls and a reviewed privacy design.
