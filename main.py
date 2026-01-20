from fastapi import FastAPI
import requests

app = FastAPI(title="API Temperatura Brasil")

CIDADES = {
    "saopaulo": (-23.5505, -46.6333),
    "riodejaneiro": (-22.9068, -43.1729),
    "brasilia": (-15.7939, -47.8828),
    "salvador": (-12.9777, -38.5016),
    "fortaleza": (-3.7319, -38.5267),
    "belohorizonte": (-19.9167, -43.9345),
}

@app.get("/")
def home():
    return {"status": "API de temperatura do Brasil ativa"}

@app.get("/temperatura/{cidade}")
def temperatura(cidade: str):
    if cidade not in CIDADES:
        return {"erro": "Cidade não encontrada"}

    lat, lon = CIDADES[cidade]
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m"
        "&timezone=auto"
    )

    dados = requests.get(url).json()

    return {
        "cidade": cidade,
        "temperatura": dados["current"]["temperature_2m"],
        "unidade": "°C"
    }
