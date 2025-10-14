# samsbet_proxy/main.py

from fastapi import FastAPI, Request # <<< 1. Importar 'Request'
from fastapi.responses import JSONResponse
import httpx
import logging
import os
import urllib3

# Desativa os avisos de segurança
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

# Criamos um cliente assíncrono que será reutilizado
client = httpx.AsyncClient(verify=False)

@app.get("/{path:path}")
async def proxy_request(path: str, request: Request): # <<< 2. Adicionar o parâmetro 'request'
    
    # <<< 3. Montar a URL completa, incluindo os parâmetros de consulta >>>
    query_params = str(request.url.query)
    sofascore_url = f"https://www.sofascore.com/api/v1/{path}"
    if query_params:
        sofascore_url += f"?{query_params}"
    
    logging.info(f"Recebido pedido para: {sofascore_url}")
    
    proxy_url = os.environ.get("PROXY_URL")
    proxies = {"http://": proxy_url, "https://": proxy_url} if proxy_url else None
    
    logging.info(f"Usando proxy: {'Sim' if proxies else 'Não'}")

    try:
        response = await client.get(sofascore_url, headers=HEADERS, proxies=proxies, timeout=20.0)
        response.raise_for_status()
        return JSONResponse(content=response.json())
    except Exception as e:
        logging.error(f"Erro no proxy para {sofascore_url}: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)
