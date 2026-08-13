# samsbet_proxy/main.py
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

import requests
import urllib3
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

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

# A Betano não passa pelo home_relay: o túnel só reescreve path do SofaScore.
BETANO_ORIGIN = os.environ.get("BETANO_ORIGIN", "https://www.betano.bet.br").rstrip("/")
BETANO_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": f"{BETANO_ORIGIN}/",
    "Origin": BETANO_ORIGIN,
}

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_GIST_ID = os.environ.get("GITHUB_GIST_ID", "")
GIST_FILENAME = "proxy_config.json"

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "8"))
RETRY_SLEEP = float(os.environ.get("RETRY_SLEEP", "0.5"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "20.0"))
STATUS_TIMEOUT = float(os.environ.get("STATUS_TIMEOUT", "10.0"))

HOME_RELAY_URL = os.environ.get("HOME_RELAY_URL", "").rstrip("/")
HOME_RELAY_TOKEN = os.environ.get("HOME_RELAY_TOKEN", "")

SOFASCORE_FETCHER = os.environ.get("SOFASCORE_FETCHER", "requests").strip().lower()
VALID_FETCHERS = {"requests", "scrapling", "home_relay", "auto"}
if SOFASCORE_FETCHER not in VALID_FETCHERS:
    logger.warning("SOFASCORE_FETCHER inválido '%s'; usando 'requests'", SOFASCORE_FETCHER)
    SOFASCORE_FETCHER = "requests"

SCRAPLING_IMPERSONATE = os.environ.get("SCRAPLING_IMPERSONATE", "chrome")
SCRAPLING_HTTP3 = os.environ.get("SCRAPLING_HTTP3", "false").lower() in {"1", "true", "yes", "sim"}
SCRAPLING_STEALTHY_HEADERS = os.environ.get("SCRAPLING_STEALTHY_HEADERS", "true").lower() in {"1", "true", "yes", "sim"}
AUTO_TRY_SCRAPLING_BEFORE_PROXY = os.environ.get("AUTO_TRY_SCRAPLING_BEFORE_PROXY", "true").lower() in {"1", "true", "yes", "sim"}


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


def safe_endpoint_label(url: Optional[str]) -> str:
    if not url:
        return "none"
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "unknown-host"
        port = f":{parsed.port}" if parsed.port else ""
        scheme = parsed.scheme or "https"
        return f"{scheme}://{host}{port}"
    except Exception:
        return "configured"


def proxy_label_from_mapping(proxies: Optional[dict]) -> str:
    if not proxies:
        return "none"
    return safe_proxy_label(proxies.get("https") or proxies.get("http"))


def build_sofascore_url(path: str, query_params: str = "") -> str:
    clean_path = path.lstrip("/")
    url = f"{SOFASCORE_BASE_URL}/{clean_path}"
    if query_params:
        url += f"?{query_params}"
    return url


def build_home_relay_url(sofascore_url: str) -> str:
    if not HOME_RELAY_URL or not HOME_RELAY_TOKEN:
        raise RuntimeError("HOME_RELAY_URL ou HOME_RELAY_TOKEN não configurado.")

    upstream = urlparse(sofascore_url)
    base = urlparse(SOFASCORE_BASE_URL)
    base_path = base.path.rstrip("/")
    upstream_path = upstream.path

    if base_path and upstream_path.startswith(base_path + "/"):
        relay_path = upstream_path[len(base_path):].lstrip("/")
    else:
        relay_path = upstream_path.lstrip("/")

    relay_url = f"{HOME_RELAY_URL}/{relay_path}"
    if upstream.query:
        relay_url += f"?{upstream.query}"
    return relay_url


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


def _json_from_text(text: str) -> Any:
    return json.loads(text)


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


_initial_url = gist_read() or os.environ.get("PROXY_URL", "")
logger.info("Proxy inicial: %s", safe_proxy_label(_initial_url))
logger.info("SOFASCORE_FETCHER=%s", SOFASCORE_FETCHER)
logger.info("HOME_RELAY_URL=%s", safe_endpoint_label(HOME_RELAY_URL))
proxy_state = {"url": _initial_url}


def verify_token(authorization: str = Header(default=None)):
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN não configurado no servidor.")
    if authorization != f"Bearer {admin_token}":
        raise HTTPException(status_code=401, detail="Token inválido.")


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
    return NormalizedResponse(response.status_code, response.text, response.json, "requests", elapsed_ms)


def fetch_with_home_relay(sofascore_url: str, timeout: float = REQUEST_TIMEOUT) -> NormalizedResponse:
    relay_url = build_home_relay_url(sofascore_url)
    started = time.perf_counter()
    header_name = "x-relay-" + "token"
    response = requests.get(
        relay_url,
        headers={header_name: HOME_RELAY_TOKEN},
        timeout=timeout,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return NormalizedResponse(response.status_code, response.text, response.json, "home_relay", elapsed_ms)


def fetch_with_scrapling(
    sofascore_url: str,
    proxies: Optional[dict] = None,
    timeout: float = REQUEST_TIMEOUT,
) -> NormalizedResponse:
    try:
        from scrapling.fetchers import Fetcher
    except Exception as e:
        raise RuntimeError(
            f"Erro ao importar Scrapling Fetcher: {type(e).__name__}: {e}. "
            "Verifique se requirements.txt instalou scrapling, curl_cffi, browserforge e playwright."
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
        int(status_code),
        text,
        json_func if callable(json_func) else lambda: _json_from_text(text),
        "scrapling",
        elapsed_ms,
    )


def is_bad_endpoint(response: NormalizedResponse) -> bool:
    response_text = (response.text or "").lower()
    return response.status_code in (402, 500) and (
        "bad_endpoint" in response_text or "residential failed" in response_text
    )


def choose_fetcher(sofascore_url: str, proxies: Optional[dict]) -> NormalizedResponse:
    if SOFASCORE_FETCHER == "home_relay":
        return fetch_with_home_relay(sofascore_url)

    if SOFASCORE_FETCHER == "scrapling":
        return fetch_with_scrapling(sofascore_url, proxies=None)

    if SOFASCORE_FETCHER == "auto":
        if AUTO_TRY_SCRAPLING_BEFORE_PROXY:
            try:
                scrapling_response = fetch_with_scrapling(sofascore_url, proxies=None)
                logger.info(
                    "auto scrapling status=%s elapsed_ms=%s route=direct url=%s",
                    scrapling_response.status_code,
                    scrapling_response.elapsed_ms,
                    sofascore_url,
                )
                if scrapling_response.status_code < 400:
                    return scrapling_response
                logger.warning(
                    "auto scrapling falhou status=%s; tentando requests/proxy=%s",
                    scrapling_response.status_code,
                    proxy_label_from_mapping(proxies),
                )
            except Exception as e:
                logger.warning(
                    "auto scrapling exception; tentando requests/proxy=%s: %s",
                    proxy_label_from_mapping(proxies),
                    e,
                )
        return fetch_with_requests(sofascore_url, proxies=proxies)

    return fetch_with_requests(sofascore_url, proxies=proxies)


def fetch_with_retry(sofascore_url: str, proxies: Optional[dict]) -> NormalizedResponse:
    last_response: Optional[NormalizedResponse] = None

    for attempt in range(1, MAX_RETRIES + 1):
        response = choose_fetcher(sofascore_url, proxies)
        last_response = response

        if response.fetcher == "requests":
            route = proxy_label_from_mapping(proxies)
        elif response.fetcher == "home_relay":
            route = safe_endpoint_label(HOME_RELAY_URL)
        else:
            route = "direct"

        logger.info(
            "upstream attempt=%s/%s fetcher=%s status=%s elapsed_ms=%s route=%s url=%s",
            attempt,
            MAX_RETRIES,
            response.fetcher,
            response.status_code,
            response.elapsed_ms,
            route,
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


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard():
    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Proxy Manager</title>
  <style>
    body{font-family:Arial,sans-serif;background:#0a0a0f;color:#e2e2f0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}.container{width:100%;max-width:680px;display:flex;flex-direction:column;gap:16px}.card{background:#111118;border:1px solid #1e1e2e;border-radius:12px;padding:20px;display:flex;flex-direction:column;gap:12px}input{width:100%;background:#0a0a0f;color:#fff;border:1px solid #33334a;border-radius:8px;padding:11px;box-sizing:border-box}button{border:0;border-radius:8px;padding:11px 14px;font-weight:700;cursor:pointer;background:#00ff88;color:#000}.secondary{background:transparent;color:#e2e2f0;border:1px solid #33334a}.row{display:flex;gap:10px}.row button{flex:1}pre{white-space:pre-wrap;word-break:break-word;background:#0a0a0f;padding:12px;border-radius:8px;border:1px solid #1e1e2e}.ok{color:#00ff88}.fail{color:#ff4466}
  </style>
</head>
<body>
<div class="container">
  <div><h1>Proxy Manager</h1><small>samsbet-proxy</small></div>
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
    <div class="row"><button class="secondary" onclick="clearProxy()">Limpar</button><button onclick="updateProxy()">Salvar & Testar</button></div>
  </div>
</div>
<script>
let TOKEN=sessionStorage.getItem('admin_token')||'';
function authHeaders(){return{Authorization:'Bearer '+TOKEN}}
function render(data){document.getElementById('status-line').innerHTML=data.ok?'<span class="ok">Funcionando</span>':'<span class="fail">Com falha</span>';document.getElementById('status-json').textContent=JSON.stringify(data,null,2)}
function showApp(data){document.getElementById('auth-card').style.display='none';document.getElementById('main-card').style.display='flex';document.getElementById('update-card').style.display='flex';render(data)}
async function doLogin(){const val=document.getElementById('token-input').value.trim();if(!val)return alert('Digite o token');TOKEN=val;const res=await fetch('/proxy-status',{headers:authHeaders()});if(!res.ok)return alert('Token inválido ou erro');sessionStorage.setItem('admin_token',TOKEN);showApp(await res.json())}
async function checkStatus(){render(await fetch('/proxy-status',{headers:authHeaders()}).then(r=>r.json()))}
async function checkFetchers(){const data=await fetch('/debug/fetchers',{headers:authHeaders()}).then(r=>r.json());render({ok:data.summary&&data.summary.any_ok,...data})}
async function updateProxy(){const url=document.getElementById('proxy-input').value.trim();const data=await fetch('/proxy-update',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({proxy_url:url})}).then(r=>r.json());document.getElementById('proxy-input').value='';render(data)}
async function clearProxy(){const data=await fetch('/proxy-update',{method:'POST',headers:{'Content-Type':'application/json',...authHeaders()},body:JSON.stringify({proxy_url:''})}).then(r=>r.json());render(data)}
if(TOKEN){fetch('/proxy-status',{headers:authHeaders()}).then(r=>r.ok?r.json():null).then(data=>{if(data)showApp(data);else sessionStorage.removeItem('admin_token')})}
</script>
</body>
</html>"""
    return HTMLResponse(content=html)


class ProxyUpdateRequest(BaseModel):
    proxy_url: str


@app.get("/proxy-status")
def proxy_status(authorization: str = Header(default=None)):
    verify_token(authorization)
    url = proxy_state["url"]
    if not url:
        return JSONResponse({"ok": False, "proxy_url": "", "fetcher_mode": SOFASCORE_FETCHER, "home_relay_url": safe_endpoint_label(HOME_RELAY_URL), "detail": "Nenhum proxy configurado."})
    try:
        test = fetch_with_requests(SOFASCORE_TEST_URL, proxies={"http": url, "https": url}, timeout=STATUS_TIMEOUT)
        test.raise_for_status()
        return JSONResponse({"ok": True, "proxy_url": url, "proxy_label": safe_proxy_label(url), "fetcher": test.fetcher, "fetcher_mode": SOFASCORE_FETCHER, "home_relay_url": safe_endpoint_label(HOME_RELAY_URL), "status_code": test.status_code, "elapsed_ms": test.elapsed_ms, "gist_synced": None})
    except Exception as e:
        logger.warning("Proxy status check falhou: %s", e)
        return JSONResponse({"ok": False, "proxy_url": url, "proxy_label": safe_proxy_label(url), "fetcher_mode": SOFASCORE_FETCHER, "home_relay_url": safe_endpoint_label(HOME_RELAY_URL), "detail": str(e)})


@app.post("/proxy-update")
def proxy_update(body: ProxyUpdateRequest, authorization: str = Header(default=None)):
    verify_token(authorization)
    proxy_state["url"] = body.proxy_url
    logger.info("PROXY_URL atualizado para: %s", safe_proxy_label(body.proxy_url))
    gist_ok = gist_write(body.proxy_url)
    if not body.proxy_url:
        return JSONResponse({"ok": False, "proxy_url": "", "fetcher_mode": SOFASCORE_FETCHER, "home_relay_url": safe_endpoint_label(HOME_RELAY_URL), "gist_synced": gist_ok, "detail": "Proxy removido."})
    try:
        test = fetch_with_requests(SOFASCORE_TEST_URL, proxies={"http": body.proxy_url, "https": body.proxy_url}, timeout=STATUS_TIMEOUT)
        test.raise_for_status()
        return JSONResponse({"ok": True, "proxy_url": body.proxy_url, "proxy_label": safe_proxy_label(body.proxy_url), "fetcher": test.fetcher, "fetcher_mode": SOFASCORE_FETCHER, "home_relay_url": safe_endpoint_label(HOME_RELAY_URL), "status_code": test.status_code, "elapsed_ms": test.elapsed_ms, "gist_synced": gist_ok})
    except Exception as e:
        logger.warning("Novo proxy falhou no teste: %s", e)
        return JSONResponse({"ok": False, "proxy_url": body.proxy_url, "proxy_label": safe_proxy_label(body.proxy_url), "fetcher_mode": SOFASCORE_FETCHER, "home_relay_url": safe_endpoint_label(HOME_RELAY_URL), "gist_synced": gist_ok, "detail": str(e)})


@app.get("/debug/fetchers")
def debug_fetchers(authorization: str = Header(default=None)):
    verify_token(authorization)
    url = proxy_state["url"]
    proxies = {"http": url, "https": url} if url else None
    checks: dict[str, Any] = {}

    def run_check(name: str, fn: Callable[[], NormalizedResponse]) -> None:
        try:
            res = fn()
            if res.fetcher == "home_relay":
                route = safe_endpoint_label(HOME_RELAY_URL)
            elif name.endswith("proxy"):
                route = proxy_label_from_mapping(proxies)
            else:
                route = "direct"
            checks[name] = {"ok": res.status_code < 400, "status_code": res.status_code, "elapsed_ms": res.elapsed_ms, "fetcher": res.fetcher, "route": route, "text_preview": (res.text or "")[:160]}
        except Exception as e:
            checks[name] = {"ok": False, "error": str(e)}

    run_check("requests_direct", lambda: fetch_with_requests(SOFASCORE_TEST_URL, proxies=None, timeout=STATUS_TIMEOUT))
    run_check("scrapling_direct", lambda: fetch_with_scrapling(SOFASCORE_TEST_URL, proxies=None, timeout=STATUS_TIMEOUT))
    if HOME_RELAY_URL and HOME_RELAY_TOKEN:
        run_check("home_relay", lambda: fetch_with_home_relay(SOFASCORE_TEST_URL, timeout=STATUS_TIMEOUT))
    if proxies:
        run_check("requests_proxy", lambda: fetch_with_requests(SOFASCORE_TEST_URL, proxies=proxies, timeout=STATUS_TIMEOUT))
        run_check("scrapling_proxy", lambda: fetch_with_scrapling(SOFASCORE_TEST_URL, proxies=proxies, timeout=STATUS_TIMEOUT))

    any_ok = any(item.get("ok") for item in checks.values() if isinstance(item, dict))
    return JSONResponse({"summary": {"any_ok": any_ok, "fetcher_mode": SOFASCORE_FETCHER, "proxy_configured": bool(url), "proxy_label": safe_proxy_label(url), "home_relay_configured": bool(HOME_RELAY_URL and HOME_RELAY_TOKEN), "home_relay_url": safe_endpoint_label(HOME_RELAY_URL), "test_url": SOFASCORE_TEST_URL}, "checks": checks})


def build_betano_url(path: str, query_params: str = "") -> str:
    clean_path = path.lstrip("/")
    url = f"{BETANO_ORIGIN}/{clean_path}"
    if query_params:
        url += f"?{query_params}"
    return url


def fetch_betano_with_curl_cffi(
    betano_url: str,
    timeout: float = REQUEST_TIMEOUT,
    headers: Optional[dict] = None,
) -> NormalizedResponse:
    from curl_cffi import requests as cf_requests

    started = time.perf_counter()
    response = cf_requests.get(
        betano_url,
        headers=headers or BETANO_HEADERS,
        impersonate="chrome",
        timeout=timeout,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return NormalizedResponse(
        response.status_code,
        response.text,
        response.json,
        "curl_cffi",
        elapsed_ms,
    )


def fetch_betano_with_scrapling(
    betano_url: str,
    timeout: float = REQUEST_TIMEOUT,
    headers: Optional[dict] = None,
) -> NormalizedResponse:
    try:
        from scrapling.fetchers import Fetcher
    except Exception as e:
        raise RuntimeError(
            f"Erro ao importar Scrapling Fetcher: {type(e).__name__}: {e}."
        ) from e

    started = time.perf_counter()
    page = Fetcher.get(
        betano_url,
        headers=headers or BETANO_HEADERS,
        timeout=timeout,
        retries=1,
        stealthy_headers=SCRAPLING_STEALTHY_HEADERS,
        impersonate=SCRAPLING_IMPERSONATE,
        http3=SCRAPLING_HTTP3,
        verify=True,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    status_code = getattr(page, "status", None) or getattr(page, "status_code", None) or 0
    text = _response_text_from_scrapling(page)
    json_func = getattr(page, "json", None)
    return NormalizedResponse(
        int(status_code),
        text,
        json_func if callable(json_func) else lambda: _json_from_text(text),
        "scrapling",
        elapsed_ms,
    )


def fetch_betano_with_requests(
    betano_url: str,
    proxies: Optional[dict],
    timeout: float = REQUEST_TIMEOUT,
    headers: Optional[dict] = None,
) -> NormalizedResponse:
    started = time.perf_counter()
    response = requests.get(
        betano_url,
        headers=headers or BETANO_HEADERS,
        proxies=proxies,
        verify=False,
        timeout=timeout,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return NormalizedResponse(response.status_code, response.text, response.json, "requests", elapsed_ms)


def choose_betano_fetcher(
    betano_url: str,
    proxies: Optional[dict],
    extra_headers: Optional[dict] = None,
) -> NormalizedResponse:
    """
    Busca a Betano a partir deste host (Render), sem o home_relay do SofaScore.

    Ordem: Scrapling → curl_cffi → requests via PROXY_URL residencial do Gist, se houver.
    """
    headers = dict(BETANO_HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    last_error: Optional[NormalizedResponse] = None
    try:
        scrapling_response = fetch_betano_with_scrapling(betano_url, headers=headers)
        logger.info(
            "betano scrapling status=%s elapsed_ms=%s url=%s",
            scrapling_response.status_code,
            scrapling_response.elapsed_ms,
            betano_url,
        )
        if scrapling_response.status_code < 400:
            return scrapling_response
        last_error = scrapling_response
    except Exception as e:
        logger.warning("betano scrapling exception: %s", e)

    try:
        cffi_response = fetch_betano_with_curl_cffi(betano_url, headers=headers)
        logger.info(
            "betano curl_cffi status=%s elapsed_ms=%s url=%s",
            cffi_response.status_code,
            cffi_response.elapsed_ms,
            betano_url,
        )
        if cffi_response.status_code < 400:
            return cffi_response
        last_error = cffi_response
    except Exception as e:
        logger.warning("betano curl_cffi exception: %s", e)

    if proxies:
        proxy_response = fetch_betano_with_requests(
            betano_url,
            proxies=proxies,
            headers=headers,
        )
        logger.info(
            "betano requests/proxy status=%s elapsed_ms=%s route=%s url=%s",
            proxy_response.status_code,
            proxy_response.elapsed_ms,
            proxy_label_from_mapping(proxies),
            betano_url,
        )
        return proxy_response

    if last_error is not None:
        return last_error
    raise RuntimeError("Nenhuma resposta Betano foi obtida.")


@app.get("/betano/{path:path}")
def betano_proxy(path: str, request: Request):
    """Espelho da Betano. Ex.: /betano/api/sport/futebol/ligas/ → betano.bet.br/..."""
    query_params = str(request.url.query)
    betano_url = build_betano_url(path, query_params)
    url = proxy_state["url"]
    proxies = {"http": url, "https": url} if url else None
    extra_headers: dict[str, str] = {}
    cookie = request.headers.get("cookie")
    if cookie:
        extra_headers["Cookie"] = cookie

    try:
        response = choose_betano_fetcher(betano_url, proxies, extra_headers=extra_headers)
        response.raise_for_status()
        payload = response.json()
        logger.info(
            "betano success fetcher=%s status=%s elapsed_ms=%s path=%s",
            response.fetcher,
            response.status_code,
            response.elapsed_ms,
            path,
        )
        return JSONResponse(content=payload)
    except HTTPException as e:
        logger.error("Erro HTTP Betano para %s: %s", betano_url, e.detail)
        return JSONResponse(content={"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        logger.exception("Erro no proxy Betano para %s", betano_url)
        return JSONResponse(content={"error": str(e)}, status_code=500)


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
        logger.info("proxy success fetcher=%s status=%s elapsed_ms=%s path=%s", response.fetcher, response.status_code, response.elapsed_ms, path)
        return JSONResponse(content=payload)
    except HTTPException as e:
        logger.error("Erro HTTP upstream para %s: %s", sofascore_url, e.detail)
        return JSONResponse(content={"error": e.detail}, status_code=500)
    except Exception as e:
        logger.exception("Erro no proxy para %s", sofascore_url)
        return JSONResponse(content={"error": str(e)}, status_code=500)
