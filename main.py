from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
import os
import io
import zipfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

app = FastAPI(title="Temperatura Brasil (Top 50 por Estado)")

# =========================
# CONFIG
# =========================
LIMIT_PADRAO = 50
REFRESH_CLIENT_SECONDS = 10          # a página atualiza a lista a cada 10s
TEMP_CACHE_TTL_SECONDS = 120         # temperatura real é renovada no servidor a cada 2 min (evita rate limit)
MAX_WORKERS = 12                     # paralelismo p/ buscar temperaturas
HTTP_TIMEOUT = 10

GEONAMES_BR_ZIP_URL = "https://download.geonames.org/export/dump/BR.zip"
GEONAMES_ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"

CACHE_DIR = "/tmp/geonames_br"
BR_TXT_PATH = os.path.join(CACHE_DIR, "BR.txt")
ADMIN1_PATH = os.path.join(CACHE_DIR, "admin1CodesASCII.txt")

# =========================
# MAPEAMENTOS (UF / REGIÕES)
# =========================
REGIOES = {
    "Norte": ["AC", "AP", "AM", "PA", "RO", "RR", "TO"],
    "Nordeste": ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
    "Centro-Oeste": ["DF", "GO", "MT", "MS"],
    "Sudeste": ["ES", "MG", "RJ", "SP"],
    "Sul": ["PR", "RS", "SC"],
}

UF_NOME = {
    "AC": "Acre",
    "AL": "Alagoas",
    "AP": "Amapá",
    "AM": "Amazonas",
    "BA": "Bahia",
    "CE": "Ceará",
    "DF": "Distrito Federal",
    "ES": "Espírito Santo",
    "GO": "Goiás",
    "MA": "Maranhão",
    "MT": "Mato Grosso",
    "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais",
    "PA": "Pará",
    "PB": "Paraíba",
    "PR": "Paraná",
    "PE": "Pernambuco",
    "PI": "Piauí",
    "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul",
    "RO": "Rondônia",
    "RR": "Roraima",
    "SC": "Santa Catarina",
    "SP": "São Paulo",
    "SE": "Sergipe",
    "TO": "Tocantins",
}

# GeoNames admin1 (BR.xx) -> UF
# Fonte: convenção do GeoNames para admin1 do Brasil
ADMIN1_TO_UF = {
    "01": "AC",
    "02": "AL",
    "03": "AP",
    "04": "AM",
    "05": "BA",
    "06": "CE",
    "07": "DF",
    "08": "ES",
    "10": "GO",
    "11": "MA",
    "13": "MT",
    "14": "MS",
    "15": "MG",
    "16": "PA",
    "17": "PB",
    "18": "PR",
    "20": "PI",
    "21": "RJ",
    "22": "RN",
    "23": "RS",
    "24": "RO",
    "25": "RR",
    "26": "SC",
    "27": "SP",
    "28": "SE",
    "29": "TO",
    "30": "PE",
}

# =========================
# DATA / CACHE EM MEMÓRIA
# =========================
_data_lock = threading.Lock()
_loaded = False

# Estrutura:
# CIDADES_POR_UF[UF] = list[city]
# city = { "id": int, "nome": str, "lat": float, "lon": float, "pop": int }
CIDADES_POR_UF = {uf: [] for uf in UF_NOME.keys()}

# temp_cache[geoname_id] = (temp_c, ts_epoch)
_temp_lock = threading.Lock()
temp_cache = {}

session = requests.Session()

# =========================
# HELPERS: DOWNLOAD + PARSE GEONAMES
# =========================
def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)

def _download_file(url: str, path: str):
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)

def _download_and_extract_geonames():
    _ensure_cache_dir()

    if not os.path.exists(ADMIN1_PATH):
        _download_file(GEONAMES_ADMIN1_URL, ADMIN1_PATH)

    if not os.path.exists(BR_TXT_PATH):
        # baixa BR.zip e extrai BR.txt
        r = session.get(GEONAMES_BR_ZIP_URL, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        # normalmente vem "BR.txt"
        with z.open("BR.txt") as f_in:
            with open(BR_TXT_PATH, "wb") as f_out:
                f_out.write(f_in.read())

def _load_data_if_needed():
    global _loaded
    if _loaded:
        return

    with _data_lock:
        if _loaded:
            return

        _download_and_extract_geonames()

        # limpa
        for uf in CIDADES_POR_UF:
            CIDADES_POR_UF[uf] = []

        # parse BR.txt (tab separated)
        # geoname format:
        # 0 geonameid, 1 name, 2 asciiname, 3 alternatenames, 4 lat, 5 lon,
        # 6 feature class, 7 feature code, 8 country code, 9 cc2,
        # 10 admin1, 11 admin2, 12 admin3, 13 admin4,
        # 14 population, 15 elevation, 16 dem, 17 timezone, 18 mod date
        with open(BR_TXT_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 19:
                    continue

                try:
                    geoname_id = int(parts[0])
                    name = parts[1]
                    lat = float(parts[4])
                    lon = float(parts[5])
                    fclass = parts[6]
                    country = parts[8]
                    admin1 = parts[10]  # "18" etc
                    pop = int(parts[14]) if parts[14] else 0
                except Exception:
                    continue

                # Só Brasil + locais habitados
                if country != "BR":
                    continue
                if fclass != "P":  # Populated place
                    continue
                if admin1 not in ADMIN1_TO_UF:
                    continue

                uf = ADMIN1_TO_UF[admin1]
                CIDADES_POR_UF[uf].append({
                    "id": geoname_id,
                    "nome": name,
                    "lat": lat,
                    "lon": lon,
                    "pop": pop
                })

        # Agora pega TOP 50 por população em cada UF
        for uf in CIDADES_POR_UF:
            CIDADES_POR_UF[uf].sort(key=lambda c: c["pop"], reverse=True)
            # mantém mais que 50 em memória pra poder trocar limit depois se quiser
            # mas por padrão, vamos usar no máximo 200 guardadas
            CIDADES_POR_UF[uf] = CIDADES_POR_UF[uf][:200]

        _loaded = True

# =========================
# TEMPERATURA (Open-Meteo) + CACHE
# =========================
def def _fetch_temps_batch_open_meteo(latlons: list[tuple[float, float]]) -> list[float | None]:
    """
    Busca temperaturas atuais para várias coordenadas em UMA chamada (mais estável).
    Retorna lista na mesma ordem de latlons.
    """
    if not latlons:
        return []

    lats = ",".join(str(lat) for lat, _ in latlons)
    lons = ",".join(str(lon) for _, lon in latlons)

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lats}&longitude={lons}"
        "&current=temperature_2m"
        "&timezone=auto"
    )
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        temps = (data.get("current") or {}).get("temperature_2m")

        # quando envia lista de coords, normalmente vem lista de temps
        if isinstance(temps, list):
            return [None if t is None else float(t) for t in temps]

        # fallback se vier só um valor
        if temps is None:
            return [None] * len(latlons)
        return [float(temps)] + ([None] * (len(latlons) - 1))
    except Exception:
        return [None] * len(latlons)


def _get_temps_for_cities(cities: list[dict]) -> list[dict]:
    """
    Usa cache + 1 chamada batch para buscar as temperaturas faltantes.
    """
    now = time.time()

    # 1) separa quem tem cache válido e quem precisa buscar
    results = [None] * len(cities)
    to_fetch = []
    to_fetch_idx = []

    with _temp_lock:
        for i, c in enumerate(cities):
            cid = c["id"]
            if cid in temp_cache:
                temp, ts = temp_cache[cid]
                if (now - ts) < TEMP_CACHE_TTL_SECONDS:
                    results[i] = temp
                    continue
            # sem cache válido
            to_fetch.append((c["lat"], c["lon"]))
            to_fetch_idx.append(i)

    # 2) busca batch só do que faltou
    fetched = _fetch_temps_batch_open_meteo(to_fetch)

    # 3) salva no cache e preenche resultados
    with _temp_lock:
        for j, i in enumerate(to_fetch_idx):
            c = cities[i]
            cid = c["id"]
            temp = fetched[j] if j < len(fetched) else None
            temp_cache[cid] = (temp, now)
            results[i] = temp

    # 4) monta retorno final
    out = []
    for i, c in enumerate(cities):
        out.append({
            "id": c["id"],
            "cidade": c["nome"],
            "uf": None,  # preenchido depois
            "habitantes": c["pop"],
            "lat": c["lat"],
            "lon": c["lon"],
            "temperatura": results[i],
            "unidade": "°C",
        })
    return out
(lat: float, lon: float) -> float | None:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m"
        "&timezone=auto"
    )
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("current", {}).get("temperature_2m")

def _get_temp_cached(city: dict) -> float | None:
    now = time.time()
    cid = city["id"]

    with _temp_lock:
        if cid in temp_cache:
            temp, ts = temp_cache[cid]
            if (now - ts) < TEMP_CACHE_TTL_SECONDS:
                return temp

    # se não tem cache válido, busca
    try:
        temp = _fetch_temp_open_meteo(city["lat"], city["lon"])
    except Exception:
        temp = None

    with _temp_lock:
        temp_cache[cid] = (temp, now)

    return temp

def def _fetch_temps_batch_open_meteo(latlons: list[tuple[float, float]]) -> list[float | None]:
    """
    Busca temperaturas atuais para várias coordenadas em UMA chamada (mais estável).
    Retorna lista na mesma ordem de latlons.
    """
    if not latlons:
        return []

    lats = ",".join(str(lat) for lat, _ in latlons)
    lons = ",".join(str(lon) for _, lon in latlons)

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lats}&longitude={lons}"
        "&current=temperature_2m"
        "&timezone=auto"
    )
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        temps = (data.get("current") or {}).get("temperature_2m")

        # quando envia lista de coords, normalmente vem lista de temps
        if isinstance(temps, list):
            return [None if t is None else float(t) for t in temps]

        # fallback se vier só um valor
        if temps is None:
            return [None] * len(latlons)
        return [float(temps)] + ([None] * (len(latlons) - 1))
    except Exception:
        return [None] * len(latlons)


def _get_temps_for_cities(cities: list[dict]) -> list[dict]:
    """
    Usa cache + 1 chamada batch para buscar as temperaturas faltantes.
    """
    now = time.time()

    # 1) separa quem tem cache válido e quem precisa buscar
    results = [None] * len(cities)
    to_fetch = []
    to_fetch_idx = []

    with _temp_lock:
        for i, c in enumerate(cities):
            cid = c["id"]
            if cid in temp_cache:
                temp, ts = temp_cache[cid]
                if (now - ts) < TEMP_CACHE_TTL_SECONDS:
                    results[i] = temp
                    continue
            # sem cache válido
            to_fetch.append((c["lat"], c["lon"]))
            to_fetch_idx.append(i)

    # 2) busca batch só do que faltou
    fetched = _fetch_temps_batch_open_meteo(to_fetch)

    # 3) salva no cache e preenche resultados
    with _temp_lock:
        for j, i in enumerate(to_fetch_idx):
            c = cities[i]
            cid = c["id"]
            temp = fetched[j] if j < len(fetched) else None
            temp_cache[cid] = (temp, now)
            results[i] = temp

    # 4) monta retorno final
    out = []
    for i, c in enumerate(cities):
        out.append({
            "id": c["id"],
            "cidade": c["nome"],
            "uf": None,  # preenchido depois
            "habitantes": c["pop"],
            "lat": c["lat"],
            "lon": c["lon"],
            "temperatura": results[i],
            "unidade": "°C",
        })
    return out
(cities: list[dict]) -> list[dict]:
    # busca com paralelismo, respeitando cache
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fut_map = {ex.submit(_get_temp_cached, c): c for c in cities}
        for fut in as_completed(fut_map):
            c = fut_map[fut]
            temp = None
            try:
                temp = fut.result()
            except Exception:
                temp = None

            results.append({
                "id": c["id"],
                "cidade": c["nome"],
                "uf": None,
                "habitantes": c["pop"],
                "lat": c["lat"],
                "lon": c["lon"],
                "temperatura": temp,
                "unidade": "°C"
            })

    return results

# =========================
# API ENDPOINTS
# =========================
@app.get("/")
def home():
    return {"status": "API Temperatura Brasil ativa", "app": "/app"}

@app.get("/api/regions")
def api_regions():
    return {"regions": list(REGIOES.keys())}

@app.get("/api/states")
def api_states(region: str = Query(...)):
    if region not in REGIOES:
        return JSONResponse({"erro": "Região inválida"}, status_code=400)
    ufs = REGIOES[region]
    return {
        "region": region,
        "states": [{"uf": uf, "nome": UF_NOME[uf]} for uf in ufs]
    }

@app.get("/api/cities")
def api_cities(
    uf: str = Query(..., min_length=2, max_length=2),
    limit: int = Query(LIMIT_PADRAO, ge=1, le=200)
):
    uf = uf.upper()
    if uf not in UF_NOME:
        return JSONResponse({"erro": "UF inválida"}, status_code=400)

    _load_data_if_needed()

    cities = CIDADES_POR_UF.get(uf, [])[:limit]
    data = _get_temps_for_cities(cities)

    # adiciona uf e ordena por temperatura (None vai pro final)
    for item in data:
        item["uf"] = uf

    data.sort(key=lambda x: (x["temperatura"] is None, -(x["temperatura"] or -9999)))
    return {
        "uf": uf,
        "estado": UF_NOME[uf],
        "limit": limit,
        "count": len(data),
        "data": data
    }

# =========================
# APP HTML
# =========================
APP_HTML = f"""
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Temperatura Brasil</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 16px; }}
    h1 {{ margin: 0 0 8px 0; }}
    .row {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }}
    .btn {{
      padding: 10px 12px; border: 1px solid #ccc; border-radius: 8px;
      background: #f7f7f7; cursor: pointer; font-weight: 600;
    }}
    .btn.active {{ background: #111; color: #fff; border-color: #111; }}
    .btn.small {{ padding: 8px 10px; font-weight: 600; }}
    .info {{ margin: 10px 0; display:flex; align-items:center; gap:10px; flex-wrap: wrap; }}
    .badge {{ padding: 6px 10px; border-radius: 999px; background:#eee; font-weight: 600; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    th, td {{ border-bottom: 1px solid #eee; padding: 10px; text-align: left; }}
    th {{ background: #fafafa; position: sticky; top: 0; }}
    .tempCell {{ border-radius: 8px; padding: 8px 10px; display:inline-block; min-width: 70px; text-align:center; font-weight: 700; }}
    .muted {{ color:#666; }}
  </style>
</head>
<body>
  <h1>Temperatura ao vivo (Top 50 por Estado)</h1>
  <div class="muted">Ordena da mais quente → mais fria. Atualiza a cada {REFRESH_CLIENT_SECONDS}s (contador).</div>

  <div class="row" id="regionButtons"></div>
  <div class="row" id="stateButtons"></div>

  <div class="info">
    <span class="badge" id="selectedLabel">Selecione uma região</span>
    <button class="btn small" id="refreshNow">Atualizar agora</button>
    <span class="badge">Atualiza em: <span id="countdown">{REFRESH_CLIENT_SECONDS}</span>s</span>
  </div>

  <div id="tableWrap"></div>

<script>
const REFRESH_SECONDS = {REFRESH_CLIENT_SECONDS};
let selectedRegion = null;
let selectedUF = null;
let timer = null;
let countdown = REFRESH_SECONDS;

function el(id) {{ return document.getElementById(id); }}

function setActive(containerId, value) {{
  const box = el(containerId);
  [...box.querySelectorAll("button")].forEach(b => {{
    b.classList.toggle("active", b.dataset.value === value);
  }});
}}

function fmtPop(n) {{
  if (n === null || n === undefined) return "-";
  return n.toLocaleString("pt-BR");
}}

function clamp(x, a, b) {{ return Math.max(a, Math.min(b, x)); }}

function tempColor(temp, tMin, tMax) {{
  if (temp === null || temp === undefined) return "hsl(0, 0%, 90%)";
  if (tMax === tMin) return "hsl(0, 80%, 70%)";
  const p = (temp - tMin) / (tMax - tMin); // 0..1
  // azul (220) -> vermelho (0)
  const hue = 220 - 220 * clamp(p, 0, 1);
  return `hsl(${hue}, 85%, 75%)`;
}}

async function loadRegions() {{
  const r = await fetch("/api/regions");
  const j = await r.json();
  const container = el("regionButtons");
  container.innerHTML = "";
  j.regions.forEach(region => {{
    const b = document.createElement("button");
    b.className = "btn";
    b.textContent = region;
    b.dataset.value = region;
    b.onclick = () => selectRegion(region);
    container.appendChild(b);
  }});
}}

async function selectRegion(region) {{
  selectedRegion = region;
  selectedUF = null;
  setActive("regionButtons", region);
  el("selectedLabel").textContent = `Região: ${region} — selecione um estado`;
  el("tableWrap").innerHTML = "";

  const r = await fetch(`/api/states?region=${encodeURIComponent(region)}`);
  const j = await r.json();

  const container = el("stateButtons");
  container.innerHTML = "";
  j.states.forEach(s => {{
    const b = document.createElement("button");
    b.className = "btn";
    b.textContent = `${s.nome} (${s.uf})`;
    b.dataset.value = s.uf;
    b.onclick = () => selectState(s.uf, s.nome);
    container.appendChild(b);
  }});
}}

async function selectState(uf, nome) {{
  selectedUF = uf;
  setActive("stateButtons", uf);
  el("selectedLabel").textContent = `Região: ${selectedRegion} — Estado: ${nome} (${uf})`;

  await refreshCities();
  restartTimer();
}}

function restartTimer() {{
  if (timer) clearInterval(timer);
  countdown = REFRESH_SECONDS;
  el("countdown").textContent = countdown;

  timer = setInterval(async () => {{
    countdown--;
    if (countdown <= 0) {{
      countdown = REFRESH_SECONDS;
      if (selectedUF) {{
        await refreshCities();
      }}
    }}
    el("countdown").textContent = countdown;
  }}, 1000);
}}

async function refreshCities() {{
  if (!selectedUF) return;

  const url = `/api/cities?uf=${encodeURIComponent(selectedUF)}&limit=50`;
  const r = await fetch(url);
  const j = await r.json();

  const data = j.data || [];
  const temps = data.map(x => x.temperatura).filter(t => t !== null && t !== undefined);
  const tMin = temps.length ? Math.min(...temps) : 0;
  const tMax = temps.length ? Math.max(...temps) : 1;

  let html = `
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Cidade</th>
          <th>UF</th>
          <th>Habitantes (aprox.)</th>
          <th>Temperatura (°C)</th>
        </tr>
      </thead>
      <tbody>
  `;

  data.forEach((x, idx) => {{
    const temp = x.temperatura;
    const bg = tempColor(temp, tMin, tMax);
    const tTxt = (temp === null || temp === undefined) ? "-" : temp.toFixed(1);
    html += `
      <tr>
        <td>${idx + 1}</td>
        <td>${x.cidade}</td>
        <td>${x.uf}</td>
        <td>${fmtPop(x.habitantes)}</td>
        <td><span class="tempCell" style="background:${bg}">${tTxt}</span></td>
      </tr>
    `;
  }});

  html += "</tbody></table>";
  el("tableWrap").innerHTML = html;
}}

el("refreshNow").onclick = async () => {{
  if (!selectedUF) return;
  await refreshCities();
  countdown = REFRESH_SECONDS;
  el("countdown").textContent = countdown;
}};

loadRegions();
</script>
</body>
</html>
"""

@app.get("/app", response_class=HTMLResponse)
def app_page():
    return HTMLResponse(APP_HTML)
