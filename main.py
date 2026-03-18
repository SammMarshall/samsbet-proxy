# samsbet_proxy/main.py
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
import requests
import logging
import os
import time
import urllib3
from pydantic import BaseModel

# --- Configurações ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO)
app = FastAPI()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_GIST_ID = os.environ.get("GITHUB_GIST_ID", "")
GIST_FILENAME  = "proxy_config.json"

MAX_RETRIES        = 8    # tentativas em caso de bad_endpoint
RETRY_SLEEP        = 0.5  # segundos entre tentativas

# --- Gist helpers ---
def gist_read() -> str:
    if not GITHUB_TOKEN or not GITHUB_GIST_ID:
        logging.warning("⚠️  GITHUB_TOKEN ou GITHUB_GIST_ID não configurados.")
        return ""
    try:
        res = requests.get(
            f"https://api.github.com/gists/{GITHUB_GIST_ID}",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        res.raise_for_status()
        import json
        content = res.json()["files"][GIST_FILENAME]["content"]
        return json.loads(content).get("proxy_url", "")
    except Exception as e:
        logging.error(f"❌ Erro ao ler Gist: {e}")
        return ""

def gist_write(proxy_url: str) -> bool:
    if not GITHUB_TOKEN or not GITHUB_GIST_ID:
        return False
    try:
        import json
        res = requests.patch(
            f"https://api.github.com/gists/{GITHUB_GIST_ID}",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"files": {GIST_FILENAME: {"content": json.dumps({"proxy_url": proxy_url}, indent=2)}}},
            timeout=10,
        )
        res.raise_for_status()
        logging.info("✅ Gist atualizado com sucesso.")
        return True
    except Exception as e:
        logging.error(f"❌ Erro ao gravar Gist: {e}")
        return False

# --- Estado em memória — carrega do Gist na inicialização ---
_initial_url = gist_read() or os.environ.get("PROXY_URL", "")
logging.info(f"🚀 Proxy inicial: {_initial_url or '(nenhum)'}")
proxy_state = {"url": _initial_url}

# --- Auth helper ---
def verify_token(authorization: str = Header(default=None)):
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN não configurado no servidor.")
    if authorization != f"Bearer {admin_token}":
        raise HTTPException(status_code=401, detail="Token inválido.")

# --- Requisição com retry para bad_endpoint ---
def fetch_with_retry(sofascore_url: str, proxies: dict) -> requests.Response:
    """
    Tenta a requisição até MAX_RETRIES vezes.
    Repete apenas se o Brightdata retornar bad_endpoint (erro 402/500).
    Qualquer outro erro encerra imediatamente.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        response = requests.get(sofascore_url, headers=HEADERS, proxies=proxies, verify=False, timeout=20.0)
        response_text = response.text.lower()

        is_bad_endpoint = (
            response.status_code in (402, 500)
            and ("bad_endpoint" in response_text or "residential failed" in response_text)
        )

        if is_bad_endpoint:
            logging.warning(f"⚠️ bad_endpoint tentativa {attempt}/{MAX_RETRIES}: {sofascore_url}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP)
            continue

        # Qualquer outro caso (sucesso ou erro diferente) — retorna imediatamente
        return response

    # Esgotou todas as tentativas — retorna a última resposta para o caller tratar
    logging.error(f"❌ Esgotou {MAX_RETRIES} tentativas (bad_endpoint) para {sofascore_url}")
    return response

# --- Dashboard HTML ---
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard():
    html = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Proxy Manager</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0a0a0f; --panel: #111118; --border: #1e1e2e;
      --accent: #00ff88; --accent-dim: #00ff8833; --accent-glow: #00ff8866;
      --danger: #ff4466; --text: #e2e2f0; --muted: #6b6b8a;
      --mono: 'JetBrains Mono', monospace; --sans: 'Syne', sans-serif;
    }
    body {
      background: var(--bg); color: var(--text); font-family: var(--sans);
      min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px;
      background-image:
        radial-gradient(ellipse 80% 50% at 50% -20%, #00ff8811 0%, transparent 70%),
        repeating-linear-gradient(0deg, transparent, transparent 39px, #1e1e2e22 39px, #1e1e2e22 40px),
        repeating-linear-gradient(90deg, transparent, transparent 39px, #1e1e2e22 39px, #1e1e2e22 40px);
    }
    .container { width: 100%; max-width: 560px; display: flex; flex-direction: column; gap: 16px; }
    header { display: flex; align-items: baseline; gap: 12px; padding-bottom: 8px; }
    header h1 { font-size: 28px; font-weight: 800; letter-spacing: -1px; color: #fff; }
    header span { font-family: var(--mono); font-size: 11px; color: var(--muted); letter-spacing: 2px; text-transform: uppercase; }
    .card {
      background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
      padding: 24px; display: flex; flex-direction: column; gap: 16px; position: relative; overflow: hidden;
    }
    .card::before {
      content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
      background: linear-gradient(90deg, transparent, var(--accent-glow), transparent);
    }
    .card-title { font-family: var(--mono); font-size: 11px; font-weight: 600; letter-spacing: 3px; text-transform: uppercase; color: var(--muted); }
    .status-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .status-badge { display: flex; align-items: center; gap: 8px; font-family: var(--mono); font-size: 14px; font-weight: 600; }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); flex-shrink: 0; }
    .dot.ok   { background: var(--accent); box-shadow: 0 0 8px var(--accent-glow); animation: pulse-ok 2s infinite; }
    .dot.fail { background: var(--danger); box-shadow: 0 0 8px var(--danger); animation: pulse-fail 1.5s infinite; }
    @keyframes pulse-ok   { 0%,100%{opacity:1} 50%{opacity:.4} }
    @keyframes pulse-fail { 0%,100%{opacity:1} 50%{opacity:.3} }
    .status-label { color: var(--text); }
    .status-label.ok   { color: var(--accent); }
    .status-label.fail { color: var(--danger); }
    .proxy-current {
      font-family: var(--mono); font-size: 12px; color: var(--muted); word-break: break-all;
      background: #0a0a0f; border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; min-height: 38px;
    }
    .gist-badge { display: flex; align-items: center; gap: 6px; font-family: var(--mono); font-size: 11px; color: var(--muted); }
    .gist-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
    .gist-dot.ok   { background: var(--accent); }
    .gist-dot.fail { background: var(--danger); }
    label { font-family: var(--mono); font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: var(--muted); display: block; margin-bottom: 6px; }
    input[type="password"], input[type="text"] {
      width: 100%; background: #0a0a0f; border: 1px solid var(--border); border-radius: 8px;
      padding: 12px 14px; font-family: var(--mono); font-size: 13px; color: var(--text);
      outline: none; transition: border-color .2s, box-shadow .2s;
    }
    input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }
    .btn {
      font-family: var(--sans); font-size: 14px; font-weight: 700; letter-spacing: .5px;
      border: none; border-radius: 8px; padding: 12px 20px; cursor: pointer;
      transition: opacity .15s, transform .1s, box-shadow .2s;
      display: flex; align-items: center; justify-content: center; gap: 8px;
    }
    .btn:active { transform: scale(.97); }
    .btn-primary { background: var(--accent); color: #000; }
    .btn-primary:hover { box-shadow: 0 0 20px var(--accent-glow); }
    .btn-ghost { background: transparent; color: var(--muted); border: 1px solid var(--border); }
    .btn-ghost:hover { color: var(--text); border-color: var(--muted); }
    .btn-row { display: flex; gap: 10px; }
    .btn-row .btn { flex: 1; }
    #toast {
      position: fixed; bottom: 24px; left: 50%;
      transform: translateX(-50%) translateY(80px);
      background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
      padding: 12px 20px; font-family: var(--mono); font-size: 13px; color: var(--text);
      transition: transform .3s cubic-bezier(.34,1.56,.64,1), opacity .3s;
      opacity: 0; pointer-events: none; white-space: nowrap; z-index: 100;
    }
    #toast.show { transform: translateX(-50%) translateY(0); opacity: 1; }
    #toast.ok   { border-color: var(--accent); color: var(--accent); }
    #toast.fail { border-color: var(--danger); color: var(--danger); }
    .spinner { width: 14px; height: 14px; border: 2px solid #00000044; border-top-color: #000; border-radius: 50%; animation: spin .6s linear infinite; display: none; }
    .btn.loading .spinner { display: block; }
    .btn.loading .btn-text { display: none; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
<div class="container">
  <header><h1>Proxy Manager</h1><span>samsbet</span></header>

  <div class="card" id="auth-card">
    <span class="card-title">Autenticação</span>
    <div>
      <label for="token-input">Admin Token</label>
      <input type="password" id="token-input" placeholder="••••••••••••" autocomplete="current-password"/>
    </div>
    <button class="btn btn-primary" onclick="doLogin()">
      <span class="btn-text">Entrar</span><div class="spinner"></div>
    </button>
  </div>

  <div class="card" id="main-card" style="display:none">
    <span class="card-title">Status do Proxy</span>
    <div class="status-row">
      <div class="status-badge">
        <div class="dot idle" id="status-dot"></div>
        <span class="status-label" id="status-label">Verificando...</span>
      </div>
      <button class="btn btn-ghost" style="padding:8px 14px;font-size:12px" onclick="checkStatus(event)">
        <span class="btn-text">↻ Testar</span>
        <div class="spinner" style="border-top-color:var(--muted)"></div>
      </button>
    </div>
    <div class="proxy-current" id="proxy-display">—</div>
    <div class="gist-badge">
      <div class="gist-dot" id="gist-dot"></div>
      <span id="gist-label">Gist: —</span>
    </div>
  </div>

  <div class="card" id="update-card" style="display:none">
    <span class="card-title">Atualizar Proxy URL</span>
    <div>
      <label for="proxy-input">Nova URL</label>
      <input type="text" id="proxy-input" placeholder="http://user:pass@host:port"/>
    </div>
    <div class="btn-row">
      <button class="btn btn-ghost" onclick="clearProxy(event)">
        <span class="btn-text">Limpar</span>
        <div class="spinner" style="border-top-color:var(--muted)"></div>
      </button>
      <button class="btn btn-primary" onclick="updateProxy(event)">
        <span class="btn-text">Salvar &amp; Testar</span><div class="spinner"></div>
      </button>
    </div>
  </div>
</div>
<div id="toast"></div>
<script>
  let TOKEN = sessionStorage.getItem('admin_token') || '';

  function toast(msg, type='ok') {
    const el = document.getElementById('toast');
    el.textContent = msg; el.className = 'show ' + type;
    clearTimeout(el._t); el._t = setTimeout(() => el.className = '', 3500);
  }
  function setLoading(btn, yes) { btn.classList.toggle('loading', yes); btn.disabled = yes; }

  function renderStatus(data) {
    const dot  = document.getElementById('status-dot');
    const lbl  = document.getElementById('status-label');
    const disp = document.getElementById('proxy-display');
    const gDot = document.getElementById('gist-dot');
    const gLbl = document.getElementById('gist-label');
    dot.className  = 'dot ' + (data.ok ? 'ok' : 'fail');
    lbl.className  = 'status-label ' + (data.ok ? 'ok' : 'fail');
    lbl.textContent  = data.ok ? 'Funcionando' : 'Com falha';
    disp.textContent = data.proxy_url || '(sem proxy configurado)';
    if (data.gist_synced !== undefined && data.gist_synced !== null) {
      gDot.className   = 'gist-dot ' + (data.gist_synced ? 'ok' : 'fail');
      gLbl.textContent = data.gist_synced ? 'Gist: sincronizado ✓' : 'Gist: falha ao sincronizar';
    }
  }

  function showApp(data) {
    document.getElementById('auth-card').style.display = 'none';
    document.getElementById('main-card').style.display = 'flex';
    document.getElementById('update-card').style.display = 'flex';
    renderStatus(data);
  }

  async function doLogin() {
    const btn = event.currentTarget;
    const val = document.getElementById('token-input').value.trim();
    if (!val) { toast('Digite o token', 'fail'); return; }
    setLoading(btn, true);
    try {
      const res = await fetch('/proxy-status', { headers: { Authorization: 'Bearer ' + val } });
      if (res.status === 401) { toast('Token inválido ✗', 'fail'); return; }
      TOKEN = val; sessionStorage.setItem('admin_token', TOKEN);
      showApp(await res.json());
    } catch(e) { toast('Erro de conexão', 'fail'); }
    finally { setLoading(btn, false); }
  }

  async function checkStatus(e) {
    const btn = e.currentTarget; setLoading(btn, true);
    try {
      const data = await fetch('/proxy-status', { headers: { Authorization: 'Bearer ' + TOKEN } }).then(r => r.json());
      renderStatus(data);
      toast(data.ok ? '✓ Proxy OK' : '✗ Proxy com falha', data.ok ? 'ok' : 'fail');
    } catch(e) { toast('Erro ao verificar', 'fail'); }
    finally { setLoading(btn, false); }
  }

  async function updateProxy(e) {
    const btn = e.currentTarget;
    const url = document.getElementById('proxy-input').value.trim();
    if (!url) { toast('Cole a URL do proxy', 'fail'); return; }
    setLoading(btn, true);
    try {
      const data = await fetch('/proxy-update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
        body: JSON.stringify({ proxy_url: url })
      }).then(r => r.json());
      renderStatus(data);
      document.getElementById('proxy-input').value = '';
      if (data.ok && data.gist_synced)       toast('✓ Proxy atualizado e salvo no Gist!', 'ok');
      else if (data.ok && !data.gist_synced) toast('✓ Proxy OK, mas falhou ao salvar no Gist', 'fail');
      else                                   toast('✗ Proxy salvo, mas falhou no teste', 'fail');
    } catch(e) { toast('Erro ao atualizar', 'fail'); }
    finally { setLoading(btn, false); }
  }

  async function clearProxy(e) {
    const btn = e.currentTarget; setLoading(btn, true);
    try {
      const data = await fetch('/proxy-update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
        body: JSON.stringify({ proxy_url: '' })
      }).then(r => r.json());
      renderStatus(data); toast('Proxy removido', 'ok');
    } catch(e) { toast('Erro', 'fail'); }
    finally { setLoading(btn, false); }
  }

  document.getElementById('token-input').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
  document.getElementById('proxy-input').addEventListener('keydown', e => { if (e.key === 'Enter') updateProxy(e); });

  if (TOKEN) {
    fetch('/proxy-status', { headers: { Authorization: 'Bearer ' + TOKEN } })
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (!data) { sessionStorage.removeItem('admin_token'); TOKEN = ''; return; } showApp(data); });
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
        return JSONResponse({"ok": False, "proxy_url": "", "detail": "Nenhum proxy configurado."})
    try:
        test = requests.get(
            "https://www.sofascore.com/api/v1/sport/football/scheduled-events/today",
            headers=HEADERS,
            proxies={"http": url, "https": url},
            verify=False,
            timeout=10.0
        )
        test.raise_for_status()
        return JSONResponse({"ok": True, "proxy_url": url, "gist_synced": None})
    except Exception as e:
        logging.warning(f"Proxy status check falhou: {e}")
        return JSONResponse({"ok": False, "proxy_url": url, "detail": str(e)})

@app.post("/proxy-update")
def proxy_update(body: ProxyUpdateRequest, authorization: str = Header(default=None)):
    verify_token(authorization)
    proxy_state["url"] = body.proxy_url
    logging.info(f"🔄 PROXY_URL atualizado para: {body.proxy_url or '(vazio)'}")
    gist_ok = gist_write(body.proxy_url)
    if not body.proxy_url:
        return JSONResponse({"ok": False, "proxy_url": "", "gist_synced": gist_ok, "detail": "Proxy removido."})
    try:
        test = requests.get(
            "https://www.sofascore.com/api/v1/sport/football/scheduled-events/today",
            headers=HEADERS,
            proxies={"http": body.proxy_url, "https": body.proxy_url},
            verify=False,
            timeout=10.0
        )
        test.raise_for_status()
        return JSONResponse({"ok": True, "proxy_url": body.proxy_url, "gist_synced": gist_ok})
    except Exception as e:
        logging.warning(f"Novo proxy falhou no teste: {e}")
        return JSONResponse({"ok": False, "proxy_url": body.proxy_url, "gist_synced": gist_ok, "detail": str(e)})


# --- Lógica do Proxy ---
@app.get("/{path:path}")
def proxy_request(path: str, request: Request):
    query_params = str(request.url.query)
    sofascore_url = f"https://www.sofascore.com/api/v1/{path}"
    if query_params:
        sofascore_url += f"?{query_params}"

    url = proxy_state["url"]
    proxies = {"http": url, "https": url} if url else None

    try:
        response = fetch_with_retry(sofascore_url, proxies)
        response.raise_for_status()
        return JSONResponse(content=response.json())
    except Exception as e:
        logging.error(f"Erro no proxy para {sofascore_url}: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)
