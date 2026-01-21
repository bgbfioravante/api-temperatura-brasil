from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import requests
from typing import Dict, List, Optional, Tuple
import time

app = FastAPI(title="Temperatura Brasil (Top 100 por UF)")

# =========================
# CONFIG
# =========================
REFRESH_SECONDS = 10  # atualiza a cada 10 segundos (contador regressivo usa isso)
TOP_N = 100           # 100 maiores cidades por estado

IBGE_LOCALIDADES = "https://servicodados.ibge.gov.br/api/v1/localidades"
IBGE_INDICADORES = "https://servicodados.ibge.gov.br/api/v1/pesquisas/indicadores"
IND_POP_RESIDENTE = 29171  # indicador bastante usado como "População residente" (Cidades@)

OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

# caches em memória (pra ficar rápido no Render Free)
CACHE_UFS: Optional[dict] = None
CACHE_TOP100: Dict[str, dict] = {}   # uf -> {"ts":..., "data":[...]}
CACHE_COORDS: Dict[str, Tuple[float, float]] = {}  # "nome|uf" -> (lat, lon)

# =========================
# HELPERS
# =========================
def http_get_json(url: str, params: dict | None = None, timeout: int = 30):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def get_ufs_with_regions() -> List[dict]:
    """
    Retorna lista de UFs com regiao (nome), sigla, nome
    Cacheia em memória.
    """
    global CACHE_UFS
    if CACHE_UFS is not None:
        return CACHE_UFS["ufs"]

    estados = http_get_json(f"{IBGE_LOCALIDADES}/estados", timeout=30)
    ufs = []
    for e in estados:
        reg = (e.get("regiao") or {}).get("nome")
        ufs.append({"sigla": e.get("sigla"), "nome": e.get("nome"), "regiao": reg})

    # ordena
    ufs.sort(key=lambda x: (x["regiao"] or "", x["sigla"] or ""))
    CACHE_UFS = {"ufs": ufs, "ts": time.time()}
    return ufs

def get_municipios_uf(uf: str) -> List[dict]:
    """
    Lista todos os municípios de uma UF (IBGE Localidades).
    Cada item tem id e nome.
    """
    uf = uf.upper()
    return http_get_json(f"{IBGE_LOCALIDADES}/estados/{uf}/municipios", timeout=40)

def get_populacao_municipio(mun_id: int) -> Optional[int]:
    """
    Busca população (indicador 29171) para um município via API de indicadores do IBGE.
    Tenta pegar o valor mais recente disponível na resposta.
    """
    try:
        data = http_get_json(f"{IBGE_INDICADORES}/{IND_POP_RESIDENTE}/resultados/{mun_id}", timeout=30)
        if not data:
            return None

        # Estrutura comum: data[0]["res"][0]["res"] é um dict {ano: valor_str}
        series = None
        if isinstance(data, list) and data:
            item0 = data[0]
            res = item0.get("res")
            if isinstance(res, list) and res:
                series = res[0].get("res")

        if not isinstance(series, dict) or not series:
            return None

        # pega o ano mais recente
        anos = sorted(series.keys())
        ultimo_ano = anos[-1]
        val = series.get(ultimo_ano)

        if val is None:
            return None

        # alguns valores vêm como string com separadores
        val_str = str(val).strip().replace(".", "").replace(" ", "")
        # às vezes tem vírgula decimal (não deveria pra população, mas garantimos)
        val_str = val_str.replace(",", ".")
        # tenta int
        return int(float(val_str))
    except Exception:
        return None

def geocode_latlon(nome: str, uf: str) -> Optional[Tuple[float, float]]:
    """
    Usa geocoding do Open-Meteo para achar lat/lon.
    Cacheia em memória.
    """
    key = f"{nome}|{uf}".lower()
    if key in CACHE_COORDS:
        return CACHE_COORDS[key]

    # query que costuma funcionar bem
    q = f"{nome}, {uf}, Brazil"
    params = {"name": q, "count": 1, "format": "json"}
    try:
        data = http_get_json(OPEN_METEO_GEOCODE, params=params, timeout=25)
        results = data.get("results") or []
        if not results:
            return None
        lat = results[0].get("latitude")
        lon = results[0].get("longitude")
        if lat is None or lon is None:
            return None
        CACHE_COORDS[key] = (float(lat), float(lon))
        return CACHE_COORDS[key]
    except Exception:
        return None

def batch_temperaturas(latlons: List[Tuple[float, float]]) -> List[Optional[float]]:
    """
    Faz 1 chamada só pro Open-Meteo com lista de coordenadas.
    Retorna lista de temperaturas na mesma ordem.
    """
    if not latlons:
        return []

    lats = ",".join([str(x[0]) for x in latlons])
    lons = ",".join([str(x[1]) for x in latlons])

    params = {
        "latitude": lats,
        "longitude": lons,
        "current": "temperature_2m",
        "timezone": "auto",
    }

    try:
        data = http_get_json(OPEN_METEO_FORECAST, params=params, timeout=35)
        cur = data.get("current") or {}

        temps = cur.get("temperature_2m")
        # Quando é lista de coordenadas, normalmente vem array
        if isinstance(temps, list):
            out = []
            for t in temps:
                out.append(None if t is None else float(t))
            return out

        # fallback: se veio escalar (1 cidade)
        if temps is None:
            return [None] * len(latlons)
        return [float(temps)] + ([None] * (len(latlons) - 1))
    except Exception:
        return [None] * len(latlons)

def get_top100_cidades_uf(uf: str) -> List[dict]:
    """
    Monta lista das TOP 100 cidades do estado por população (IBGE).
    Cacheia por algumas horas pra não ficar pesado.
    """
    uf = uf.upper()

    # cache por 12h
    cached = CACHE_TOP100.get(uf)
    if cached and (time.time() - cached["ts"] < 12 * 3600):
        return cached["data"]

    municipios = get_municipios_uf(uf)

    # busca população (isso é pesado, mas só na primeira vez por UF por 12h)
    items = []
    for m in municipios:
        mid = m.get("id")
        nome = m.get("nome")
        if mid is None or nome is None:
            continue
        pop = get_populacao_municipio(int(mid))
        if pop is None:
            continue
        items.append({"id": int(mid), "nome": nome, "uf": uf, "pop": pop})

    # ordena por população desc e pega top 100
    items.sort(key=lambda x: x["pop"], reverse=True)
    top = items[:TOP_N]

    # garante coordenadas (geocoding) e guarda no cache
    # (coordenadas também ficam em cache separado)
    out = []
    for it in top:
        coords = geocode_latlon(it["nome"], uf)
        if coords is None:
            # ainda assim mantemos, mas sem coordenadas/temperatura
            out.append({**it, "lat": None, "lon": None})
        else:
            out.append({**it, "lat": coords[0], "lon": coords[1]})

    CACHE_TOP100[uf] = {"ts": time.time(), "data": out}
    return out

# =========================
# API ROUTES
# =========================
@app.get("/")
def home():
    return {"status": "API ativa", "dica": "Acesse /app"}

@app.get("/ufs")
def ufs():
    return {"total": len(get_ufs_with_regions()), "ufs": get_ufs_with_regions()}

@app.get("/top100")
def top100(uf: str):
    data = get_top100_cidades_uf(uf)
    return {"uf": uf.upper(), "total": len(data), "cidades": data}

@app.get("/temps_top100")
def temps_top100(uf: str):
    """
    Retorna TOP 100 do estado com temperaturas ao vivo (1 chamada batch).
    """
    uf = uf.upper()
    top = get_top100_cidades_uf(uf)

    latlons = []
    idx_map = []  # quais indices têm coords
    for i, it in enumerate(top):
        if it.get("lat") is not None and it.get("lon") is not None:
            latlons.append((it["lat"], it["lon"]))
            idx_map.append(i)

    temps = batch_temperaturas(latlons)

    # monta resposta alinhada com top
    out = []
    j = 0
    for i, it in enumerate(top):
        t = None
        if i in idx_map:
            t = temps[j] if j < len(temps) else None
            j += 1

        out.append({
            "nome": it["nome"],
            "uf": it["uf"],
            "pop": it["pop"],
            "temperatura_c": t,
        })

    return {"uf": uf, "total": len(out), "resultados": out}

# =========================
# APP (HTML)
# =========================
@app.get("/app", response_class=HTMLResponse)
def app_page():
    return f"""
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Temperatura Brasil - Top 100 por Estado</title>
  <style>
    body {{ font-family:-apple-system,system-ui,Arial; padding:16px; }}
    h1 {{ margin:0 0 8px; font-size:22px; }}
    .muted {{ color:#666; font-size:13px; line-height:1.35; }}
    .row {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }}
    .btn {{
      padding:9px 12px; font-size:14px; border-radius:999px;
      border:1px solid #ddd; background:#fff; cursor:pointer;
    }}
    .btn.active {{ border-color:#000; background:#000; color:#fff; }}
    .panel {{ margin-top:12px; padding:10px; border:1px solid #eee; border-radius:12px; background:#fafafa; }}
    .panel-title {{ font-weight:700; margin-bottom:8px; }}
    table {{ width:100%; border-collapse:collapse; margin-top:12px; }}
    th,td {{ border-bottom:1px solid #ddd; padding:10px; text-align:left; }}
    th {{ background:#f6f6f6; position:sticky; top:0; z-index:1; }}
    td.temp {{ font-weight:800; }}
    tbody tr {{ transition: background-color 200ms ease; }}
    .right {{ margin-left:10px; }}
  </style>
</head>
<body>
  <h1>Temperatura ao vivo — Top 100 por Estado</h1>
  <div class="muted">
    Clique em <b>Região</b> → clique em <b>Estado</b> → mostra as <b>100 maiores cidades</b> do estado (por população),
    ordenadas da <b>mais quente</b> para a <b>mais fria</b>.
    <br/>
    Atualiza automaticamente a cada <b>{REFRESH_SECONDS}s</b> e as cidades mudam de posição.
  </div>

  <div class="panel">
    <div class="panel-title">Regiões</div>
    <div class="row" id="regioes"></div>

    <div style="height:10px;"></div>
    <div class="panel-title">Estados</div>
    <div class="row" id="estados"></div>

    <div style="height:10px;"></div>
    <button class="btn" onclick="carregar(true)">🔄 Atualizar agora</button>
    <span id="status" class="muted right"></span>
    <span id="countdown" class="muted right"></span>
  </div>

  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Cidade</th>
        <th>UF</th>
        <th>População</th>
        <th>Temperatura (°C)</th>
      </tr>
    </thead>
    <tbody id="tb"></tbody>
  </table>

<script>
const REGIOES = ["Sul","Sudeste","Centro-Oeste","Norte","Nordeste"];
const REFRESH_SECONDS = {REFRESH_SECONDS};

let UFS = [];
let selecionadaRegiao = null;
let selecionadoUF = null;
let carregando = false;

// contador regressivo
let remaining = REFRESH_SECONDS;
function renderCountdown() {{
  const el = document.getElementById("countdown");
  if (!el) return;
  el.textContent = `Atualiza em: ${{remaining}}s`;
}}

setInterval(() => {{
  remaining -= 1;
  if (remaining < 0) remaining = REFRESH_SECONDS;
  renderCountdown();
}}, 1000);

// formatadores
function fmtPop(n) {{
  try {{ return Number(n).toLocaleString("pt-BR"); }} catch {{ return n; }}
}}
function fmtTemp(t) {{
  if (t === null || t === undefined) return "—";
  return Number(t).toFixed(1);
}}

// cores por temperatura: vermelho (quente) -> azul (frio) em HSL
function corPorTemp(temp, minT, maxT) {{
  if (temp === null || temp === undefined || isNaN(temp)) return "hsl(0 0% 96%)";
  if (maxT === minT) return "hsl(0 85% 88%)";
  const x = (temp - minT) / (maxT - minT);
  const clamped = Math.max(0, Math.min(1, x));
  const hue = 200 - (200 * clamped); // frio=200, quente=0
  return `hsl(${{hue}} 85% 88%)`;
}}

function renderRegioes() {{
  const root = document.getElementById("regioes");
  root.innerHTML = "";
  REGIOES.forEach(r => {{
    const b = document.createElement("button");
    b.className = "btn" + (selecionadaRegiao === r ? " active" : "");
    b.textContent = r;
    b.onclick = () => {{
      selecionadaRegiao = r;
      selecionadoUF = null;
      renderRegioes();
      renderEstados();
      limparTabela("Escolha um estado.");
    }};
    root.appendChild(b);
  }});
}}

function renderEstados() {{
  const root = document.getElementById("estados");
  root.innerHTML = "";

  const lista = UFS
    .filter(x => x.regiao === selecionadaRegiao)
    .sort((a,b) => (a.sigla || "").localeCompare(b.sigla || ""));

  lista.forEach(uf => {{
    const b = document.createElement("button");
    b.className = "btn" + (selecionadoUF === uf.sigla ? " active" : "");
    b.textContent = uf.sigla;
    b.onclick = () => {{
      selecionadoUF = uf.sigla;
      remaining = REFRESH_SECONDS; // reseta contador
      renderEstados();
      carregar(true);
    }};
    root.appendChild(b);
  }});

  if (lista.length === 0) {{
    const msg = document.createElement("div");
    msg.className = "muted";
    msg.textContent = "Selecione uma região.";
    root.appendChild(msg);
  }}
}}

function limparTabela(statusMsg) {{
  document.getElementById("tb").innerHTML = "";
  const status = document.getElementById("status");
  status.textContent = statusMsg || "";
}}

async function carregar(force=false) {{
  if (carregando && !force) return;
  carregando = true;

  const status = document.getElementById("status");
  const tb = document.getElementById("tb");
  status.textContent = "Carregando...";
  tb.innerHTML = "";

  try {{
    if (!selecionadoUF) {{
      status.textContent = "Escolha um estado.";
      carregando = false;
      return;
    }}

    // pega top100 + temperaturas ao vivo
    const r = await fetch(`/temps_top100?uf=${{encodeURIComponent(selecionadoUF)}}`, {{ cache: "no-store" }});
    const data = await r.json();
    let itens = data.resultados || [];

    // calcula min/max (para cores)
    const tempsValidas = itens
      .map(x => x.temperatura_c)
      .filter(t => t !== null && t !== undefined && !isNaN(t))
      .map(Number);

    const minT = tempsValidas.length ? Math.min(...tempsValidas) : 0;
    const maxT = tempsValidas.length ? Math.max(...tempsValidas) : 0;

    // ordena por temperatura desc (mais quente no topo)
    itens.sort((a,b) => {{
      const ta = (a.temperatura_c === null || a.temperatura_c === undefined) ? -9999 : Number(a.temperatura_c);
      const tbv = (b.temperatura_c === null || b.temperatura_c === undefined) ? -9999 : Number(b.temperatura_c);
      return tbv - ta;
    }});

    const frag = document.createDocumentFragment();
    itens.forEach((item, idx) => {{
      const tr = document.createElement("tr");
      const t = (item.temperatura_c === null || item.temperatura_c === undefined) ? null : Number(item.temperatura_c);
      tr.style.backgroundColor = corPorTemp(t, minT, maxT);

      tr.innerHTML = `
        <td><b>${{idx + 1}}</b></td>
        <td>${{item.nome}}</td>
        <td>${{item.uf}}</td>
        <td>${{fmtPop(item.pop)}}</td>
        <td class="temp">${{fmtTemp(item.temperatura_c)}}</td>
      `;
      frag.appendChild(tr);
    }});

    tb.appendChild(frag);

    status.textContent = "Atualizado ✅ " + new Date().toLocaleTimeString("pt-BR");
    remaining = REFRESH_SECONDS; // reseta contador quando atualiza
    renderCountdown();
  }} catch (e) {{
    status.textContent = "Erro ao carregar. Tente novamente.";
  }} finally {{
    carregando = false;
  }}
}}

async function init() {{
  // carrega UFs + regiões (IBGE)
  const r = await fetch("/ufs", {{ cache: "no-store" }});
  const data = await r.json();
  UFS = data.ufs || [];

  renderRegioes();
  renderEstados();
  renderCountdown();
  limparTabela("Escolha uma região e um estado.");
}}

init();

// auto refresh a cada 10s
setInterval(() => {{
  if (selecionadoUF) {{
    carregar(false);
  }}
}}, REFRESH_SECONDS * 1000);
</script>
</body>
</html>
"""

