from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests

app = FastAPI(title="API Temperatura Brasil")

# Cidades com +1 milhão (lista inicial) + população estimada + coordenadas
# Obs: População = estimativa (pode ajustar depois). Temperatura = ao vivo (Open-Meteo).
CIDADES = {
    "saopaulo": {"nome": "São Paulo", "uf": "SP", "lat": -23.5505, "lon": -46.6333, "pop_estimada": 12300000},
    "riodejaneiro": {"nome": "Rio de Janeiro", "uf": "RJ", "lat": -22.9068, "lon": -43.1729, "pop_estimada": 6700000},
    "brasilia": {"nome": "Brasília", "uf": "DF", "lat": -15.7939, "lon": -47.8828, "pop_estimada": 3000000},
    "salvador": {"nome": "Salvador", "uf": "BA", "lat": -12.9777, "lon": -38.5016, "pop_estimada": 2900000},
    "fortaleza": {"nome": "Fortaleza", "uf": "CE", "lat": -3.7319, "lon": -38.5267, "pop_estimada": 2700000},
    "belohorizonte": {"nome": "Belo Horizonte", "uf": "MG", "lat": -19.9167, "lon": -43.9345, "pop_estimada": 2500000},
    "manaus": {"nome": "Manaus", "uf": "AM", "lat": -3.1190, "lon": -60.0217, "pop_estimada": 2200000},
    "curitiba": {"nome": "Curitiba", "uf": "PR", "lat": -25.4284, "lon": -49.2733, "pop_estimada": 1900000},
    "recife": {"nome": "Recife", "uf": "PE", "lat": -8.0476, "lon": -34.8770, "pop_estimada": 1600000},
    "goiania": {"nome": "Goiânia", "uf": "GO", "lat": -16.6869, "lon": -49.2648, "pop_estimada": 1500000},
    "belem": {"nome": "Belém", "uf": "PA", "lat": -1.4558, "lon": -48.4902, "pop_estimada": 1500000},
    "portoalegre": {"nome": "Porto Alegre", "uf": "RS", "lat": -30.0346, "lon": -51.2177, "pop_estimada": 1400000},
    "guarulhos": {"nome": "Guarulhos", "uf": "SP", "lat": -23.4543, "lon": -46.5337, "pop_estimada": 1400000},
    "campinas": {"nome": "Campinas", "uf": "SP", "lat": -22.9056, "lon": -47.0608, "pop_estimada": 1200000},
    "saoluis": {"nome": "São Luís", "uf": "MA", "lat": -2.5307, "lon": -44.3068, "pop_estimada": 1100000},
    "saogoncalo": {"nome": "São Gonçalo", "uf": "RJ", "lat": -22.8268, "lon": -43.0634, "pop_estimada": 1100000},
    "maceio": {"nome": "Maceió", "uf": "AL", "lat": -9.6658, "lon": -35.7353, "pop_estimada": 1000000},
    "duquedecaxias": {"nome": "Duque de Caxias", "uf": "RJ", "lat": -22.7858, "lon": -43.3117, "pop_estimada": 1000000},
}

def buscar_temperatura(lat: float, lon: float) -> float | None:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m"
        "&timezone=auto"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    dados = r.json()
    return dados.get("current", {}).get("temperature_2m")

@app.get("/")
def home():
    return {"status": "API de temperatura do Brasil ativa"}

# Lista de cidades (com população)
@app.get("/cidades")
def listar_cidades():
    cidades = []
    for slug, info in CIDADES.items():
        cidades.append({
            "slug": slug,
            "nome": info["nome"],
            "uf": info["uf"],
            "pop_estimada": info["pop_estimada"],
        })
    cidades.sort(key=lambda x: x["pop_estimada"], reverse=True)
    return {"total": len(cidades), "cidades": cidades}

# Temperatura de 1 cidade
@app.get("/temperatura/{cidade}")
def temperatura_uma(cidade: str):
    cidade = cidade.lower()
    if cidade not in CIDADES:
        return {"erro": "Cidade não encontrada", "dica": "Use /cidades para ver as disponíveis."}

    info = CIDADES[cidade]
    temp = buscar_temperatura(info["lat"], info["lon"])

    return {
        "slug": cidade,
        "cidade": info["nome"],
        "uf": info["uf"],
        "pop_estimada": info["pop_estimada"],
        "temperatura_c": temp,
        "unidade": "°C",
    }

# Temperatura de TODAS as cidades (tabela completa)
@app.get("/temperaturas")
def temperaturas_todas():
    resultados = []
    for slug, info in CIDADES.items():
        try:
            temp = buscar_temperatura(info["lat"], info["lon"])
        except Exception:
            temp = None

        resultados.append({
            "slug": slug,
            "cidade": info["nome"],
            "uf": info["uf"],
            "pop_estimada": info["pop_estimada"],
            "temperatura_c": temp,
            "unidade": "°C",
        })

    resultados.sort(key=lambda x: x["pop_estimada"], reverse=True)
    return {"total": len(resultados), "resultados": resultados}

# Página “tipo app” (abre no Safari/Chrome e mostra tudo em tabela)
@app.get("/app", response_class=HTMLResponse)
def pagina_app():
    return """
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Temperatura - Cidades +1 milhão</title>
  <style>
    body { font-family: -apple-system, system-ui, Arial; padding: 16px; }
    h1 { margin: 0 0 10px; font-size: 22px; }
    .muted { color: #666; font-size: 13px; }
    button { padding: 10px 14px; font-size: 16px; border-radius: 10px; }
    table { width: 100%; border-collapse: collapse; margin-top: 14px; }
    th, td { border-bottom: 1px solid #ddd; padding: 10px; text-align: left; }
    th { background: #f6f6f6; position: sticky; top: 0; }
  </style>
</head>
<body>
  <h1>Temperatura ao vivo (cidades +1 milhão)</h1>
  <div class="muted">População = estimativa. Temperatura = ao vivo (Open-Meteo).</div>
  <p><button onclick="carregar()">🔄 Atualizar</button></p>

  <div id="status" class="muted"></div>

  <table>
    <thead>
      <tr>
        <th>Cidade</th>
        <th>UF</th>
        <th>Habitantes (estim.)</th>
        <th>Temperatura (°C)</th>
      </tr>
    </thead>
    <tbody id="tb"></tbody>
  </table>

<script>
async function carregar() {
  const status = document.getElementById("status");
  const tb = document.getElementById("tb");
  tb.innerHTML = "";
  status.textContent = "Carregando...";

  try {
    const r = await fetch("/temperaturas");
    const data = await r.json();

    data.resultados.forEach(item => {
      const tr = document.createElement("tr");
      const temp = (item.temperatura_c === null || item.temperatura_c === undefined) ? "—" : item.temperatura_c;
      tr.innerHTML = `
        <td>${item.cidade}</td>
        <td>${item.uf}</td>
        <td>${Number(item.pop_estimada).toLocaleString("pt-BR")}</td>
        <td>${temp}</td>
      `;
      tb.appendChild(tr);
    });

    status.textContent = "Atualizado ✅";
  } catch (e) {
    status.textContent = "Erro ao carregar. Tente novamente.";
  }
}

carregar();
</script>
</body>
</html>
"""
