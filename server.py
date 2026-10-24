"""BSMS: proxy de radiossondagens observadas para estações brasileiras.
A fonte consultada é o arquivo de radiossondagens da Universidade de Wyoming.
A API nunca cria níveis meteorológicos sintéticos: se a fonte não fornecer dados
válidos, responde com erro HTTP e o front-end mantém o perfil vazio.
"""
from datetime import date
import csv
import io
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
import httpx

app = FastAPI(title="BSMS Radiossondagens Brasileiras", version="1.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restringir aos domínios do app ao publicar em produção
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Estações com identificadores WMO usados em arquivos históricos de radiossondagem.
# A presença na lista NÃO garante que exista observação na data solicitada.
STATIONS = {
    "82022": "Boa Vista",
    "82099": "Macapá",
    "82193": "Belém",
    "82281": "São Luís",
    "82332": "Manaus",
    "82397": "Fortaleza",
    "82599": "Natal",
    "82824": "Porto Velho",
    "82900": "Recife",
    "82917": "Rio Branco",
    "82965": "Alta Floresta",
    "82983": "Petrolina",
    "83208": "Vilhena",
    "83229": "Salvador",
    "83362": "Cuiabá",
    "83378": "Brasília",
    "83498": "Caravelas",
    "83566": "Confins / Belo Horizonte",
    "83612": "Campo Grande",
    "83649": "Vitória",
    "83746": "Rio de Janeiro / Galeão",
    "83780": "São Paulo / Congonhas",
    "83840": "Curitiba",
    "83899": "Florianópolis",
    "83937": "Santa Maria",
    "83971": "Porto Alegre",
}

# Interface atual da Universidade de Wyoming (a interface CGI antiga foi aposentada).
UWYO_URL = "https://weather.arcc.uwyo.edu/wsgi/sounding"

@app.get("/health")
def health():
    return {"ok": True, "service": "BSMS radiossondagens brasileiras", "stations": len(STATIONS)}

@app.get("/api/stations")
def stations():
    return [{"id": code, "name": name, "wmo": code} for code, name in STATIONS.items()]

def norm(s):
    return " ".join((s or "").strip().lower().replace("_", " ").replace("-", " ").split())

def parse_float(value):
    try:
        if value is None or str(value).strip() in ("", "-", "/////", "99999", "9999"):
            return None
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None

@app.get("/api/sounding")
async def sounding(
    station: str = Query(..., min_length=5, max_length=5, pattern=r"^\d{5}$"),
    date_utc: str = Query(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    hour: str = Query(..., pattern=r"^(00|12)$"),
):
    if station not in STATIONS:
        raise HTTPException(400, "Estação não está na lista brasileira do BSMS.")
    try:
        date.fromisoformat(date_utc)
    except ValueError:
        raise HTTPException(400, "Data inválida.")
    # Não consultar datas futuras.
    if date.fromisoformat(date_utc) > date.today():
        raise HTTPException(400, "A data selecionada está no futuro.")
    params = {
        "region": "samer",
        "TYPE": "TEXT:CSV",
        "YEAR": date_utc[:4],
        "MONTH": date_utc[5:7],
        # Current archive uses day/hour notation (DD/00Z or DD/12Z).
        "FROM": f"{date_utc[8:10]}/{hour}Z",
        "TO": f"{date_utc[8:10]}/{hour}Z",
        "STNM": station,
    }
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "BSMS-Radiossondagens/1.2", "Accept": "text/csv,text/plain,*/*"},
        ) as client:
            response = await client.get(UWYO_URL, params=params)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Falha ao conectar à fonte de radiossondagens: {exc}")
    if response.status_code != 200:
        raise HTTPException(502, f"A fonte de radiossondagens respondeu HTTP {response.status_code}.")
    body = response.text.strip()
    if not body or any(msg in body.lower() for msg in ("no data available", "no data found", "no sounding", "not found")):
        raise HTTPException(404, "Não há radiossondagem disponível para esta estação/data/hora.")
    try:
        # Some archive responses wrap the CSV table in HTML or prepend metadata.
        # Find the actual header line instead of assuming it is line 1.
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        header_index = None
        for i, line in enumerate(lines):
            candidate = norm(line.replace('"', '').replace(',', ' '))
            has_pressure = any(token in candidate for token in ("pres", "pressure"))
            has_temp = "temp" in candidate or "temperature" in candidate
            if has_pressure and has_temp and ("," in line or ";" in line):
                header_index = i
                break
        if header_index is None:
            # Fallback for standard comma-separated output.
            header_index = 0
        csv_text = "\\n".join(lines[header_index:])
        delimiter = ";" if csv_text.splitlines()[0].count(";") > csv_text.splitlines()[0].count(",") else ","
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError("Resposta sem cabeçalho CSV reconhecível.")
        columns = {norm(str(k).strip('"')): k for k in reader.fieldnames if k}
        def find(*names):
            for name in names:
                if name in columns:
                    return columns[name]
            for key, original in columns.items():
                if any(name == key or name in key for name in names):
                    return original
            return None

        pcol = find("pressure hpa", "pressure", "pres hpa", "pres", "p")
        tcol = find("temperature c", "temperature", "temp c", "temp", "t")
        tdcol = find("dew point temperature c", "dew point temperature", "dewpoint c", "dewpoint", "dwpt", "td")
        wdcol = find("wind direction degree", "wind direction", "direction degree", "drct", "wdir")
        wscol = find("wind speed m s", "wind speed", "speed m s", "sped", "wind speed kt", "sknt", "wspd")
        if not pcol or not tcol:
            raise ValueError("Cabeçalhos recebidos: " + ", ".join(columns.keys())[:300])
        levels = []
        for row in reader:
            p = parse_float(row.get(pcol))
            t = parse_float(row.get(tcol))
            td = parse_float(row.get(tdcol)) if tdcol else None
            if p is None or t is None or not (100 <= p <= 1100 and -120 <= t <= 70):
                continue
            item = {"pressure": p, "temperature": t}
            if td is not None and -120 <= td <= 70:
                item["dewpoint"] = td
            if wdcol:
                wd = parse_float(row.get(wdcol))
                if wd is not None and 0 <= wd <= 360:
                    item["wind_direction"] = wd
            if wscol:
                ws = parse_float(row.get(wscol))
                if ws is not None and 0 <= ws <= 200:
                    if "kt" in norm(wscol) or "sknt" in norm(wscol):
                        ws *= 0.514444
                    item["wind_speed"] = ws
            levels.append(item)
    except Exception as exc:
        raise HTTPException(502, f"Não foi possível interpretar o CSV da fonte atual: {exc}")
    levels.sort(key=lambda x: x["pressure"], reverse=True)
    if len(levels) < 2:
        raise HTTPException(404, "A fonte não retornou pelo menos dois níveis observados válidos.")
    if sum("dewpoint" in x for x in levels) < 2:
        raise HTTPException(404, "A fonte não forneceu ponto de orvalho observado suficiente; perfil não exibido para evitar inventar dados.")
    return {
        "station": station,
        "station_name": STATIONS[station],
        "observation_utc": f"{date_utc} {hour}Z",
        "source": "University of Wyoming Atmospheric Science Radiosonde Archive",
        "source_url": "https://weather.arcc.uwyo.edu/upperair/sounding.shtml",
        "levels": levels,
    }


# Servir o próprio HTML no mesmo domínio da API elimina a chamada incorreta a
# 127.0.0.1 quando o app é hospedado publicamente.
APP_DIR = Path(__file__).resolve().parent

@app.get("/", include_in_schema=False)
def home():
    index = APP_DIR / "index.html"
    if not index.exists():
        return JSONResponse({"error": "index.html não encontrado junto ao serviço."}, status_code=404)
    return FileResponse(index, media_type="text/html")

@app.get("/README.txt", include_in_schema=False)
def readme():
    path = APP_DIR / "README.txt"
    if path.exists():
        return FileResponse(path, media_type="text/plain; charset=utf-8")
    raise HTTPException(404, "README não encontrado.")
