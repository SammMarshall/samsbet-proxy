# samsbet_proxy/main.py

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import requests
import logging
import os
import urllib3

# Desativa os avisos de segurança sobre não verificar SSL
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

@app.get("/{path:path}")
async def proxy_request(path: str):
    sofascore_url = f"https://www.sofascore.com/api/v1/{path}"
    logging.info(f"Recebido pedido para: {sofascore_url}")
    
    proxy_url = os.environ.get("PROXY_URL")
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    
    logging.info(f"Usando proxy: {'Sim' if proxies else 'Não'}")

    try:
        # <<< A BALA DE PRATA ESTÁ AQUI: verify=False >>>
        # Isso diz ao 'requests' para não se preocupar com a verificação SSL,
        # exatamente como a flag -k no curl.
        response = requests.get(sofascore_url, headers=HEADERS, proxies=proxies, verify=False)
        response.raise_for_status()
        return JSONResponse(content=response.json())
    except requests.exceptions.HTTPError as e:
        logging.error(f"Erro HTTP para {sofascore_url}: {e.response.status_code}")
        return JSONResponse(content={"error": str(e)}, status_code=e.response.status_code)
    except Exception as e:
        logging.error(f"Erro inesperado para {sofascore_url}: {e}")
        return JSONResponse(content={"error": "Erro interno no proxy"}, status_code=500)
