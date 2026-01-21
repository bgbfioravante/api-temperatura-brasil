from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import requests
import time

app = FastAPI(title="Temperatura Brasil — Top 50 por Estado")

# =========================
# CONFIG
# =========================
REFRESH_SECONDS = 10  # atualiza a cada 10s
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

REGIOES = ["Sul", "Sudeste", "Centro-Oeste", "Norte", "Nordeste"]

ESTADOS_POR_REGIAO = {
    "Sul": ["PR", "SC", "RS"],
    "Sudeste": ["SP", "RJ", "MG", "ES"],
    "Centro-Oeste": ["DF", "GO", "MT", "MS"],
    "Norte": ["AC", "AP", "AM", "PA", "RO", "RR", "TO"],
    "Nordeste": ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
}

# Código IBGE do estado (N3 no SIDRA)
UF_TO_N3 = {
    "RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17,
    "MA": 21, "PI": 22, "CE": 23, "RN": 24, "PB": 25, "PE": 26, "AL": 27, "SE": 28, "BA": 29,
    "MG": 31, "ES": 32, "RJ": 33, "SP": 35,
    "PR": 41, "SC": 42, "RS": 43,
    "MS": 50, "MT": 51, "GO": 52, "DF": 53
}

# =========================
# CACHE (para não estourar APIs)
# =========================
CACHE = {
    "municipios_por_uf": {},     # UF -> list[{code_ibge, nome}]
    "pop_por_uf": {},            # UF -> dict[nome_cidade] = pop
    "geo_por_cidade_uf": {},     # (uf, nome) -> (lat, lon)
    "top50_por_uf": {},          # UF -> list[{nome, pop, lat, lon}]
    "top50_last_update": {},     # UF -> timestamp
}


# =========================
# FUNÇÕES: IBGE (municípios)
# =========================
def obter_municipios_ibge_por_uf(uf: str):
    """
    IBGE Localidades: lista municípios de um estado por UF.
    Retorna lista com nome e id (código do município).
    """
    uf = uf.upper()
    if uf in CACHE["municipios_por_uf"]:
        return CACHE["municipios_por_uf"][uf]

    # endpoint oficial de localidades (IBGE)
    # OBS: Esse endpoint retorna municípios e ids.
    url = f"https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()

    municipios = [{"code_ibge": str(m["id"]), "nome": m["nome"]} for m in data]
    CACHE["municipios_por_uf"][uf] = municipios
    return municipios


# =========================
# FUNÇÕES: POPULAÇÃO (SIDRA)
# =========================
def obter_populacao_sidra_por_uf(uf: str):
    """
    Puxa população por município via API SIDRA.
    Tabela 6579 é usada frequentemente para estimativas de população.
    Se a API variar, a parte de 'pop' pode falhar — nesse caso eu já deixo fallback.
    """
    uf = uf.upper()
    if uf in CACHE["pop_por_uf"]:
        return CACHE["pop_por_uf"][uf]

    n3 = UF_TO_N3.get(uf)
    if not n3:
        return {}

    # SIDRA (tentativa padrão):
    # t/6579 (estimativas), n6 municípios "all" filtrando por n3 (UF)
    # v/9324 (ou variável equivalente). Como pode mudar, tratamos com fallback.
    sidra_url = f"https://apisidra.ibge.gov.br/values/t/6579/n6/all/v/all/p/last?formato=json"
    r = requests.get(sidra_url, timeout=30)
    r.raise_for_status()
    arr = r.json()

    # Filtra pelo UF dentro do campo "Município" (às vezes vem com UF), ou por código IBGE (melhor)
    # Como o retorno do SIDRA varia, aqui a gente tenta “melhor esforço”.
    pop_map = {}

    # arr[0] costuma ser cabeçalho
    for row in arr[1:]:
        # Tentativas comuns de chaves:
        municipio = row.get("Município") or row.get("Município (Código)") or row.get("Município - código") or row.get("Município - Nome")
        valor = row.get("Valor") or row.get("V") or row.get("valor")

        # Alguns retornos trazem "Unidade da Federação (Código)"
        uf_code = row.get("Unidade da Federação (Código)") or row.get("UF (Código)") or row.get("Unidade da Federação")

        # Se tiver UF código, usamos:
        if uf_code and str(uf_code).strip() != str(n3):
            continue

        if municipio and valor:
            try:
                pop = int(float(str(valor).replace(".", "").replace(",", ".")))
                pop_map[str(municipio).strip()] = pop
            except:
                pass

    CACHE["pop_por_uf"][uf] = pop_map
    return pop_map


# =========================
# FUNÇÕES: GEO (Open-Meteo Geocoding)
# =========================
def geocode_city(uf: str, nome: str):
    key = (uf.upper(), nome.lower().strip())
    if key in CACHE["geo_por_cidade_uf"]:
        return CACHE["geo_por_cidade_uf"][key]

    params = {
        "name": nome,
        "count": 10,
        "language": "pt",
        "format": "json"
    }
    r = requests.get(GEOCODING_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    # tenta escolher resultado no Brasil
    results = data.get("results") or []
    best = None
    for it in results:
        if (it.get("country_code") == "BR") or (it.get("country") == "Brazil") or (it.get("country") == "Brasil"):
            best = it
            break

    if not best and results:
        best = results[0]

    if not best:
        CACHE["geo_por_cidade_uf"][key] = None
        return None

    lat = best.get("latitude")
    lon = best.get("longitude")
    if lat is None or lon is None:
        CACHE["geo_por_cidade_uf"][key] = None
        return None

    CACHE["geo_por_cidade_uf"][key] = (float(lat), float(lon))
    return (float(lat), float(lon))


# =========================
# FUNÇÕES: TEMPERATURA (Open-Meteo Forecast)
# =========================
def buscar_temperatura(lat: float, lon: float):
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m",
        "timezone": "auto"
    }
    r = requests.get(FORECAST_URL, params=params, timeout=20)
    r.raise_for_status()
    dados = r.json()
    return dados["current"]["temperature_2m"]


# =========================
# MONTAR TOP 50 POR UF (cacheado)
# =========================
def montar_top50_uf(uf: str):
    uf = uf.upper()

    # atualiza top50 no máximo a cada 6h (pode ajustar)
    now = time.time()
    last = CACHE["top50_last_update"].get(uf, 0)
    if uf in CACHE["top50_por_uf"] and (now - last) < 6 * 3600:
        return CACHE["top50_por_uf"][uf]

    municipios = obter_municipios_ibge_por_uf(uf)
    pop_map = obter_populacao_sidra_por_uf(uf)

    # se pop_map vier vazio (falha da SIDRA), a lista não consegue “maiores”
    # então faz fallback por ordem alfabética (melhor do que nada)
    lista = []
    for m in municipios:
        nome = m["nome"]
        pop = pop_map.get(nome) or pop_map.get(f"{nome} ({uf})") or 0

        geo = geocode_city(uf, nome)
        if not geo:
            continue

        lat, lon = geo
        lista.append({"nome": nome, "uf": uf, "pop": int(pop), "lat": lat, "lon": lon})

    # Ordena por pop desc e pega top 50
    lista.sort(key=lambda x: x["pop"], reverse=True)
    top50 = lista[:50]

    CACHE["top50_por_uf"][uf] = top50
    CACHE["top50_last_update"][uf] = now
    return top50


# =========================
# API ROUTES
# =========================
@app.get("/")
def home():
    return {"status": "API de temperatura do Brasil ativa"}

@app.get("/regioes")
def regioes():
    return {"regioes": REGIOES}

@app.get("/estados")
def estados(regiao: str):
    if regiao not in ESTADOS_POR_REGIAO:
        return JSONResponse({"erro": "Região inválida"}, status_code=400)
    return {"regiao": regiao, "estados": ESTADOS_POR_REGIAO[regiao]}

@app.get("/top50")
def top50(uf: str):
    top = montar_top50_uf(uf)
    return {"uf": uf.upper(), "total": len(top), "cidades": top}

@app.get("/temperaturas")
def temperaturas(uf: str):
    top = montar_top50_uf(uf)

    resultado = []
    for c in top:
        try:
            temp = buscar_temperatura(c["lat"], c["lon"])
        except Exception:
            temp = None

        resultado.append({
            "nome": c["nome"],
            "uf": c["uf"],
            "pop": c["pop"],
            "temperatura": temp,
            "unidade": "°C"
        })

    # Ordena: mais quente no topo, None no fim
    resultado.sort(key=lambda x: (x["temperatura"] is None, -(x["temperatura"] or -9999)))
    return {"uf": uf.upper(), "total": len(resultado), "cidades": resultado}


# =========================
# APP HTML
# =========================
@app.get("/app", response_class=HTMLResponse)
def app_page():
    return f"""
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Temperatura Brasil</title>
<style>
  body {{
    font-family: Arial, sans-serif;
    margin: 18px;
  }}
  .row {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom: 10px; }}
  button {{
    padding: 10px 12px;
    border: 1px solid #ddd;
    background: #f6f6f6;
    border-radius: 10px;
    cursor: pointer;
  }}
  button.active {{
    background: #111;
    color: #fff;
  }}
  .pill {{
    display:inline-block;
    padding: 6px 10px;
    border-radius: 999px;
    background: #eee;
    margin-left: 8px;
    font-size: 12px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 12px;
  }}
  th, td {{
    text-align: left;
    padding: 10px;
    border-bottom: 1px solid #eee;
  }}
  th {{
    background: #fafafa;
    position: sticky;
    top: 0;
  }}
  .muted {{ color:#666; font-size: 13px; }}
  .topline {{
    display:flex; align-items:center; justify-content:space-between; gap:10px;
  }}
  .counter {{
    font-weight: bold;
  }}
</style>
</head>
<body>
  <div class="topline">
    <h2>Temperatura ao vivo — Top 50 por Estado</h2>
    <div class="counter">Atualiza em: <span id="count">{REFRESH_SECONDS}</span>s</div>
  </div>
  <div class="muted">
    Clique em uma região → escolha o estado → lista ordena do mais quente para o mais frio e atualiza automaticamente.
  </div>

  <h3>Regiões</h3>
  <div class="row" id="regioes"></div>

  <h3>Estados <span class="pill" id="regiaoAtual">Nenhuma</span></h3>
  <div class="row" id="estados"></div>

  <h3>Ranking <span class="pill" id="ufAtual">Nenhum</span></h3>
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
    <tbody id="tbody"></tbody>
  </table>

<script>
  const REFRESH = {REFRESH_SECONDS};
  let countdown = REFRESH;
  let ufSelecionada = null;
  let regiaoSelecionada = null;
  let timer = null;

  function tempColor(t) {{
    // gradiente simples: quente = vermelho, frio = azul
    // faixa: -5 a 40 (ajustável)
    const min = -5, max = 40;
    if (t === null || t === undefined) return "#ddd";
    let x = (t - min) / (max - min);
    x = Math.max(0, Math.min(1, x));
    // x=1 vermelho, x=0 azul
    const r = Math.round(255 * x);
    const g = Math.round(80 + (120 * (1-x)));
    const b = Math.round(255 * (1-x));
    return `rgb(${{r}}, ${{g}}, ${{b}})`;
  }}

  function setActive(containerId, valueAttr, value) {{
    const el = document.getElementById(containerId);
    Array.from(el.querySelectorAll("button")).forEach(btn => {{
      btn.classList.toggle("active", btn.getAttribute(valueAttr) === value);
    }});
  }}

  async function carregarRegioes() {{
    const r = await fetch("/regioes");
    const data = await r.json();
    const box = document.getElementById("regioes");
    box.innerHTML = "";
    data.regioes.forEach(reg => {{
      const b = document.createElement("button");
      b.textContent = reg;
      b.setAttribute("data-reg", reg);
      b.onclick = () => selecionarRegiao(reg);
      box.appendChild(b);
    }});
  }}

  async function selecionarRegiao(reg) {{
    regiaoSelecionada = reg;
    document.getElementById("regiaoAtual").textContent = reg;
    ufSelecionada = null;
    document.getElementById("ufAtual").textContent = "Nenhum";
    document.getElementById("tbody").innerHTML = "";
    document.getElementById("status").textContent = "Escolha um estado...";

    setActive("regioes", "data-reg", reg);
    await carregarEstados(reg);
  }}

  async function carregarEstados(reg) {{
    const r = await fetch(`/estados?regiao=${{encodeURIComponent(reg)}}`);
    const data = await r.json();
    const box = document.getElementById("estados");
    box.innerHTML = "";
    data.estados.forEach(uf => {{
      const b = document.createElement("button");
      b.textContent = uf;
      b.setAttribute("data-uf", uf);
      b.onclick = () => selecionarUF(uf);
      box.appendChild(b);
    }});
  }}

  async function selecionarUF(uf) {{
    ufSelecionada = uf;
    document.getElementById("ufAtual").textContent = uf;
    setActive("estados", "data-uf", uf);
    document.getElementById("status").textContent = "Carregando temperaturas (pode demorar um pouco na primeira vez)...";

    // primeira carga imediata
    await atualizarTabela();

    // reinicia contador e timer
    countdown = REFRESH;
    document.getElementById("count").textContent = countdown;

    if (timer) clearInterval(timer);
    timer = setInterval(async () => {{
      countdown -= 1;
      if (countdown <= 0) {{
        countdown = REFRESH;
        await atualizarTabela();
      }}
      document.getElementById("count").textContent = countdown;
    }}, 1000);
  }}

  async function atualizarTabela() {{
    if (!ufSelecionada) return;
    try {{
      const r = await fetch(`/temperaturas?uf=${{encodeURIComponent(ufSelecionada)}}`);
      const data = await r.json();
      const tbody = document.getElementById("tbody");
      tbody.innerHTML = "";

      data.cidades.forEach(c => {{
        const tr = document.createElement("tr");

        const tdCidade = document.createElement("td");
        tdCidade.textContent = c.nome;

        const tdUF = document.createElement("td");
        tdUF.textContent = c.uf;

        const tdPop = document.createElement("td");
        tdPop.textContent = (c.pop || 0).toLocaleString("pt-BR");

        const tdTemp = document.createElement("td");
        tdTemp.textContent = (c.temperatura === null || c.temperatura === undefined) ? "—" : c.temperatura.toFixed(1);
        tdTemp.style.background = tempColor(c.temperatura);
        tdTemp.style.borderRadius = "8px";

        tr.appendChild(tdCidade);
        tr.appendChild(tdUF);
        tr.appendChild(tdPop);
        tr.appendChild(tdTemp);

        tbody.appendChild(tr);
      }});

      const okCount = data.cidades.filter(x => x.temperatura !== null && x.temperatura !== undefined).length;
      document.getElementById("status").textContent =
        `Atualizado ✓ (temperaturas carregadas: ${{okCount}}/${{data.cidades.length}})`;

    }} catch (e) {{
      document.getElementById("status").textContent = "Erro ao buscar dados. Tente novamente.";
    }}
  }}

  // init
  carregarRegioes();
  document.getElementById("status").textContent = "Escolha uma região...";
</script>
</body>
</html>
"""


# =========================
# health
# =========================
@app.get("/health")
def health():
    return {"ok": True}
