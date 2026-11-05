"""
poll.py — scrapea TIF + KMZ de mirovaweb.it cada vez que cambia Last-Modified.

Diseñado para correr cada 5 minutos vía GitHub Actions disparado por
cron-job.org. Idempotente: si nada cambió, exit 0 sin commit.

Output:
  - data/tif/{Volcano}/{YYYYMMDD_HHMMSS}_{sensor}.tif
  - data/kmz/{Volcano}/{YYYYMMDD_HHMMSS}_{sensor}.kmz
  - index.csv (append-only log)
  - data/png/{Volcano}/{YYYYMMDD_HHMMSS}_{sensor}_{kind}.png, adelgazado (ver
    "Adelgazamiento de PNG" más abajo); index_png.csv registra también lo omitido.

Uso local:
  pip install -r polling/requirements.txt
  python polling/poll.py [--dry-run] [--volcano LASCAR] [--sensor VIIRS375]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
# Config

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "volcanoes.yml"
INDEX_CSV = REPO_ROOT / "index.csv"
INDEX_PNG_CSV = REPO_ROOT / "index_png.csv"
DATA_TIF = REPO_ROOT / "data" / "tif"
DATA_KMZ = REPO_ROOT / "data" / "kmz"
DATA_PNG = REPO_ROOT / "data" / "png"

BASE_URL = "https://www.mirovaweb.it/OUTPUTweb/MIROVA"
VOLCANOMAP_URL = "https://www.mirovaweb.it/NRT/volcanoMap.php"
HTTP_TIMEOUT = 30  # seconds

# Acquisition timestamp regex for the volcanoMap.php page header.
# MIROVA renders e.g. "<strong>Last Update:</strong> 09-May-2026 05:42:02 (11 hours ago)"
# This is the satellite acquisition time of the currently published TIF, which
# can differ substantially from HTTP Last-Modified (MIROVA republishes the same
# acquisition with new Last-Modified periodically).
ACQ_RE = re.compile(
    r"Last Update:</strong>\s*(\d{2}-[A-Z][a-z]{2}-\d{4})\s+(\d{2}:\d{2}:\d{2})"
)
USER_AGENT = (
    "mirova-tif-archive/1.0 (+https://github.com/MendozaVolcanic/mirova-tif-archive) "
    "research mirror by SERNAGEOMIN Chile"
)

INDEX_HEADER = [
    "captured_at_utc",
    "volcano",
    "sensor",
    "band",
    "acquisition_utc",      # NEW: satellite acquisition time from volcanoMap.php
    "last_modified_utc",    # HTTP Last-Modified of the TIF on MIROVA's server
    "md5",
    "size_bytes",
    "tif_path",
    "kmz_path",
]

INDEX_PNG_HEADER = [
    "captured_at_utc",
    "volcano",
    "sensor",      # MODIS / VIIRS750 / VIIRS375 / COMB
    "kind",        # VRP / logVRP / Dist / Latest10NTI
    "last_modified_utc",
    "md5",
    "size_bytes",
    "png_path",     # vacío si la versión se omitió a propósito (ver `omitido`)
    "omitido",      # vacío = guardada; si no, el motivo por el que no se guardó (S173)
]
# Cabecera del índice antes de S173 (8 columnas). `append_png_index` la migra en el lugar.
INDEX_PNG_HEADER_V1 = INDEX_PNG_HEADER[:-1]

# Per-sensor PNG types (the 4 plots per sensor)
PER_SENSOR_PNG_KINDS = ["VRP", "logVRP", "Dist", "Latest10NTI"]
# COMB cross-sensor PNGs (3 plots, no Latest10NTI for COMB)
COMB_PNG_KINDS = ["VRP", "logVRP", "Dist"]

# ---------------------------------------------------------------------------
# Adelgazamiento de PNG (S173, 2026-11-05, aprobado por Nicolás)
#
# POR QUÉ. El repo pesaba 21,7 GB el 2026-11-05 y el 94 % eran PNG; entraban ~3 GB al mes, muy por
# encima de lo que GitHub recomienda (5 GB por repo). Cada PNG es distinto en bytes (cambia el
# rótulo "Last Update"), pero casi toda su INFORMACIÓN se repite:
#
# - VRP, logVRP y Dist (por sensor y COMB) son series de tiempo ACUMULATIVAS: el gráfico de hoy
#   trae dibujados los puntos de ayer. Se guarda la PRIMERA versión nueva de cada día UTC: como se
#   genera con la primera pasada del día, trae completo el día anterior. Se pierden las versiones
#   intermedias del día (incluida la estrella de "última alerta" del mapa COMB cuando hay varias
#   alertas el mismo día); esos números ya los publica latest.php y los guarda Mirova-v1.
#
# - Latest10NTI es un mosaico de las 10 ÚLTIMAS pasadas, con la hora de cada una. Desde el
#   2026-09-22 MIROVA sirve siempre el mismo TIF, así que este mosaico es la única información
#   espacial nueva que queda: ninguna pasada puede quedar fuera de todos los mosaicos guardados.
#   Regla, tres partes:
#     (1) se guarda una versión de cada NTI_CADA_N_VERSIONES; las demás se omiten;
#     (2) se guarda la versión que llega tras NTI_SILENCIO_HORAS o más sin versiones (puede traer
#         de golpe muchas pasadas si MIROVA se estaba poniendo al día, y las más viejas quedarían
#         al fondo del mosaico, a punto de salir);
#     (3) "rescate": una versión omitida que lleva NTI_SILENCIO_HORAS publicada sin sucesor se
#         guarda en ese momento (ver `hay_que_rescatar`): mientras MIROVA calla sigue a la vista.
#   O sea, todo silencio de 8 h o más queda con un mosaico guardado a cada lado. Medido sobre
#   index_png.csv e index.csv (2026-05-08 a 2026-11-05, 11 volcanes, 3 sensores):
#     * hay 1,3 a 1,5 versiones del mosaico por pasada (MIROVA lo republica sin pasada nueva);
#     * un día UTC trae como máximo 9 (MODIS), 12 (VIIRS 375) y 13 (VIIRS 750) versiones;
#     * en 8 h móviles caben como máximo 6 versiones del mosaico y 5 pasadas con TIF.
#   Entre dos mosaicos guardados entran, entonces, las pasadas de 4 versiones separadas por menos
#   de 8 h: unas 3 en el caso típico, 8 si cada versión trae dos (poller que pierde corridas), y en
#   el peor caso medido una versión con las ≤ 5 de 8 h más tres sueltas. Todo cabe en 10.
#   Aplicada a la historia completa (medir/simular_regla.py en la sesión S173 de VRP Chile): 0
#   pasadas quedan sin mosaico guardado, contando cada versión como una pasada y contando las 14.149
#   pasadas con TIF de mayo a septiembre; se guarda el 46 % de los bytes del mosaico. Sin la parte (2)
#   bajaba a 32 %, pero una prueba sintética (MIROVA se pone al día con 14 pasadas) perdía 3.
#   Límite aceptado: si el POLLER (no MIROVA) se cae justo después de omitir y en su ausencia
#   entran más de ~7 pasadas, se pierden las de hasta 3 versiones omitidas pendientes. Guardando todo
#   también se pierde lo que exceda 10 en ese hueco; lo propio de esta regla es lo pendiente.
#
# Lo omitido NO desaparece del índice: queda una fila con md5, tamaño y Last-Modified, png_path
# vacío y el motivo en `omitido`, para que se distinga de una captura fallida.
# Los TIF y KMZ no se adelgazan. Pruebas: tests/test_adelgazar_png.py.
GRAFICOS_UNO_POR_DIA = frozenset({"VRP", "logVRP", "Dist"})
NTI_CADA_N_VERSIONES = 4
NTI_SILENCIO_HORAS = 8
OMITIDO_GRAFICO = "grafico_ya_guardado_este_dia"
OMITIDO_NTI = "nti_cubierto_por_mosaico_guardado"

# ---------------------------------------------------------------------------
# Data classes


@dataclass(frozen=True)
class Target:
    volcano: str        # MIROVA name, e.g. "Lascar"
    sensor: str         # MODIS / VIIRS750 / VIIRS375
    band: str           # B21 / M13 / I04

    def tif_url(self) -> str:
        return f"{BASE_URL}/{self.sensor}/VOLCANOES/{self.volcano}/{self.volcano}_{self.sensor}_{self.band}.tif"

    def kmz_url(self) -> str:
        return f"{BASE_URL}/{self.sensor}/VOLCANOES/{self.volcano}/{self.volcano}_{self.sensor}_Last_GE.kmz"


@dataclass(frozen=True)
class PngTarget:
    """Asset PNG: per-sensor plot OR COMB cross-sensor plot."""
    volcano: str        # MIROVA name
    sensor: str         # MODIS / VIIRS750 / VIIRS375 / COMB
    kind: str           # VRP / logVRP / Dist / Latest10NTI

    def url(self) -> str:
        return f"{BASE_URL}/{self.sensor}/VOLCANOES/{self.volcano}/{self.volcano}_{self.sensor}_{self.kind}.png"


@dataclass
class EstadoPng:
    """Lo que la regla de adelgazamiento necesita del historial de un (volcán, sensor, tipo)."""
    ultimo_lm_guardado: datetime | None = None
    omitidas_desde_guardado: int = 0
    ultimo_lm_visto: datetime | None = None     # última versión nueva, guardada u omitida


def actualizar_estado(e: EstadoPng, last_modified: datetime | None, guardada: bool) -> None:
    """Registra una versión NUEVA (guardada u omitida) en el estado."""
    if guardada:
        e.omitidas_desde_guardado = 0
        if last_modified is not None:
            e.ultimo_lm_guardado = last_modified
    else:
        e.omitidas_desde_guardado += 1
    if last_modified is not None:
        e.ultimo_lm_visto = last_modified


def decidir_guardar_png(kind: str, last_modified: datetime, estado: EstadoPng | None) -> str:
    """"" si una versión NUEVA (md5 distinto) se guarda; si no, el motivo para omitirla."""
    if estado is None or estado.ultimo_lm_guardado is None:
        return ""
    if kind in GRAFICOS_UNO_POR_DIA:
        if last_modified.date() != estado.ultimo_lm_guardado.date():
            return ""
        return OMITIDO_GRAFICO
    if kind == "Latest10NTI":
        if estado.omitidas_desde_guardado + 1 >= NTI_CADA_N_VERSIONES:
            return ""
        visto = estado.ultimo_lm_visto or estado.ultimo_lm_guardado
        if last_modified - visto >= timedelta(hours=NTI_SILENCIO_HORAS):
            return ""
        return OMITIDO_NTI
    return ""   # tipo desconocido: se guarda, por las dudas


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


def hay_que_rescatar(kind: str, last_row: dict, ahora: datetime) -> bool:
    """True si el mosaico NTI vigente se omitió y lleva NTI_SILENCIO_HORAS sin sucesor.

    POR QUÉ. Un mosaico omitido confía en que el SIGUIENTE guardado repita sus pasadas. Si MIROVA
    calla un rato y después publica de golpe muchas pasadas (se pone al día), el siguiente podría
    ya no mostrarlas. Mientras MIROVA calla, el mosaico omitido sigue publicado: se guarda ahí.
    """
    if kind != "Latest10NTI" or not last_row.get("omitido"):
        return False
    try:
        lm = datetime.fromisoformat(last_row["last_modified_utc"])
    except ValueError:
        return True
    return ahora - lm >= timedelta(hours=NTI_SILENCIO_HORAS)


# ---------------------------------------------------------------------------
# Helpers


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def load_targets() -> list[Target]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    targets: list[Target] = []
    for v in cfg["volcanoes"]:
        for s in cfg["sensors"]:
            targets.append(Target(volcano=v["mirova_name"], sensor=s["name"], band=s["band"]))
    return targets


def load_png_targets() -> list[PngTarget]:
    """Build the list of PNG assets to poll: per-sensor plots + COMB plots."""
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    targets: list[PngTarget] = []
    for v in cfg["volcanoes"]:
        vol = v["mirova_name"]
        for s in cfg["sensors"]:
            for kind in PER_SENSOR_PNG_KINDS:
                targets.append(PngTarget(volcano=vol, sensor=s["name"], kind=kind))
        for kind in COMB_PNG_KINDS:
            targets.append(PngTarget(volcano=vol, sensor="COMB", kind=kind))
    return targets


def load_index() -> dict[tuple[str, str], dict]:
    """Map (volcano, sensor) -> last row dict for TIF index."""
    if not INDEX_CSV.exists():
        return {}
    last: dict[tuple[str, str], dict] = {}
    with INDEX_CSV.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            key = (row["volcano"], row["sensor"])
            last[key] = row
    return last


def load_png_index_y_estado() -> tuple[
    dict[tuple[str, str, str], dict], dict[tuple[str, str, str], EstadoPng]
]:
    """(volcano, sensor, kind) -> última fila del índice PNG, y el estado del adelgazamiento.

    Una fila con `omitido` es una versión omitida; una fila "touched" (mismo md5 que la anterior)
    no es versión nueva y no cuenta.
    """
    if not INDEX_PNG_CSV.exists():
        return {}, {}
    last: dict[tuple[str, str, str], dict] = {}
    estado: dict[tuple[str, str, str], EstadoPng] = {}
    with INDEX_PNG_CSV.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("omitido") is None:
                row["omitido"] = ""      # fila anterior a S173 (8 columnas)
            key = (row["volcano"], row["sensor"], row["kind"])
            prev = last.get(key)
            e = estado.setdefault(key, EstadoPng())
            try:
                lm = datetime.fromisoformat(row["last_modified_utc"])
            except ValueError:
                lm = None
            if row["omitido"]:
                if not (prev and prev.get("md5") == row["md5"]):
                    actualizar_estado(e, lm, guardada=False)
            elif row.get("png_path"):
                # ruta nueva = versión guardada (también el rescate, que repite el md5 omitido)
                if not (prev and prev.get("png_path") == row["png_path"]):
                    actualizar_estado(e, lm, guardada=True)
            last[key] = row
    return last, estado


def load_png_index() -> dict[tuple[str, str, str], dict]:
    """Map (volcano, sensor, kind) -> last row dict for PNG index."""
    return load_png_index_y_estado()[0]


def append_index(rows: list[dict]) -> None:
    new = not INDEX_CSV.exists()
    INDEX_CSV.parent.mkdir(parents=True, exist_ok=True)
    with INDEX_CSV.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_HEADER)
        if new:
            writer.writeheader()
        writer.writerows(rows)


def _migrar_cabecera_png() -> None:
    """Agrega la columna `omitido` a la cabecera de un índice anterior a S173.

    Sólo cambia la primera línea: las filas viejas quedan byte a byte iguales (el diff de git es
    una línea) y cualquier lector CSV las lee con `omitido` vacío.
    """
    with INDEX_PNG_CSV.open("rb") as f:
        primera = f.readline()
        if primera.rstrip(b"\r\n").decode("utf-8") != ",".join(INDEX_PNG_HEADER_V1):
            return
        resto = f.read()
    fin = b"\r\n" if primera.endswith(b"\r\n") else b"\n"
    tmp = INDEX_PNG_CSV.with_name(INDEX_PNG_CSV.name + ".tmp")
    tmp.write_bytes(",".join(INDEX_PNG_HEADER).encode("utf-8") + fin + resto)
    os.replace(tmp, INDEX_PNG_CSV)


def append_png_index(rows: list[dict]) -> None:
    new = not INDEX_PNG_CSV.exists()
    INDEX_PNG_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not new:
        _migrar_cabecera_png()
    with INDEX_PNG_CSV.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_PNG_HEADER)
        if new:
            writer.writeheader()
        writer.writerows(rows)


def head_last_modified(url: str, session: requests.Session) -> datetime | None:
    """Returns Last-Modified as UTC datetime, or None on error/404."""
    try:
        r = session.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        logging.warning("HEAD failed for %s: %s", url, e)
        return None
    if r.status_code != 200:
        logging.info("HEAD %s -> %s", url, r.status_code)
        return None
    lm = r.headers.get("Last-Modified")
    if not lm:
        return None
    return parsedate_to_datetime(lm).astimezone(timezone.utc)


def fetch_binary(url: str, session: requests.Session) -> bytes | None:
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        logging.warning("GET failed for %s: %s", url, e)
        return None
    if r.status_code != 200:
        logging.info("GET %s -> %s", url, r.status_code)
        return None
    return r.content


# Firma de los primeros bytes de cada formato, comprobada contra archivos reales del archivo
# (TIF `II*` + byte cero, PNG estándar, KMZ = zip `PK`). TIFF big-endian se acepta por si MIROVA
# cambia de biblioteca.
FIRMAS = {
    "tif": (b"II*\x00", b"MM\x00*"),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "kmz": (b"PK\x03\x04",),
}


def contenido_valido(data: bytes | None, tipo: str) -> bool:
    """True si `data` es un archivo no vacío del formato esperado.

    POR QUÉ. MIROVA sobrescribe su imagen "Last" en cada pasada. Si el poller la pide justo
    mientras el servidor la reescribe, recibe un 200 con el cuerpo vacío (o una página de error
    servida con 200). Hasta 2026-09 eso se archivaba como un TIF de 0 bytes (7 TIF y 2 PNG en
    ~154 mil capturas), y en la consulta siguiente el TIF real de la MISMA pasada chocaba con el
    vacío en el guard de colisión: quedaba guardado con sufijo `_lm` y sin hora de adquisición.
    Rechazar acá, sin tocar el índice, hace que la consulta siguiente simplemente reintente.
    """
    if not data:
        return False
    return data.startswith(FIRMAS[tipo])


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%d_%H%M%S")


def fetch_acquisition_time(
    volcano: str, sensor: str, session: requests.Session
) -> datetime | None:
    """
    Scrape `volcanoMap.php` for the currently displayed satellite acquisition
    timestamp. This is the Last Update shown in the page header — the actual
    pass time, NOT the HTTP Last-Modified of the TIF.

    Returns None on failure. Treated as UTC (MIROVA is consistently UTC).
    """
    url = f"{VOLCANOMAP_URL}?volcano={volcano}&sensor={sensor}"
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        logging.warning("acquisition fetch failed for %s/%s: %s", volcano, sensor, e)
        return None
    if r.status_code != 200:
        logging.warning("acquisition fetch %s/%s -> %d", volcano, sensor, r.status_code)
        return None
    m = ACQ_RE.search(r.text)
    if not m:
        logging.warning("acquisition timestamp not found in %s/%s page", volcano, sensor)
        return None
    date_str, time_str = m.group(1), m.group(2)
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%d-%b-%Y %H:%M:%S")
    except ValueError as e:
        logging.warning("acquisition parse failed: %s", e)
        return None
    return dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Core


def process_target(
    target: Target,
    session: requests.Session,
    last_index: dict[tuple[str, str], dict],
    dry_run: bool,
) -> dict | None:
    key = (target.volcano, target.sensor)
    last_modified = head_last_modified(target.tif_url(), session)
    if last_modified is None:
        return None

    last_row = last_index.get(key)
    if last_row and last_row["last_modified_utc"] == last_modified.isoformat():
        # Same Last-Modified as last capture — MIROVA hasn't pushed a new pass.
        return None

    logging.info(
        "[%s/%s] new pass detected (Last-Modified=%s, prev=%s)",
        target.volcano,
        target.sensor,
        last_modified.isoformat(),
        last_row["last_modified_utc"] if last_row else "<none>",
    )

    tif_bytes = fetch_binary(target.tif_url(), session)
    if tif_bytes is None:
        return None
    if not contenido_valido(tif_bytes, "tif"):
        # No se guarda y NO se devuelve fila: el índice queda con el Last-Modified anterior, así
        # que la próxima consulta vuelve a bajar el TIF, ya escrito entero por MIROVA.
        logging.warning(
            "[%s/%s] TIF inválido (%d bytes, empieza %r); no se archiva, se reintenta en la próxima consulta",
            target.volcano, target.sensor, len(tif_bytes), tif_bytes[:8],
        )
        return None

    md5 = md5_hex(tif_bytes)
    if last_row and last_row["md5"] == md5:
        # Last-Modified bumped but content identical → silent re-touch by MIROVA
        # OR republication of the same acquisition. Update index, no new file.
        # Refresh acquisition_utc too in case MIROVA republished with the same
        # binary but a different stated acquisition (rare; defensive).
        acq = fetch_acquisition_time(target.volcano, target.sensor, session)
        logging.info(
            "[%s/%s] same md5 (%s); silent re-touch update",
            target.volcano, target.sensor, md5,
        )
        return {
            **last_row,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "last_modified_utc": last_modified.isoformat(),
            "acquisition_utc": acq.isoformat() if acq else last_row.get("acquisition_utc", ""),
        }

    # NEW content. Fetch acquisition_time for proper filename + KMZ.
    acquisition = fetch_acquisition_time(target.volcano, target.sensor, session)
    kmz_bytes = fetch_binary(target.kmz_url(), session)
    if kmz_bytes is not None and not contenido_valido(kmz_bytes, "kmz"):
        # El KMZ es accesorio: se guarda el TIF igual y el KMZ queda vacío en el índice.
        logging.warning(
            "[%s/%s] KMZ inválido (%d bytes); se guarda sólo el TIF",
            target.volcano, target.sensor, len(kmz_bytes),
        )
        kmz_bytes = None

    if dry_run:
        logging.info(
            "[dry-run] would save %s/%s acq=%s (%d bytes)",
            target.volcano, target.sensor,
            acquisition.isoformat() if acquisition else "<unknown>",
            len(tif_bytes),
        )
        return None

    # Filename uses acquisition time when available (true satellite pass),
    # falling back to Last-Modified if MIROVA's page can't be parsed.
    name_dt = acquisition if acquisition is not None else last_modified
    fname = f"{stamp(name_dt)}_{target.sensor}"
    tif_path = DATA_TIF / target.volcano / f"{fname}.tif"
    tif_path.parent.mkdir(parents=True, exist_ok=True)

    # COLLISION GUARD: si el archivo destino ya existe con CONTENIDO DISTINTO
    # al que vamos a escribir, MIROVA tiene desync entre header y binario:
    # el header del visor mostró acquisition X, pero el binario del TIF que
    # bajamos corresponde a una acquisition POSTERIOR cuyo header todavía no
    # se actualizó. Sin esto, sobrescribimos un TIF previo válido.
    # Fallback: nombrar por last_modified con sufijo _lm y marcar
    # acquisition_utc="" para que sea claro que no es confiable.
    if tif_path.exists():
        existing_md5 = md5_hex(tif_path.read_bytes())
        if existing_md5 != md5:
            logging.warning(
                "[%s/%s] COLLISION: %s ya existe con md5 %s, nuevo md5 %s; "
                "MIROVA header desync. Usando fallback last_modified naming.",
                target.volcano, target.sensor, fname, existing_md5[:10], md5[:10],
            )
            acquisition = None  # invalidate — no confiamos en el header
            fname = f"{stamp(last_modified)}_{target.sensor}_lm"
            tif_path = DATA_TIF / target.volcano / f"{fname}.tif"
            # Si ESE también colisiona, abortar limpio (caso muy raro)
            if tif_path.exists():
                logging.error(
                    "[%s/%s] fallback path %s also exists; aborting save",
                    target.volcano, target.sensor, fname,
                )
                return None

    tif_path.write_bytes(tif_bytes)

    kmz_path_str = ""
    if kmz_bytes is not None:
        kmz_path = DATA_KMZ / target.volcano / f"{fname}.kmz"
        kmz_path.parent.mkdir(parents=True, exist_ok=True)
        kmz_path.write_bytes(kmz_bytes)
        kmz_path_str = str(kmz_path.relative_to(REPO_ROOT)).replace("\\", "/")

    logging.info(
        "[%s/%s] saved acq=%s lm=%s",
        target.volcano, target.sensor,
        acquisition.isoformat() if acquisition else "<unknown>",
        last_modified.isoformat(),
    )

    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "volcano": target.volcano,
        "sensor": target.sensor,
        "band": target.band,
        "acquisition_utc": acquisition.isoformat() if acquisition else "",
        "last_modified_utc": last_modified.isoformat(),
        "md5": md5,
        "size_bytes": len(tif_bytes),
        "tif_path": str(tif_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "kmz_path": kmz_path_str,
    }


def process_png_target(
    target: PngTarget,
    session: requests.Session,
    last_index: dict[tuple[str, str, str], dict],
    dry_run: bool,
    estado: dict[tuple[str, str, str], EstadoPng] | None = None,
) -> dict | None:
    """Fila a agregar al índice (versión guardada, re-touch u omitida), o None."""
    key = (target.volcano, target.sensor, target.kind)
    last_modified = head_last_modified(target.url(), session)
    if last_modified is None:
        return None

    last_row = last_index.get(key)
    rescate = False
    if last_row and last_row["last_modified_utc"] == last_modified.isoformat():
        if not hay_que_rescatar(target.kind, last_row, ahora_utc()):
            return None
        rescate = True

    png_bytes = fetch_binary(target.url(), session)
    if png_bytes is None:
        return None
    if not contenido_valido(png_bytes, "png"):
        logging.warning(
            "[%s/%s/%s] PNG inválido (%d bytes); no se archiva, se reintenta en la próxima consulta",
            target.volcano, target.sensor, target.kind, len(png_bytes),
        )
        return None

    md5 = md5_hex(png_bytes)
    if last_row and last_row["md5"] == md5 and rescate:
        # El mosaico omitido sigue publicado y nadie lo reemplazó en NTI_SILENCIO_HORAS: se guarda ahora.
        motivo = ""
        logging.info(
            "[%s/%s/%s] rescate de mosaico omitido sin sucesor (Last-Modified=%s)",
            target.volcano, target.sensor, target.kind, last_modified.isoformat(),
        )
    elif last_row and last_row["md5"] == md5:
        # silent re-touch
        return {
            **last_row,
            "omitido": last_row.get("omitido") or "",
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "last_modified_utc": last_modified.isoformat(),
        }
    else:
        motivo = decidir_guardar_png(target.kind, last_modified, (estado or {}).get(key))

    if dry_run:
        logging.info(
            "[dry-run] would %s %s/%s/%s (%d bytes)",
            f"omit ({motivo})" if motivo else "save",
            target.volcano, target.sensor, target.kind, len(png_bytes),
        )
        return None

    if motivo:
        logging.info(
            "[%s/%s/%s] omitido: %s (Last-Modified=%s)",
            target.volcano, target.sensor, target.kind, motivo, last_modified.isoformat(),
        )
        return {
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "volcano": target.volcano,
            "sensor": target.sensor,
            "kind": target.kind,
            "last_modified_utc": last_modified.isoformat(),
            "md5": md5,
            "size_bytes": len(png_bytes),
            "png_path": "",
            "omitido": motivo,
        }

    fname = f"{stamp(last_modified)}_{target.sensor}_{target.kind}"
    png_path = DATA_PNG / target.volcano / f"{fname}.png"
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.write_bytes(png_bytes)

    logging.info(
        "[%s/%s/%s] saved (Last-Modified=%s)",
        target.volcano, target.sensor, target.kind, last_modified.isoformat(),
    )

    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "volcano": target.volcano,
        "sensor": target.sensor,
        "kind": target.kind,
        "last_modified_utc": last_modified.isoformat(),
        "md5": md5,
        "size_bytes": len(png_bytes),
        "png_path": str(png_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "omitido": "",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="HEAD + diff but don't save")
    parser.add_argument("--volcano", help="filter to single MIROVA volcano name")
    parser.add_argument("--sensor", help="filter to single sensor (MODIS/VIIRS750/VIIRS375/COMB)")
    parser.add_argument("--skip-tif", action="store_true", help="poll only PNGs (skip TIF/KMZ)")
    parser.add_argument("--skip-png", action="store_true", help="poll only TIF/KMZ (skip PNGs)")
    args = parser.parse_args()

    setup_logging()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    new_rows_tif: list[dict] = []
    touched_tif: list[dict] = []
    new_rows_png: list[dict] = []
    touched_png: list[dict] = []
    omitted_png: list[dict] = []

    # ---- TIF + KMZ leg ----
    if not args.skip_tif:
        targets = load_targets()
        if args.volcano:
            targets = [t for t in targets if t.volcano == args.volcano]
        if args.sensor:
            targets = [t for t in targets if t.sensor == args.sensor]
        last_index = load_index()
        for t in targets:
            row = process_target(t, session, last_index, args.dry_run)
            if row is None:
                continue
            prev = last_index.get((t.volcano, t.sensor))
            if prev and row.get("tif_path") == prev.get("tif_path"):
                touched_tif.append(row)
            else:
                new_rows_tif.append(row)
        # Persist BOTH new and touched rows so next poll's last_index has the
        # updated Last-Modified — otherwise we re-download every cycle when
        # MIROVA touches the file without changing content (~146KB wasted/poll).
        if not args.dry_run and (new_rows_tif or touched_tif):
            append_index(new_rows_tif + touched_tif)

    # ---- PNG leg ----
    if not args.skip_png:
        png_targets = load_png_targets()
        if args.volcano:
            png_targets = [t for t in png_targets if t.volcano == args.volcano]
        if args.sensor:
            png_targets = [t for t in png_targets if t.sensor == args.sensor]
        last_png_index, estado_png = load_png_index_y_estado()
        for t in png_targets:
            row = process_png_target(t, session, last_png_index, args.dry_run, estado_png)
            if row is None:
                continue
            prev = last_png_index.get((t.volcano, t.sensor, t.kind))
            if prev and row.get("md5") == prev.get("md5") and row.get("png_path") == prev.get("png_path"):
                touched_png.append(row)
            elif row.get("omitido"):
                omitted_png.append(row)
            else:
                new_rows_png.append(row)
        if not args.dry_run and (new_rows_png or touched_png or omitted_png):
            append_png_index(new_rows_png + omitted_png + touched_png)

    total_new = len(new_rows_tif) + len(new_rows_png)
    total_touched = len(touched_tif) + len(touched_png) + len(omitted_png)

    if total_new == 0 and total_touched == 0:
        logging.info("no changes")
        print("STATUS: no_changes")
        return 0

    logging.info(
        "summary: tif=%d/%d png=%d/%d/%d (new/touched/omitted)",
        len(new_rows_tif), len(touched_tif),
        len(new_rows_png), len(touched_png), len(omitted_png),
    )
    print(
        f"STATUS: changes "
        f"tif_new={len(new_rows_tif)} tif_touched={len(touched_tif)} "
        f"png_new={len(new_rows_png)} png_touched={len(touched_png)} "
        f"png_omitted={len(omitted_png)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
