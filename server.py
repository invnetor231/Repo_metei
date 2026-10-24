"""BSMS: proxy de radiossondagens observadas para estações brasileiras.
A fonte consultada é o arquivo de radiossondagens da Universidade de Wyoming.
A API nunca cria níveis meteorológicos sintéticos: se a fonte não fornecer dados
válidos, responde com erro HTTP e o front-end mantém o perfil vazio.
"""
from datetime import date
import re
from html.parser import HTMLParser
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
import httpx

app = FastAPI(title="BSMS Radiossondagens Brasileiras", version="1.3.0")
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
UWYO_URL = "https://weather.uwyo.edu/wsgi/sounding"

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
    # A interface WSGI atual usa datetime, id e type. Os parâmetros antigos
    # YEAR/MONTH/FROM/TO/STNM provocam HTTP 400 nesse endpoint.
    params = {
        "datetime": f"{date_utc} {hour}:00:00",
        "id": station,
        "type": "TEXT:LIST",
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
        detail = response.text[:250].replace("\n", " ").strip()
        raise HTTPException(502, f"A fonte de radiossondagens respondeu HTTP {response.status_code}. {detail}")

    # A fonte retorna a tabela TEXT:LIST dentro de HTML/pre; interpretar as
    # colunas posicionais documentadas (PRES HGHT TEMP DWPT ... DRCT SKNT).
    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
        def handle_data(self, data):
            self.parts.append(data)

    extractor = TextExtractor()
    extractor.feed(response.text)
    body = "\n".join(extractor.parts).strip()
    if not body or any(msg in body.lower() for msg in ("no data available", "no data found", "no sounding", "not found", "no observations")):
        raise HTTPException(404, "Não há radiossondagem disponível para esta estação/data/hora.")

    levels = []
    for line in body.splitlines():
        line = line.strip()
        if not line or not re.match(r"^\d{2,4}(?:\.\d+)?\s", line):
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        # TEXT:LIST: PRES, HGHT, TEMP, DWPT, RELH, MIXR, DRCT, SKNT, ...
        p = parse_float(fields[0])
        t = parse_float(fields[2])
        td = parse_float(fields[3])
        if p is None or t is None or td is None or not (100 <= p <= 1100 and -120 <= t <= 70 and -120 <= td <= 70):
            continue
        item = {"pressure": p, "temperature": t, "dewpoint": td}
        if len(fields) > 6:
            wd = parse_float(fields[6])
            if wd is not None and 0 <= wd <= 360:
                item["wind_direction"] = wd
        if len(fields) > 7:
            ws = parse_float(fields[7])
            if ws is not None and 0 <= ws <= 250:
                item["wind_speed"] = ws * 0.514444  # SKNT: knots to m/s
        levels.append(item)
    levels.sort(key=lambda x: x["pressure"], reverse=True)
    if len(levels) < 2:
        raise HTTPException(404, "A fonte não retornou pelo menos dois níveis observados válidos.")
    if sum("dewpoint" in x for x in levels) < 2:
        raise HTTPException(404, "A fonte não forneceu ponto de orvalho observado suficiente; perfil não exibido para evitar inventar dados.")
    return {
        "station": station,
        "station_name": STATIONS[station],
        "observation_utc": f"{date_utc} {hour}Z",
        "source": "University of Wyoming Atmospheric Science Radiosonde Archive (TEXT:LIST)",
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
