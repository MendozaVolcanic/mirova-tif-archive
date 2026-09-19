# -*- coding: utf-8 -*-
"""El poller no debe archivar respuestas vacías o truncadas de MIROVA.

POR QUÉ. MIROVA sobrescribe su TIF "Last" en cada pasada. Si el poller lo pide justo mientras el
servidor lo reescribe, recibe un 200 con cuerpo vacío. Hasta 2026-09 eso se guardaba como un TIF de
0 bytes con el nombre de la hora de la pasada (7 TIF y 2 PNG en ~154 mil capturas), y en la consulta
siguiente el TIF real de esa misma pasada chocaba con el vacío en el guard de colisión: se guardaba
con el sufijo `_lm` y SIN hora de adquisición. El dato no se perdía, pero quedaba mal rotulado.
Caso real: Nevados de Chillán 2026-09-13, vacío `20260913_051801_VIIRS375.tif` y el real guardado
como `20260913_074634_VIIRS375_lm.tif`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "polling"))

import poll  # noqa: E402

TIF_REAL = b"II*\x00\x98Q\x00\x00" + b"\x00" * 64   # cabecera real de un TIF archivado
PNG_REAL = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
KMZ_REAL = b"PK\x03\x04" + b"\x00" * 64


def test_acepta_los_formatos_reales():
    assert poll.contenido_valido(TIF_REAL, "tif")
    assert poll.contenido_valido(b"MM\x00*" + b"\x00" * 64, "tif")   # TIFF big-endian
    assert poll.contenido_valido(PNG_REAL, "png")
    assert poll.contenido_valido(KMZ_REAL, "kmz")


def test_rechaza_vacio():
    for tipo in ("tif", "png", "kmz"):
        assert not poll.contenido_valido(b"", tipo)
        assert not poll.contenido_valido(None, tipo)


def test_rechaza_otra_cosa_con_codigo_200():
    # una página de error HTML servida con 200, o un binario de otro tipo
    html = b"<html><body>Service Unavailable</body></html>"
    assert not poll.contenido_valido(html, "tif")
    assert not poll.contenido_valido(PNG_REAL, "tif")
    assert not poll.contenido_valido(TIF_REAL, "png")
    assert not poll.contenido_valido(TIF_REAL, "kmz")


def test_un_tif_vacio_no_se_guarda_ni_toca_el_indice(tmp_path, monkeypatch):
    """Sin el arreglo, `process_target` escribe el archivo vacío y devuelve una fila de índice."""
    monkeypatch.setattr(poll, "DATA_TIF", tmp_path / "tif")
    monkeypatch.setattr(poll, "DATA_KMZ", tmp_path / "kmz")
    monkeypatch.setattr(poll, "REPO_ROOT", tmp_path)
    from datetime import datetime, timezone
    monkeypatch.setattr(poll, "head_last_modified",
                        lambda url, s: datetime(2026, 9, 13, 7, 46, 44, tzinfo=timezone.utc))
    monkeypatch.setattr(poll, "fetch_binary", lambda url, s: b"")
    monkeypatch.setattr(poll, "fetch_acquisition_time",
                        lambda v, se, s: datetime(2026, 9, 13, 5, 18, 1, tzinfo=timezone.utc))
    t = poll.Target(volcano="ChillanNevadosde", sensor="VIIRS375", band="I04")
    fila = poll.process_target(t, session=None, last_index={}, dry_run=False)
    assert fila is None, "un TIF vacío no puede entrar al índice: la consulta siguiente debe reintentar"
    assert not list((tmp_path / "tif").rglob("*.tif")), "no debe quedar ningún archivo escrito"
