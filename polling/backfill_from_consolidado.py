"""
backfill_from_consolidado.py — infiere acquisition_utc para filas legacy
de index.csv cruzando contra el consolidado de Mirova-v1 (latest.php scraping).

Estrategia:
  Para cada fila con acquisition_utc vacío:
    1. Mapear nombre volcán al usado en consolidado (PuyehueCordonCaulle ↔
       "Puyehue-Cordon Caulle", ChillanNevadosde ↔ "Nevados de Chillan", etc.).
    2. Mapear sensor (VIIRS750 ↔ "VIIRS" en consolidado).
    3. Filtrar consolidado por (volcán, sensor) y por
       Fecha_Satelite_UTC ∈ [last_modified - 3h, last_modified].
    4. Tomar la fila más reciente en esa ventana (la pasada inmediatamente
       previa al Last-Modified). Esa es la acquisition probable.
    5. Renombrar archivos al naming acquisition.

  Tolerancia: si no hay match en la ventana, dejar la fila legacy sin tocar.
  No es heurística infalible — MIROVA puede tener pasadas no scrapeadas si
  Mirova-v1 falló en algún cron de 5 min. Pero típicamente da match.

Run-once. Idempotente (re-run no duplica work).
"""

from __future__ import annotations

import csv
import io
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_CSV = REPO_ROOT / "index.csv"

# URL canónica del consolidado en Mirova-v1
CONSOLIDADO_URL = (
    "https://raw.githubusercontent.com/MendozaVolcanic/Mirova-v1/"
    "main/monitoreo_satelital/registro_vrp_consolidado.csv"
)

# Map: nombre en index.csv -> nombre en consolidado.csv (Volcan column)
VOLCANO_MAP = {
    "Isluga": "Isluga",
    "Lascar": "Lascar",
    "Lastarria": "Lastarria",
    "Tupungatito": "Tupungatito",
    "PlanchonPeteroa": "PlanchonPeteroa",
    "ChillanNevadosde": "Nevados de Chillan",
    "Copahue": "Copahue",
    "Llaima": "Llaima",
    "Villarrica": "Villarrica",
    "PuyehueCordonCaulle": "Puyehue-Cordon Caulle",
    "Chaiten": "Chaitén",
}

# Map: sensor en index.csv -> sensor en consolidado.csv
SENSOR_MAP = {
    "MODIS": "MODIS",
    "VIIRS750": "VIIRS",   # consolidado usa "VIIRS" para 750m
    "VIIRS375": "VIIRS375",
}

# Ventana hacia atrás desde Last-Modified
LATENCY_WINDOW = timedelta(hours=3)

# Header consistente con poll.py / migrate_acquisition.py
HEADER = [
    "captured_at_utc",
    "volcano",
    "sensor",
    "band",
    "acquisition_utc",
    "last_modified_utc",
    "md5",
    "size_bytes",
    "tif_path",
    "kmz_path",
]


def parse_dt_utc(s: str) -> datetime | None:
    """Parsea ISO 8601 con TZ o YYYY-MM-DD HH:MM:SS (asume UTC)."""
    if not s:
        return None
    try:
        # ISO 8601 con TZ
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%d_%H%M%S")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    # 1) Bajar consolidado y construir lookup
    logging.info("descargando consolidado de Mirova-v1...")
    r = requests.get(CONSOLIDADO_URL, timeout=60)
    r.raise_for_status()
    text = r.text
    consolidado_rows = list(csv.DictReader(io.StringIO(text)))
    logging.info("  %d filas en consolidado", len(consolidado_rows))

    # Index: (vol_consolidado, sensor_consolidado) -> sorted list of acquisition datetimes
    by_vs: dict[tuple[str, str], list[datetime]] = {}
    for row in consolidado_rows:
        vol = row.get("Volcan", "").strip()
        sens = row.get("Sensor", "").strip()
        ts_str = row.get("Fecha_Satelite_UTC", "").strip()
        if not vol or not sens or not ts_str:
            continue
        ts = parse_dt_utc(ts_str)
        if ts is None:
            continue
        by_vs.setdefault((vol, sens), []).append(ts)
    for k in by_vs:
        by_vs[k].sort()
    logging.info("  %d combinaciones (volcán, sensor) únicas en consolidado", len(by_vs))

    # 2) Cargar nuestro index
    with INDEX_CSV.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    legacy = [r for r in rows if not r.get("acquisition_utc")]
    logging.info("nuestro index.csv: %d filas total, %d legacy sin acquisition", len(rows), len(legacy))

    # 3) Para cada legacy, intentar inferir acquisition
    #    Hacemos lookup por (volcán, sensor, md5) — si dos filas comparten md5,
    #    deben tener la misma acquisition.
    md5_to_acq: dict[tuple[str, str, str], datetime] = {}
    inferred = 0
    no_match = 0

    for row in legacy:
        key = (row["volcano"], row["sensor"], row["md5"])
        if key in md5_to_acq:
            continue  # ya inferida por otra fila con mismo md5

        vol_idx = row["volcano"]
        sens_idx = row["sensor"]
        vol_cons = VOLCANO_MAP.get(vol_idx)
        sens_cons = SENSOR_MAP.get(sens_idx)
        if not vol_cons or not sens_cons:
            continue
        last_mod = parse_dt_utc(row["last_modified_utc"])
        if last_mod is None:
            continue

        candidates = by_vs.get((vol_cons, sens_cons), [])
        # Buscar la acquisition más reciente en (last_mod - LATENCY_WINDOW, last_mod]
        lower = last_mod - LATENCY_WINDOW
        best: datetime | None = None
        for ts in reversed(candidates):  # buscar de atrás hacia adelante
            if ts > last_mod:
                continue
            if ts < lower:
                break
            best = ts
            break
        if best is None:
            no_match += 1
            continue
        md5_to_acq[key] = best
        inferred += 1

    logging.info("inferred=%d unique md5s, no_match=%d", inferred, no_match)

    # 4) Aplicar a las filas + renombrar archivos
    rename_count = 0
    rename_collision = 0
    backfilled_rows = 0
    seen_renames: set[tuple[Path, Path]] = set()

    for row in rows:
        if row.get("acquisition_utc"):
            continue  # ya populado, skip
        key = (row["volcano"], row["sensor"], row["md5"])
        acq = md5_to_acq.get(key)
        if acq is None:
            continue
        row["acquisition_utc"] = acq.isoformat()
        backfilled_rows += 1

        # Renombrar archivos
        new_fname = f"{stamp(acq)}_{row['sensor']}"
        for kind in ("tif", "kmz"):
            col = f"{kind}_path"
            old_rel = row.get(col, "")
            if not old_rel:
                continue
            new_rel = f"data/{kind}/{row['volcano']}/{new_fname}.{kind}"
            if old_rel == new_rel:
                continue
            old_p = REPO_ROOT / old_rel
            new_p = REPO_ROOT / new_rel
            row[col] = new_rel
            pair = (old_p, new_p)
            if pair in seen_renames:
                continue
            seen_renames.add(pair)
            if not old_p.exists():
                # Posible: ya renombrado en una fila anterior con mismo md5
                continue
            if new_p.exists():
                rename_collision += 1
                logging.warning(
                    "%s collision: %s -> %s ya existe; conservando old",
                    kind, old_rel, new_rel,
                )
                continue
            old_p.rename(new_p)
            rename_count += 1

    # 5) Reescribir index
    with INDEX_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)

    still_legacy = sum(1 for r in rows if not r.get("acquisition_utc"))
    logging.info(
        "DONE. backfilled_rows=%d renamed=%d collisions=%d still_legacy=%d",
        backfilled_rows, rename_count, rename_collision, still_legacy,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
