# main.py
import os
import re
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup

# Optional parsers
try:
    import docx
except Exception:
    docx = None

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except Exception:
    pdf_extract_text = None

# Optional Redis
try:
    import redis.asyncio as aioredis
except Exception:
    aioredis = None

app = FastAPI(title="Verificador Jurídico Colombiano - UI")

# Mount static and templates
BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

CURRENT_YEAR = datetime.now().year

# Concurrency control
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", "4"))
semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# Cache TTL
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(60 * 60)))

# Redis optional
REDIS_URL = os.getenv("REDIS_URL", None)
redis_client = None
if REDIS_URL and aioredis:
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

# Regex patterns (named groups)
patterns = {
    "corte_constitucional": re.compile(
        r"\b(?P<prefix>SU|T|C|AUTO|AUTO\w*)[-\s]?0*(?P<number>\d{1,6})(?:[\/\-\s]?(?P<year>\d{2,4}))?\b",
        re.IGNORECASE
    ),
    "ley": re.compile(
        r"\bLey\s+0*(?P<number>\d{1,6})\s+de\s+(?P<year>19\d{2}|20\d{2})\b",
        re.IGNORECASE
    ),
    "decreto": re.compile(
        r"\bDecreto\s+0*(?P<number>\d{1,6})\s+de\s+(?P<year>19\d{2}|20\d{2})\b",
        re.IGNORECASE
    ),
    "csj": re.compile(
        r"\b(?P<prefix>SL|SC|STC|STL|CSJ)[-]?(?P<number>\d{1,6})[-\/]?(?P<year>19\d{2}|20\d{2})\b",
        re.IGNORECASE
    ),
}

# -------------------------
# Cache helpers
# -------------------------
async def cache_get(key: str) -> Optional[Any]:
    if redis_client:
        try:
            return await redis_client.get(key)
        except Exception:
            return None
    # in-memory fallback
    entry = IN_MEMORY_CACHE.get(key)
    if not entry:
        return None
    expiry, value = entry
    if datetime.utcnow() > expiry:
        del IN_MEMORY_CACHE[key]
        return None
    return value

async def cache_set(key: str, value: Any, ttl_seconds: Optional[int] = None):
    ttl = ttl_seconds or CACHE_TTL_SECONDS
    if redis_client:
        try:
            await redis_client.set(key, value, ex=ttl)
            return
        except Exception:
            pass
    IN_MEMORY_CACHE[key] = (datetime.utcnow() + timedelta(seconds=ttl), value)

IN_MEMORY_CACHE: Dict[str, Any] = {}

# -------------------------
# Normalización y reglas
# -------------------------
def normalize_match(category: str, match: re.Match) -> Dict[str, Any]:
    groups = match.groupdict()
    prefix = groups.get("prefix") or ""
    number = groups.get("number") or ""
    year = groups.get("year") or ""
    if year and len(year) == 2:
        year_int = int(year)
        year = f"20{year}" if year_int <= (CURRENT_YEAR % 100) else f"19{year}"
    normalized = {
        "category": category,
        "raw": match.group(0),
        "prefix": prefix.upper() if prefix else None,
        "number": number.lstrip("0") if number else None,
        "year": year if year else None
    }
    return normalized

def evaluate_reference_normalized(normalized: Dict[str, Any]) -> List[Dict[str, str]]:
    findings = []
    year = normalized.get("year")
    if year:
        try:
            y = int(year)
        except Exception:
            y = None
        if y:
            if y > CURRENT_YEAR:
                findings.append({"severity": "alta", "issue": "Año futuro incompatible"})
            if normalized.get("category") == "corte_constitucional" and y < 1992:
                findings.append({"severity": "alta", "issue": "Corte Constitucional inexistente para esa fecha"})
    return findings

# -------------------------
# HTTP fetch with semaphore and cache
# -------------------------
async def fetch_with_semaphore(url: str, params: Dict[str, str] = None, headers: Dict[str, str] = None, timeout: int = 10) -> str:
    cache_key = f"GET:{url}:{params}"
    cached = await cache_get(cache_key)
    if cached:
        return cached

    async with semaphore:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            text = resp.text
            await cache_set(cache_key, text)
            return text

# -------------------------
# Verificación: Corte Constitucional
# -------------------------
async def search_corte_constitucional(normalized: Dict[str, Any]) -> Dict[str, Any]:
    base_search_url = "https://www.corteconstitucional.gov.co/relatoria/buscador.php"
    prefix = normalized.get("prefix") or ""
    number = normalized.get("number") or ""
    year = normalized.get("year") or ""
    q_parts = [p for p in (prefix, number, year) if p]
    query = " ".join(q_parts).strip()
    if not query:
        return {"status": "sin_parametros", "confidence": 0}

    try:
        html = await fetch_with_semaphore(base_search_url, params={"q": query}, headers={"User-Agent": "verificador/1.0"})
    except Exception as e:
        return {"status": "error", "detail": str(e), "confidence": 0}

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()

    confidence = 0
    if number and number.lower() in text:
        confidence += 60
    if year and year.lower() in text:
        confidence += 30

    first_link = soup.select_one("a")
    found_url = None
    status = "sin_coincidencia"
    if first_link and first_link.get("href"):
        found_url = first_link.get("href")
        if found_url.startswith("/"):
            found_url = "https://www.corteconstitucional.gov.co" + found_url
        status = "verificado" if confidence >= 70 else "coincidencia_parcial"
    else:
        if confidence >= 80:
            status = "coincidencia_parcial"

    return {"status": status, "confidence": confidence, "source": found_url}

# -------------------------
# Verificación: SUIN-Juriscol (leyes y decretos)
# -------------------------
async def search_suin_juriscol(normalized: Dict[str, Any]) -> Dict[str, Any]:
    base_search_url = "https://www.suin-juriscol.gov.co/buscador"
    number = normalized.get("number") or ""
    year = normalized.get("year") or ""
    q_parts = [p for p in (number, year) if p]
    query = " ".join(q_parts).strip()
    if not query:
        return {"status": "sin_parametros", "confidence": 0}

    try:
        html = await fetch_with_semaphore(base_search_url, params={"q": query}, headers={"User-Agent": "verificador/1.0"})
    except Exception as e:
        return {"status": "error", "detail": str(e), "confidence": 0}

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()

    confidence = 0
    if number and number.lower() in text:
        confidence += 60
    if year and year.lower() in text:
        confidence += 30

    first_link = soup.select_one("a")
    found_url = None
    status = "sin_coincidencia"
    if first_link and first_link.get("href"):
        found_url = first_link.get("href")
        if found_url.startswith("/"):
            found_url = "https://www.suin-juriscol.gov.co" + found_url
        status = "verificado" if confidence >= 70 else "coincidencia_parcial"
    else:
        if confidence >= 80:
            status = "coincidencia_parcial"

    return {"status": status, "confidence": confidence, "source": found_url}

# -------------------------
# Router de verificación
# -------------------------
async def verify_reference(category: str, normalized: Dict[str, Any]) -> Dict[str, Any]:
    if category == "corte_constitucional":
        return await search_corte_constitucional(normalized)
    if category in ("ley", "decreto"):
        return await search_suin_juriscol(normalized)
    return {"status": "no_fuente", "confidence": 0}

# -------------------------
# Texto desde archivo
# -------------------------
async def extract_text_from_upload(upload: UploadFile) -> str:
    filename = upload.filename or ""
    ext = filename.split(".")[-1].lower()
    content = await upload.read()
    # txt
    if ext in ("txt",):
        return content.decode(errors="ignore")
    # docx
    if ext in ("docx",) and docx:
        from io import BytesIO
        doc = docx.Document(BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs]
        return "\n".join(paragraphs)
    # pdf
    if ext in ("pdf",) and pdf_extract_text:
        from io import BytesIO
        # write to temp file because pdfminer expects a file path or file-like
        tmp_path = f"/tmp/{filename}"
        with open(tmp_path, "wb") as f:
            f.write(content)
        try:
            text = pdf_extract_text(tmp_path)
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return text or ""
    # fallback: try decode
    try:
        return content.decode(errors="ignore")
    except Exception:
        return ""

# -------------------------
# Endpoint UI: index
# -------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "docx_available": bool(docx), "pdf_available": bool(pdf_extract_text)})

# -------------------------
# Endpoint UI: submit (form)
# -------------------------
@app.post("/submit", response_class=HTMLResponse)
async def submit(request: Request, text_input: str = Form(""), file: Optional[UploadFile] = File(None)):
    # Obtener texto: prioridad archivo > campo de texto
    text = ""
    if file:
        text = await extract_text_from_upload(file)
    else:
        text = text_input or ""

    # Ejecutar auditoría (misma lógica que el endpoint API)
    extracted = []
    for category, pattern in patterns.items():
        for match in pattern.finditer(text):
            normalized = normalize_match(category, match)
            findings = evaluate_reference_normalized(normalized)
            try:
                verification = await verify_reference(category, normalized)
            except Exception as e:
                verification = {"status": "error", "detail": str(e), "confidence": 0}
            extracted.append({
                "reference_raw": normalized["raw"],
                "normalized": normalized,
                "findings": findings,
                "verification": verification
            })

    # Renderizar reporte
    return templates.TemplateResponse("report.html", {"request": request, "results": extracted, "raw_text": text})

# -------------------------
# API endpoint (JSON) - opcional
# -------------------------
class AuditRequest(BaseModel):
    text: str

@app.post("/api/audit")
async def api_audit(req: AuditRequest):
    text = req.text or ""
    extracted = []
    for category, pattern in patterns.items():
        for match in pattern.finditer(text):
            normalized = normalize_match(category, match)
            findings = evaluate_reference_normalized(normalized)
            try:
                verification = await verify_reference(category, normalized)
            except Exception as e:
                verification = {"status": "error", "detail": str(e), "confidence": 0}
            extracted.append({
                "reference_raw": normalized["raw"],
                "normalized": normalized,
                "findings": findings,
                "verification": verification
            })
    return {"results": extracted}
