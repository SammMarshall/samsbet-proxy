from fastapi import FastAPI
from fastapi.responses import JSONResponse
import requests
import logging

# Configuração básica de logging
logging.basicConfig(level=logging.INFO)
app = FastAPI()

# Headers aprimorados para o "disfarce"
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
    try:
        response = requests.get(sofascore_url, headers=HEADERS)
        response.raise_for_status()
        return JSONResponse(content=response.json())
    except requests.exceptions.HTTPError as e:
        logging.error(f"Erro HTTP para {sofascore_url}: {e.response.status_code}")
        return JSONResponse(content={"error": str(e)}, status_code=e.response.status_code)
    except Exception as e:
        logging.error(f"Erro inesperado para {sofascore_url}: {e}")
        return JSONResponse(content={"error": "Erro interno no proxy"}, status_code=500)