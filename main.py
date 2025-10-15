# samsbet_proxy/main.py

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import requests  # <-- Voltamos para o bom e velho 'requests'
import logging
import os
import urllib3
import redis  # <-- Importa o redis
import json   # <-- Importa o json para serialização

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

# --- Conexão com o Redis ---
redis_client = None
try:
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        redis_client = redis.from_url(redis_url)
        redis_client.ping()
        logging.info("✅ Conectado ao cache Redis com sucesso!")
except Exception as e:
    logging.warning(f"⚠️ Falha ao conectar ao Redis: {e}")
    redis_client = None

# --- Lógica do Proxy com Cache ---
@app.get("/{path:path}")
def proxy_request(path: str, request: Request):
    query_params = str(request.url.query)
    sofascore_url = f"https://www.sofascore.com/api/v1/{path}"
    if query_params:
        sofascore_url += f"?{query_params}"

    # <<< LÓGICA DE CACHE >>>
    cache_key = sofascore_url # A própria URL é a chave perfeita
    if redis_client:
        cached_data = redis_client.get(cache_key)
        if cached_data:
            logging.info(f"✅ CACHE HIT para: {cache_key}")
            return JSONResponse(content=json.loads(cached_data))

    logging.info(f"❌ CACHE MISS para: {cache_key}. Buscando na API...")
    
    proxy_url = os.environ.get("PROXY_URL")
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    
    try:
        response = requests.get(sofascore_url, headers=HEADERS, proxies=proxies, verify=False, timeout=20.0)
        response.raise_for_status()
        response_data = response.json()

        # <<< SALVANDO NO CACHE >>>
        if redis_client:
            # Salva a resposta no Redis por 4 horas (14400 segundos)
            redis_client.setex(cache_key, 14400, json.dumps(response_data))

        return JSONResponse(content=response_data)
    except Exception as e:
        logging.error(f"Erro no proxy para {sofascore_url}: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)
