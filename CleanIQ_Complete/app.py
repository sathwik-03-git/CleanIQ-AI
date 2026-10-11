import os, uuid, tempfile, json, re
from pathlib import Path
from typing import Any
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).parent
DATA_DIR = Path(tempfile.gettempdir()) / "cleaniq_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "250"))
app = FastAPI(title="CleanIQ", version="2.0.0")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
SESSIONS: dict[str, dict[str, Any]] = {}

def safe_read(path: Path, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".csv":
            # Low-memory parser reduces peak parsing memory for large CSVs.
            return pd.read_csv(path, low_memory=False)
        if suffix in (".xlsx", ".xlsm"):
            return pd.read_excel(path, engine="openpyxl")
    except Exception as exc:
        raise HTTPException(400, f"Could not read this file: {exc}")
    raise HTTPException(400, "Upload a CSV or XLSX/XLSM file.")

def json_value(v):
    if pd.isna(v):
        return None
    if hasattr(v, "item"):
        try: return v.item()
        except Exception: pass
    if hasattr(v, "isoformat"):
        try: return v.isoformat()
        except Exception: pass
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)

def analysis(df: pd.DataFrame):
    missing = {str(c): int(df[c].isna().sum()) for c in df.columns}
    duplicates = int(df.duplicated().sum()) if len(df) else 0
    types = {str(c): str(df[c].dtype) for c in df.columns}
    outliers = {}
    for c in df.select_dtypes(include="number").columns:
        s = df[c].dropna()
        if len(s) >= 4:
            q1, q3 = s.quantile(.25), s.quantile(.75)
            count = int(((s < q1 - 1.5*(q3-q1)) | (s > q3 + 1.5*(q3-q1))).sum())
            outliers[str(c)] = count
    return {"rows": int(len(df)), "columns": int(len(df.columns)),
            "duplicate_rows": duplicates, "missing_values": int(df.isna().sum().sum()),
            "missing_by_column": missing, "data_types": types,
            "potential_outliers_by_column": outliers, "column_names": [str(c) for c in df.columns]}

def preview(df, n=10):
    return [{str(k): json_value(v) for k,v in row.items()} for row in df.head(n).to_dict(orient="records")]

def get_session(file_id):
    item = SESSIONS.get(file_id)
    if not item:
        raise HTTPException(404, "Dataset session not found. Upload the file again.")
    return item

@app.get("/", response_class=HTMLResponse)
def home():
    return (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")

@app.get("/health")
def health():
    return {"status":"ok", "service":"CleanIQ"}

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    name = Path(file.filename or "dataset.csv").name
    if Path(name).suffix.lower() not in (".csv", ".xlsx", ".xlsm"):
        raise HTTPException(400, "Supported formats: CSV, XLSX, XLSM.")
    file_id = uuid.uuid4().hex
    dest = DATA_DIR / f"{file_id}{Path(name).suffix.lower()}"
    size = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"File exceeds the {MAX_UPLOAD_MB} MB upload limit.")
                out.write(chunk)
        df = safe_read(dest, name)
        if len(df.columns) == 0:
            raise HTTPException(400, "The file has no columns.")
        SESSIONS[file_id] = {"path": dest, "filename": name, "df": df, "original_analysis": analysis(df), "cleaned": False}
        a = analysis(df)
        return {"file_id": file_id, "filename": name, "size_bytes": size, "analysis": a, "preview": preview(df)}
    except Exception:
        if file_id not in SESSIONS:
            dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

@app.post("/api/clean/{file_id}")
async def clean(file_id: str, payload: dict):
    item = get_session(file_id)
    df = item["df"].copy()
    operations = payload.get("operations", [])
    allowed = {"remove_duplicates","standardize_text","fill_missing_median","fill_missing_mode","drop_rows_with_missing","drop_columns_with_missing","trim_whitespace","detect_outliers"}
    if not isinstance(operations, list) or not operations or any(op not in allowed for op in operations):
        raise HTTPException(400, "Select at least one valid cleaning operation.")
    before = analysis(df)
    report = []
    for op in operations:
        rows_before = len(df)
        if op == "remove_duplicates":
            df = df.drop_duplicates()
            report.append({"operation":op,"affected":rows_before-len(df),"description":f"Removed {rows_before-len(df):,} exact duplicate rows."})
        elif op == "standardize_text":
            cols = df.select_dtypes(include=["object","string"]).columns
            for c in cols:
                df[c] = df[c].map(lambda x: re.sub(r"\s+", " ", x).strip() if isinstance(x,str) else x)
            report.append({"operation":op,"affected":len(cols),"description":f"Standardized whitespace in {len(cols)} text columns."})
        elif op == "trim_whitespace":
            cols = df.select_dtypes(include=["object","string"]).columns
            for c in cols:
                df[c] = df[c].map(lambda x: x.strip() if isinstance(x,str) else x)
            report.append({"operation":op,"affected":len(cols),"description":f"Trimmed text in {len(cols)} columns."})
        elif op == "fill_missing_median":
            cols = df.select_dtypes(include="number").columns
            total = 0
            for c in cols:
                n = int(df[c].isna().sum())
                if n: df[c] = df[c].fillna(df[c].median()); total += n
            report.append({"operation":op,"affected":total,"description":f"Filled {total:,} numeric missing cells with column medians."})
        elif op == "fill_missing_mode":
            total = 0
            for c in df.columns:
                if df[c].isna().any():
                    modes = df[c].mode(dropna=True)
                    if not modes.empty:
                        n = int(df[c].isna().sum()); df[c] = df[c].fillna(modes.iloc[0]); total += n
            report.append({"operation":op,"affected":total,"description":f"Filled {total:,} missing cells with column modes where available."})
        elif op == "drop_rows_with_missing":
            df = df.dropna()
            report.append({"operation":op,"affected":rows_before-len(df),"description":f"Removed {rows_before-len(df):,} rows containing missing values."})
        elif op == "drop_columns_with_missing":
            cols = [c for c in df.columns if df[c].isna().any()]
            df = df.drop(columns=cols)
            report.append({"operation":op,"affected":len(cols),"description":f"Removed {len(cols)} columns containing missing values."})
        elif op == "detect_outliers":
            found = {}
            for c in df.select_dtypes(include="number").columns:
                s=df[c].dropna()
                if len(s)>=4:
                    q1,q3=s.quantile(.25),s.quantile(.75); n=int(((s<q1-1.5*(q3-q1))|(s>q3+1.5*(q3-q1))).sum())
                    if n: found[str(c)] = n
            report.append({"operation":op,"affected":sum(found.values()),"description":f"Flagged {sum(found.values()):,} potential outliers; values were not changed."})
    item["df"] = df
    item["cleaned"] = True
    item["report"] = report
    return {"analysis_before":before,"analysis_after":analysis(df),"preview":preview(df),"report":report}

@app.post("/api/ai/suggest")
async def suggest(payload: dict):
    file_id = payload.get("file_id")
    instruction = str(payload.get("instruction","")).strip()
    if not instruction:
        raise HTTPException(400, "Enter a cleaning goal.")
    item = get_session(file_id)
    a = analysis(item["df"])
    # Optional Gemini provider. The app remains useful without a key/provider access.
    key = os.getenv("GEMINI_API_KEY")
    if key:
        try:
            from google import genai
            client = genai.Client(api_key=key)
            prompt = ("You are a cautious data quality assistant. Return ONLY JSON with keys explanation and operations. "
                      "Allowed operations: remove_duplicates, standardize_text, fill_missing_median, fill_missing_mode, "
                      "drop_rows_with_missing, drop_columns_with_missing, trim_whitespace, detect_outliers. "
                      "Never invent operations. Dataset profile: "+json.dumps(a)+" User goal: "+instruction)
            response = client.models.generate_content(model=os.getenv("GEMINI_MODEL","gemini-2.5-flash"), contents=prompt)
            raw = response.text.strip().replace("```json","").replace("```","").strip()
            obj = json.loads(raw)
            ops = [x for x in obj.get("operations",[]) if x in {"remove_duplicates","standardize_text","fill_missing_median","fill_missing_mode","drop_rows_with_missing","drop_columns_with_missing","trim_whitespace","detect_outliers"}]
            if ops: return {"explanation":obj.get("explanation","Recommendations generated."),"operations":ops,"provider":"Gemini"}
        except Exception:
            pass
    # Deterministic profile-based fallback is always available if Gemini is unavailable.
    ops=[]
    if a["duplicate_rows"]: ops.append("remove_duplicates")
    if a["missing_values"]:
        if any(a["missing_by_column"].get(c,0) and ("int" in a["data_types"].get(c,"").lower() or "float" in a["data_types"].get(c,"").lower()) for c in a["column_names"]):
            ops.append("fill_missing_median")
        ops.append("fill_missing_mode")
    if any(a["potential_outliers_by_column"].values()): ops.append("detect_outliers")
    if not ops: ops=["standardize_text","trim_whitespace"]
    goal = " ".join(instruction.lower().split())
    if "duplicate" in goal: ops.insert(0,"remove_duplicates")
    if "median" in goal and "fill_missing_median" not in ops: ops.append("fill_missing_median")
    if "drop" in goal and "missing" in goal: ops.append("drop_rows_with_missing")
    # preserve order, unique ops
    ops=list(dict.fromkeys(ops))
    return {"explanation":"Profile-based recommendations are ready. Gemini is optional; if its API is unavailable, CleanIQ uses the dataset quality profile to recommend safe, reviewable operations.","operations":ops,"provider":"Local profile engine"}

@app.get("/api/download/{file_id}")
def download(file_id: str, format: str = "csv"):
    item = get_session(file_id)
    df = item["df"]
    fmt = format.lower()
    if fmt == "csv":
        out = DATA_DIR / f"{file_id}_cleaned.csv"
        df.to_csv(out,index=False)
        media = "text/csv"
    elif fmt in ("xlsx","excel"):
        out = DATA_DIR / f"{file_id}_cleaned.xlsx"
        try: df.to_excel(out,index=False,engine="openpyxl")
        except Exception as exc: raise HTTPException(500,f"Excel export failed: {exc}")
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        raise HTTPException(400,"Choose csv or xlsx.")
    return FileResponse(out, media_type=media, filename=f"{Path(item['filename']).stem}_cleaned.{ 'csv' if fmt=='csv' else 'xlsx'}")

@app.on_event("shutdown")
def cleanup():
    for item in SESSIONS.values():
        try: item["path"].unlink(missing_ok=True)
        except Exception: pass
