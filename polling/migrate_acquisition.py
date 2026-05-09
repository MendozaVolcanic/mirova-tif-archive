"""
migrate_acquisition.py — backfill `acquisition_utc` column on existing index.csv
and rename TIF/KMZ files when we can determine the satellite acquisition time.

Strategy:
  Pre-fix the index/file naming used HTTP `Last-Modified` as the file timestamp,
  but MIROVA republishes the same acquisition with new Last-Modified periodically.
  This script:

  1) For each (volcano, sensor) pair currently in the index, fetch the live
     volcanoMap.php to get the **acquisition timestamp** of the currently
     published TIF, AND compute md5 of that current TIF.

  2) For each existing index row whose md5 matches the current MIROVA md5,
     backfill `acquisition_utc` and rename the on-disk file to
     `{YYYYMMDD_HHMMSS_acquisition}_{sensor}.{tif|kmz}`.

  3) Rows whose md5 doesn't match (older captures already replaced on
     MIROVA's server) keep their old filename and `acquisition_utc=""`.
     Their files are left in place — we can't determine post-hoc what
     acquisition MIROVA was publishing at the time.

Run once locally then commit. Idempotent (safe to re-run).
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
import sys
import io
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import yaml

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_CSV = REPO_ROOT / "index.csv"
DATA_TIF = REPO_ROOT / "data" / "tif"
DATA_KMZ = REPO_ROOT / "data" / "kmz"

BASE_URL = "https://www.mirovaweb.it/OUTPUTweb/MIROVA"
VOLCANOMAP_URL = "https://www.mirovaweb.it/NRT/volcanoMap.php"
TIMEOUT = 30

ACQ_RE = re.compile(
    r"Last Update:</strong>\s*(\d{2}-[A-Z][a-z]{2}-\d{4})\s+(\d{2}:\d{2}:\d{2})"
)

# Final schema (must match poll.py INDEX_HEADER)
NEW_HEADER = [
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


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%d_%H%M%S")


def fetch_current_state(volcano: str, sensor: str, band: str, session: requests.Session):
    """Returns (md5, acquisition_dt, last_modified_dt) for the currently
    published TIF + acquisition_time, or (None, None, None) on failure."""
    tif_url = f"{BASE_URL}/{sensor}/VOLCANOES/{volcano}/{volcano}_{sensor}_{band}.tif"
    try:
        r = session.get(tif_url, timeout=TIMEOUT)
    except requests.RequestException as e:
        logging.warning("TIF fetch failed for %s/%s: %s", volcano, sensor, e)
        return None, None, None
    if r.status_code != 200:
        return None, None, None
    md5 = hashlib.md5(r.content).hexdigest()
    lm = r.headers.get("Last-Modified")
    lm_dt = parsedate_to_datetime(lm).astimezone(timezone.utc) if lm else None

    map_url = f"{VOLCANOMAP_URL}?volcano={volcano}&sensor={sensor}"
    try:
        r2 = session.get(map_url, timeout=TIMEOUT)
    except requests.RequestException as e:
        logging.warning("map fetch failed for %s/%s: %s", volcano, sensor, e)
        return md5, None, lm_dt
    if r2.status_code != 200:
        return md5, None, lm_dt
    m = ACQ_RE.search(r2.text)
    if not m:
        return md5, None, lm_dt
    try:
        acq_dt = datetime.strptime(
            f"{m.group(1)} {m.group(2)}", "%d-%b-%Y %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return md5, None, lm_dt
    return md5, acq_dt, lm_dt


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if not INDEX_CSV.exists():
        logging.error("index.csv not found")
        return 2

    # Load existing rows
    with INDEX_CSV.open(encoding="utf-8", newline="") as f:
        old_rows = list(csv.DictReader(f))
    logging.info("loaded %d rows from index.csv", len(old_rows))

    # Distinct (volcano, sensor, band) combos to query
    combos = {(r["volcano"], r["sensor"], r["band"]) for r in old_rows}
    logging.info("querying %d (volcano,sensor) combinations on MIROVA", len(combos))

    session = requests.Session()
    session.headers.update({"User-Agent": "mirova-tif-archive/1.0 migration"})

    # current_state[(vol,sensor)] = (md5, acquisition_dt)
    current_state: dict[tuple[str, str], tuple[str | None, datetime | None]] = {}
    for vol, sensor, band in sorted(combos):
        md5, acq, lm = fetch_current_state(vol, sensor, band, session)
        current_state[(vol, sensor)] = (md5, acq)
        logging.info(
            "  %s/%s current md5=%s acq=%s",
            vol, sensor,
            (md5 or "?")[:10],
            acq.isoformat() if acq else "?",
        )

    # Process rows
    new_rows: list[dict] = []
    backfilled = 0
    renamed = 0
    rename_collisions = 0
    for row in old_rows:
        key = (row["volcano"], row["sensor"])
        cur_md5, cur_acq = current_state.get(key, (None, None))

        new_row = {h: row.get(h, "") for h in NEW_HEADER}

        if cur_md5 and cur_md5 == row["md5"] and cur_acq is not None:
            # We can determine acquisition for this row.
            new_row["acquisition_utc"] = cur_acq.isoformat()
            backfilled += 1

            # Rename files if filename doesn't already use acquisition stamp.
            new_fname = f"{stamp(cur_acq)}_{row['sensor']}"
            old_tif_rel = row["tif_path"]
            old_kmz_rel = row["kmz_path"]
            new_tif_rel = (
                f"data/tif/{row['volcano']}/{new_fname}.tif"
            )
            new_kmz_rel = (
                f"data/kmz/{row['volcano']}/{new_fname}.kmz" if old_kmz_rel else ""
            )

            for old_rel, new_rel, kind in [
                (old_tif_rel, new_tif_rel, "tif"),
                (old_kmz_rel, new_kmz_rel, "kmz"),
            ]:
                if not old_rel:
                    continue
                if old_rel == new_rel:
                    continue
                old_p = REPO_ROOT / old_rel
                new_p = REPO_ROOT / new_rel
                if not old_p.exists():
                    logging.warning("missing on-disk file: %s", old_p)
                    continue
                if new_p.exists():
                    rename_collisions += 1
                    logging.warning(
                        "%s rename collision: %s -> %s already exists; keeping old",
                        kind, old_rel, new_rel,
                    )
                    continue
                old_p.rename(new_p)
                renamed += 1

            new_row["tif_path"] = new_tif_rel
            if old_kmz_rel:
                new_row["kmz_path"] = new_kmz_rel
        # else: legacy row, acquisition_utc remains "" (unknown)

        new_rows.append(new_row)

    # Write the rewritten index
    with INDEX_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=NEW_HEADER)
        writer.writeheader()
        writer.writerows(new_rows)

    logging.info(
        "DONE. backfilled=%d renamed=%d collisions=%d total_rows=%d",
        backfilled, renamed, rename_collisions, len(new_rows),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
