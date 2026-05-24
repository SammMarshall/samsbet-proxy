# samsbet_proxy/main.py
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests
import urllib3
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# --- Configurações ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("samsbet-proxy")
app = FastAPI()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

SOFASCORE_BASE_URL = os.environ.get("SOFASCORE_BASE_URL", "https://www.sofascore.com/api/v1").rstrip("/")
SOFASCORE_TEST_PATH = "sport/football/scheduled-events/today"
SOFASCORE_TEST_URL = f"{SOFASCORE_BASE_URL}/{SOFASCORE_TEST_PATH}"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_GIST_ID = os.environ.get("GITHUB_GIST_ID", "")
GIST_FILENAME = "proxy_config.json"

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "8"))
RETRY_SLEEP = float(os.environ.get("RETRY_SLEEP", "0.5"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "20.0"))
STATUS_TIMEOUT = float(os.environ.get("STATUS_TIMEOUT", "10.0"))

# Mantém o comportamento atual como padrão.
# Valores aceitos:
# - requests: comportamento antigo, usando requests e proxy se configurado.
# - scrapling: usa Scrapling Fetcher como motor principal.
# - auto: tenta Scrapling direto primeiro; se falhar, cai para requests/proxy.
SOFASCORE_FETCHER = os.environ.get("SOFASCORE_FETCHER", "requests").strip().lower()
if SOFASCORE_FETCHER not in {"requests", "scrapling", "auto"}:
    logger.warning("SOFASCORE_FETCHER inválido '%s'; usando 'requests'", SOFASCORE_FETCHER)
    SOFASCORE_FETCHER = "requests"

SCRAPLING_IMPERSONATE = os.environ.get("SCRAPLING_IMPERSONATE", "chrome")
SCRAPLING_HTTP3 = os.environ.get("SCRAPLING_HTTP3", "false").lower() in {"1", "true", "yes", "sim"}
SCRAPLING_STEALTHY_HEADERS = os.environ.get("SCRAPLING_STEALTHY_HEADERS", "true").lower() in {
    "1",
    "true",
    "yes",
    "sim",
}
AUTO_TRY_SCRAPLING_BEFORE_PROXY = os.environ.get("AUTO_TRY_SCRAPLING_BEFORE_PROXY", "true").lower() in {
    "1",
    "true",
    "yes",
    "sim",
}


# --- Helpers gerais ---
def safe_proxy_label(proxy_url: Optional[str]) -> str:
    if not proxy_url:
        return "none"
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname or "unknown-host"
        port = f":{parsed.port}" if parsed.port else ""
        scheme = parsed.scheme or "proxy"
        return f"{scheme}://{host}{port}"
    except Exception:
        return "configured"


def build_sofascore_url(path: str, query_params: str = "") -> str:
    clean_path = path.lstrip("/")
    url = f"{SOFASCORE_BASE_URL}/{clean_path}"
    if query_params:
        url += f"?{query_params}"
    return url


@dataclass
class NormalizedResponse:
    status_code: int
    text: str
    json_loader: Callable[[], Any]
    fetcher: str
    elapsed_ms: int

    def json(self) -> Any:
        return self.json_loader()

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            detail = (self.text or "")[:500]
            raise HTTPException(
                status_code=self.status_code,
                detail=f"Upstream HTTP {self.status_code} via {self.fetcher}: {detail}",
            )


def _response_text_from_scrapling(page: Any) -> str:
    text_attr = getattr(page, "text", None)
    if callable(text_attr):
        try:
            return str(text_attr())
        except Exception:
            pass
    if isinstance(text_attr, str):
        return text_attr

    body = getattr(page, "body", b"")
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        return body
    return str(body or "")


def _json_from_text(text: str) -> Any:
    return json.loads(text)


# --- Gist helpers ---
def gist_read() -> str:
    if not GITHUB_TOKEN or not GITHUB_GIST_ID:
        logger.warning("GITHUB_TOKEN ou GITHUB_GIST_ID não configurados.")
        return ""
    try:
        res = requests.get(
            f"https://api.github.com/gists/{GITHUB_GIST_ID}",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        res.raise_for_status()
        content = res.json()["files"][GIST_FILENAME]["content"]
        return json.loads(content).get("proxy_url", "")
    except Exception as e:
        logger.error("Erro ao ler Gist: %s", e)
        return ""


def gist_write(proxy_url: str) -> bool:
    if not GITHUB_TOKEN or not GITHUB_GIST_ID:
        return False
    try:
        res = requests.patch(
            f"https://api.github.com/gists/{GITHUB_GIST_ID}",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"files": {GIST_FILENAME: {"content": json.dumps({"proxy_url": proxy_url}, indent=2)}}},
            timeout=10,
        )
        res.raise_for_status()
        logger.info("Gist atualizado com sucesso.")
        return True
    except Exception as e:
        logger.error("Erro ao gravar Gist: %s", e)
        return False


# --- Estado em memória — carrega do Gist na inicialização ---
_initial_url = gist_read() or os.environ.get("PROXY_URL", "")
logger.info("Proxy inicial: %s", safe_proxy_label(_initial_url))
logger.info("SOFASCORE_FETCHER=%s", SOFASCORE_FETCHER)
proxy_state = {"url": _initial_url}


# --- Auth helper ---
def verify_token(authorization: str = Header(default=None)):
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN não configurado no servidor.")
    if authorization != f"Bearer {admin_token}":
        raise HTTPException(status_code=401, detail="Token inválido.")


# --- Motores de fetch ---
def fetch_with_requests(sofascore_url: str, proxies: Optional[dict], timeout: float = REQUEST_TIMEOUT) -> NormalizedResponse:
    started = time.perf_counter()
    response = requests.get(
        sofascore_url,
        headers=HEADERS,
        proxies=proxies,
        verify=False,
        timeout=timeout,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return NormalizedResponse(
        status_code=response.status_code,
        text=response.text,
        json_loader=response.json,
        fetcher="requests",
        elapsed_ms=elapsed_ms,
    )


def fetch_with_scrapling(
    sofascore_url: str,
    proxies: Optional[dict] = None,
    timeout: float = REQUEST_TIMEOUT,
) -> NormalizedResponse:
    """Fetch via Scrapling Fetcher.

    Usa curl_cffi por baixo, com impersonação TLS e headers de navegador.
    O proxy é aceito para testes/fallback, mas o objetivo principal aqui é testar sem proxy.
    """
    try:
        from scrapling.fetchers import Fetcher
    except Exception as e:
        raise RuntimeError(
            "Scrapling não está instalado. Adicione 'scrapling[fetchers]' ao requirements.txt."
        ) from e

    kwargs: dict[str, Any] = {
        "headers": HEADERS,
        "timeout": timeout,
        "retries": 1,
        "stealthy_headers": SCRAPLING_STEALTHY_HEADERS,
        "impersonate": SCRAPLING_IMPERSONATE,
        "http3": SCRAPLING_HTTP3,
        "verify": True,
    }
    if proxies:
        kwargs["proxies"] = proxies

    started = time.perf_counter()
    page = Fetcher.get(sofascore_url, **kwargs)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    status_code = getattr(page, "status", None) or getattr(page, "status_code", None) or 0
    text = _response_text_from_scrapling(page)
    json_func = getattr(page, "json", None)

    return NormalizedResponse(
        status_code=int(status_code),
        text=text,
        json_loader=json_func if callable(json_func) else lambda: _json_from_text(text),
        fetcher="scrapling",
        elapsed_ms=elapsed_ms,
    )


def is_bad_endpoint(response: NormalizedResponse) -> bool:
    response_text = (response.text or "").lower()
    return response.status_code in (402, 500) and (
        "bad_endpoint" in response_text or "residential failed" in response_text
    )


def choose_fetcher(sofascore_url: str, proxies: Optional[dict]) -> NormalizedResponse:
    """Executa uma tentativa única conforme SOFASCORE_FETCHER."""
    if SOFASCORE_FETCHER == "scrapling":
        return fetch_with_scrapling(sofascore_url, proxies=None)

    if SOFASCORE_FETCHER == "auto":
        if AUTO_TRY_SCRAPLING_BEFORE_PROXY:
            try:
                scrapling_response = fetch_with_scrapling(sofascore_url, proxies=None)
                logger.info(
                    "auto scrapling status=%s elapsed_ms=%s url=%s",
                    scrapling_response.status_code,
                    scrapling_response.elapsed_ms,
                    sofascore_url,
                )
                if scrapling_response.status_code < 400:
                    return scrapling_response
                logger.warning(
                    "auto scrapling falhou status=%s; tentando requests/proxy",
                    scrapling_response.status_code,
                )
            except Exception as e:
                logger.warning("auto scrapling exception; tentando requests/proxy: %s", e)
        return fetch_with_requests(sofascore_url, proxies=proxies)

    return fetch_with_requests(sofascore_url, proxies=proxies)


# --- Requisição com retry para bad_endpoint ---
def fetch_with_retry(sofascore_url: str, proxies: Optional[dict]) -> NormalizedResponse:
    """
    Mantém o retry antigo para bad_endpoint de proxy.
    No modo default, continua usando requests exatamente como antes.
    """
    last_response: Optional[NormalizedResponse] = None

    for attempt in range(1, MAX_RETRIES + 1):
        response = choose_fetcher(sofascore_url, proxies)
        last_response = response

        logger.info(
            "upstream attempt=%s/%s fetcher=%s status=%s elapsed_ms=%s proxy=%s url=%s",
            attempt,
            MAX_RETRIES,
            response.fetcher,
            response.status_code,
            response.elapsed_ms,
            safe_proxy_label(proxy_state.get("url")) if proxies else "none",
            sofascore_url,
        )

        if is_bad_endpoint(response):
            logger.warning("bad_endpoint tentativa %s/%s: %s", attempt, MAX_RETRIES, sofascore_url)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP)
            continue

        return response

    logger.error("Esgotou %s tentativas para %s", MAX_RETRIES, sofascore_url)
    if last_response is None:
        raise RuntimeError("Nenhuma resposta upstream foi obtida.")
    return last_response


# --- Dashboard HTML ---
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard():
    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Proxy Manager</title>
  <style>
    body { font-family: Arial, sans-serif; background:#0a0a0f; color:#e2e2f0; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px; }
    .container { width:100%; max-width:680px; display:flex; flex-direction:column; gap:16px; }
    .card { background:#111118; border:1px solid #1e1e2e; border-radius:12px; padding:20px; display:flex; flex-direction:column; gap:12px; }
    h1 { margin:0 0 4px; }
    small, label { color:#8a8aa8; }
    input { width:100%; background:#0a0a0f; color:#fff; border:1px solid #33334a; border-radius:8px; padding:11px; box-sizing:border-box; }
    button { border:0; border-radius:8px; padding:11px 14px; font-weight:700; cursor:pointer; background:#00ff88; color:#000; }
    button.secondary { background:transparent; color:#e2e2f0; border:1px solid #33334a; }
    .row { display:flex; gap:10px; }
    .row button { flex:1; }
    pre { white-space:pre-wrap; word-break:break-word; background:#0a0a0f; padding:12px; border-radius:8px; border:1px solid #1e1e2e; }
    .ok { color:#00ff88; }
    .fail { color:#ff4466; }
  </style>
</head>
<body>
<div class="container">
  <div>
    <h1>Proxy Manager</h1>
    <small>samsbet-proxy</small>
  </div>

  <div class="card" id="auth-card">
    <label>Admin Token</label>
    <input type="password" id="token-input" placeholder="Bearer token" />
    <button onclick="doLogin()">Entrar</button>
  </div>

  <div class="card" id="main-card" style="display:none">
    <strong>Status</strong>
    <div id="status-line">—</div>
    <pre id="status-json">—</pre>
    <div class="row">
      <button class="secondary" onclick="checkStatus()">Testar proxy</button>
      <button class="secondary" onclick="checkFetchers()">Diagnóstico fetchers</button>
    </div>
  </div>

  <div class="card" id="update-card" style="display:none">
    <label>Nova URL do proxy</label>
    <input type="text" id="proxy-input" placeholder="http://user:pass@host:port" />
    <div class="row">
      <button class="secondary" onclick="clearProxy()">Limpar</button>
      <button onclick="updateProxy()">Salvar & Testar</button>
    </div>
  </div>
</div>
<script>
let TOKEN = sessionStorage.getItem('admin_token') || '';
function authHeaders() { return { Authorization: 'Bearer ' + TOKEN }; }
function render(data) {
  document.getElementById('status-line').innerHTML = data.ok ? '<span class="ok">Funcionando</span>' : '<span class="fail">Com falha</span>';
  document.getElementById('status-json').textContent = JSON.stringify(data, null, 2);
}
function showApp(data) {
  document.getElementById('auth-card').style.display = 'none';
  document.getElementById('main-card').style.display = 'flex';
  document.getElementById('update-card').style.display = 'flex';
  render(data);
}
async function doLogin() {
  const val = document.getElementById('token-input').value.trim();
  if (!val) return alert('Digite o token');
  TOKEN = val;
  const res = await fetch('/proxy-status', { headers: authHeaders() });
  if (!res.ok) return alert('Token inválido ou erro');
  sessionStorage.setItem('admin_token', TOKEN);
  showApp(await res.json());
}
async function checkStatus() {
  const data = await fetch('/proxy-status', { headers: authHeaders() }).then(r => r.json());
  render(data);
}
async function checkFetchers() {
  const data = await fetch('/debug/fetchers', { headers: authHeaders() }).then(r => r.json());
  render({ ok: data.summary && data.summary.any_ok, ...data });
}
async function updateProxy() {
  const url = document.getElementById('proxy-input').value.trim();
  const data = await fetch('/proxy-update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ proxy_url: url })
  }).then(r => r.json());
  document.getElementById('proxy-input').value = '';
  render(data);
}
async function clearProxy() {
  const data = await fetch('/proxy-update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ proxy_url: '' })
  }).then(r => r.json());
  render(data);
}
if (TOKEN) {
  fetch('/proxy-status', { headers: authHeaders() })
    .then(r => r.ok ? r.json() : null)
    .then(data => { if (data) showApp(data); else sessionStorage.removeItem('admin_token'); });
}
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


# --- Endpoints de gerenciamento ---
class ProxyUpdateRequest(BaseModel):
    proxy_url: str


@app.get("/proxy-status")
def proxy_status(authorization: str = Header(default=None)):
    verify_token(authorization)
    url = proxy_state["url"]
    if not url:
        return JSONResponse(
            {
                "ok": False,
                "proxy_url": "",
                "fetcher_mode": SOFASCORE_FETCHER,
                "detail": "Nenhum proxy configurado.",
            }
        )
    try:
        test = fetch_with_requests(
            SOFASCORE_TEST_URL,
            proxies={"http": url, "https": url},
            timeout=STATUS_TIMEOUT,
        )
        test.raise_for_status()
        return JSONResponse(
            {
                "ok": True,
                "proxy_url": url,
                "proxy_label": safe_proxy_label(url),
                "fetcher": test.fetcher,
                "fetcher_mode": SOFASCORE_FETCHER,
                "status_code": test.status_code,
                "elapsed_ms": test.elapsed_ms,
                "gist_synced": None,
            }
        )
    except Exception as e:
        logger.warning("Proxy status check falhou: %s", e)
        return JSONResponse(
            {
                "ok": False,
                "proxy_url": url,
                "proxy_label": safe_proxy_label(url),
                "fetcher_mode": SOFASCORE_FETCHER,
                "detail": str(e),
            }
        )


@app.post("/proxy-update")
def proxy_update(body: ProxyUpdateRequest, authorization: str = Header(default=None)):
    verify_token(authorization)
    proxy_state["url"] = body.proxy_url
    logger.info("PROXY_URL atualizado para: %s", safe_proxy_label(body.proxy_url))
    gist_ok = gist_write(body.proxy_url)
    if not body.proxy_url:
        return JSONResponse(
            {
                "ok": False,
                "proxy_url": "",
                "fetcher_mode": SOFASCORE_FETCHER,
                "gist_synced": gist_ok,
                "detail": "Proxy removido.",
            }
        )
    try:
        test = fetch_with_requests(
            SOFASCORE_TEST_URL,
            proxies={"http": body.proxy_url, "https": body.proxy_url},
            timeout=STATUS_TIMEOUT,
        )
        test.raise_for_status()
        return JSONResponse(
            {
                "ok": True,
                "proxy_url": body.proxy_url,
                "proxy_label": safe_proxy_label(body.proxy_url),
                "fetcher": test.fetcher,
                "fetcher_mode": SOFASCORE_FETCHER,
                "status_code": test.status_code,
                "elapsed_ms": test.elapsed_ms,
                "gist_synced": gist_ok,
            }
        )
    except Exception as e:
        logger.warning("Novo proxy falhou no teste: %s", e)
        return JSONResponse(
            {
                "ok": False,
                "proxy_url": body.proxy_url,
                "proxy_label": safe_proxy_label(body.proxy_url),
                "fetcher_mode": SOFASCORE_FETCHER,
                "gist_synced": gist_ok,
                "detail": str(e),
            }
        )


@app.get("/debug/fetchers")
def debug_fetchers(authorization: str = Header(default=None)):
    verify_token(authorization)
    url = proxy_state["url"]
    proxies = {"http": url, "https": url} if url else None

    checks: dict[str, Any] = {}

    def run_check(name: str, fn: Callable[[], NormalizedResponse]) -> None:
        try:
            res = fn()
            checks[name] = {
                "ok": res.status_code < 400,
                "status_code": res.status_code,
                "elapsed_ms": res.elapsed_ms,
                "fetcher": res.fetcher,
                "text_preview": (res.text or "")[:160],
            }
        except Exception as e:
            checks[name] = {"ok": False, "error": str(e)}

    run_check("requests_direct", lambda: fetch_with_requests(SOFASCORE_TEST_URL, proxies=None, timeout=STATUS_TIMEOUT))
    run_check("scrapling_direct", lambda: fetch_with_scrapling(SOFASCORE_TEST_URL, proxies=None, timeout=STATUS_TIMEOUT))
    if proxies:
        run_check("requests_proxy", lambda: fetch_with_requests(SOFASCORE_TEST_URL, proxies=proxies, timeout=STATUS_TIMEOUT))
        run_check("scrapling_proxy", lambda: fetch_with_scrapling(SOFASCORE_TEST_URL, proxies=proxies, timeout=STATUS_TIMEOUT))

    any_ok = any(item.get("ok") for item in checks.values() if isinstance(item, dict))
    return JSONResponse(
        {
            "summary": {
                "any_ok": any_ok,
                "fetcher_mode": SOFASCORE_FETCHER,
                "proxy_configured": bool(url),
                "proxy_label": safe_proxy_label(url),
                "test_url": SOFASCORE_TEST_URL,
            },
            "checks": checks,
        }
    )


# --- Lógica do Proxy ---
@app.get("/{path:path}")
def proxy_request(path: str, request: Request):
    query_params = str(request.url.query)
    sofascore_url = build_sofascore_url(path, query_params)

    url = proxy_state["url"]
    proxies = {"http": url, "https": url} if url else None

    try:
        response = fetch_with_retry(sofascore_url, proxies)
        response.raise_for_status()
        payload = response.json()
        logger.info(
            "proxy success fetcher=%s status=%s elapsed_ms=%s path=%s",
            response.fetcher,
            response.status_code,
            response.elapsed_ms,
            path,
        )
        return JSONResponse(content=payload)
    except HTTPException as e:
        logger.error("Erro HTTP upstream para %s: %s", sofascore_url, e.detail)
        return JSONResponse(content={"error": e.detail}, status_code=500)
    except Exception as e:
        logger.exception("Erro no proxy para %s", sofascore_url)
        return JSONResponse(content={"error": str(e)}, status_code=500)
