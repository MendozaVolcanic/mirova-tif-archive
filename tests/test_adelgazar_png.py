# -*- coding: utf-8 -*-
"""El poller guarda menos PNG sin perder información.

POR QUÉ. Los PNG son el 94 % de los 21,7 GB del repo y entran ~3 GB al mes. Casi todo es
redundante en información, aunque cada archivo sea distinto en bytes:

- `VRP`, `logVRP` y `Dist` (por sensor y COMB) son series de tiempo acumulativas: el gráfico de
  hoy contiene dibujados todos los puntos de ayer. Basta una versión por día.
- `Latest10NTI` es un mosaico de las 10 últimas pasadas: cada pasada reaparece en hasta 10
  mosaicos seguidos. Desde el 2026-09-22 MIROVA sirve siempre el mismo TIF, así que este mosaico
  es la única información espacial nueva que queda: NINGUNA pasada puede quedar fuera de todos
  los mosaicos guardados.

Estas pruebas simulan una secuencia de consultas contra un servidor falso de MIROVA, corren el
`main()` real del poller (índice incluido) y leen de vuelta los archivos que quedaron en disco.
"""
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "polling"))

import poll  # noqa: E402

SIG = b"\x89PNG\r\n\x1a\n"
T0 = datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc)
VOL = "Prueba"


class _Resp:
    def __init__(self, status, headers=None, content=b""):
        self.status_code = status
        self.headers = headers or {}
        self.content = content


class ServidorFalso:
    """Imita OUTPUTweb de MIROVA: una imagen 'Last' por URL que se sobrescribe."""

    def __init__(self):
        self.archivos = {}  # url -> (last_modified, bytes)
        self.headers = {}

    def publicar(self, url, lm, data):
        self.archivos[url] = (lm, data)

    def head(self, url, timeout=None, allow_redirects=True):
        if url not in self.archivos:
            return _Resp(404)
        lm, _ = self.archivos[url]
        return _Resp(200, {"Last-Modified": format_datetime(lm, usegmt=True)})

    def get(self, url, timeout=None):
        if url not in self.archivos:
            return _Resp(404)
        return _Resp(200, content=self.archivos[url][1])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "volcanoes.yml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        f"volcanoes:\n  - mirova_id: 1\n    mirova_name: {VOL}\n    display: {VOL}\n"
        "sensors:\n  - name: VIIRS375\n    band: I04\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(poll, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(poll, "CONFIG_PATH", cfg)
    monkeypatch.setattr(poll, "INDEX_CSV", tmp_path / "index.csv")
    monkeypatch.setattr(poll, "INDEX_PNG_CSV", tmp_path / "index_png.csv")
    monkeypatch.setattr(poll, "DATA_TIF", tmp_path / "data" / "tif")
    monkeypatch.setattr(poll, "DATA_KMZ", tmp_path / "data" / "kmz")
    monkeypatch.setattr(poll, "DATA_PNG", tmp_path / "data" / "png")
    srv = ServidorFalso()
    monkeypatch.setattr(poll.requests, "Session", lambda: srv)
    return tmp_path, srv, monkeypatch


def consultar(monkeypatch, sensor, dry_run=False, ahora=None):
    if ahora is not None:
        monkeypatch.setattr(poll, "ahora_utc", lambda: ahora)
    argv = ["poll.py", "--skip-tif", "--volcano", VOL, "--sensor", sensor]
    if dry_run:
        argv.append("--dry-run")
    monkeypatch.setattr(sys, "argv", argv)
    assert poll.main() == 0


def url(sensor, kind):
    return poll.PngTarget(volcano=VOL, sensor=sensor, kind=kind).url()


def filas_indice(tmp_path):
    p = tmp_path / "index_png.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def guardados(tmp_path, sensor, kind):
    d = tmp_path / "data" / "png" / VOL
    if not d.exists():
        return []
    return sorted(d.glob(f"*_{sensor}_{kind}.png"))


# ---------------------------------------------------------------------------
# Simulación de pasadas y del mosaico Latest10NTI


def simular_nti(repo, eventos, poller_vivo=True):
    """`eventos` = lista de (lm, n_pasadas_nuevas). n=0 es una republicación con bytes nuevos
    sin pasada nueva (MIROVA lo hace: hay 1,4 versiones por pasada en el índice real).

    Devuelve (pasadas_que_ya_salieron_del_mosaico, cubiertas_si_se_guardara_todo,
    cubiertas_por_lo_guardado). Sólo se exige cobertura a las pasadas que YA salieron del mosaico
    vigente: las que siguen a la vista todavía pueden entrar en el próximo mosaico guardado.
    Con `poller_vivo` el poller consulta cada 30 min también cuando MIROVA calla; sin él, sólo
    consulta justo después de cada publicación (poller caído en los silencios).
    """
    tmp_path, srv, mp = repo
    pasadas = []
    cubiertas_todo = set()
    previo = None
    for i, (lm, nuevas) in enumerate(eventos):
        if poller_vivo and previo is not None:
            t = previo + timedelta(minutes=30)
            while t < lm:
                consultar(mp, "VIIRS375", ahora=t)
                t += timedelta(minutes=30)
        previo = lm
        for _ in range(nuevas):
            pasadas.append(f"P{len(pasadas):04d}")
        ultimas10 = pasadas[-10:]
        cubiertas_todo.update(ultimas10)
        data = SIG + json.dumps({"pases": ultimas10, "version": i}).encode()
        srv.publicar(url("VIIRS375", "Latest10NTI"), lm, data)
        consultar(mp, "VIIRS375", ahora=lm + timedelta(minutes=5))
    cubiertas = set()
    for f in guardados(tmp_path, "VIIRS375", "Latest10NTI"):
        cubiertas.update(json.loads(f.read_bytes()[len(SIG):])["pases"])
    salidas = set(pasadas[:-10])
    return salidas, cubiertas_todo, cubiertas


def eventos_viirs_peor_dia(dias=4):
    """Ritmo del peor caso medido (14 versiones en 24 h, Chaitén VIIRS 375): pasadas agrupadas de
    noche y de tarde, con republicaciones intercaladas."""
    ev = []
    for d in range(dias):
        base = T0 + timedelta(days=d)
        for h, n in [(0.5, 1), (1.3, 1), (2.1, 0), (3.0, 1), (4.6, 1), (5.4, 1), (6.2, 0),
                     (16.7, 1), (17.5, 1), (18.3, 1), (19.1, 0), (20.0, 1), (21.0, 0), (22.4, 1)]:
            ev.append((base + timedelta(hours=h), n))
    return ev


def test_nti_ninguna_pasada_queda_sin_mosaico_guardado(repo):
    ev = eventos_viirs_peor_dia()
    salidas, cubiertas_todo, cubiertas = simular_nti(repo, ev)
    assert len(salidas) >= 30 and salidas <= cubiertas_todo
    faltan = salidas - cubiertas
    assert not faltan, f"pasadas sin ningún mosaico guardado: {sorted(faltan)}"
    # y efectivamente adelgaza
    n_guardados = len(guardados(repo[0], "VIIRS375", "Latest10NTI"))
    assert n_guardados <= len(ev) // 2, (n_guardados, len(ev))


def test_nti_aguanta_versiones_que_traen_dos_pasadas(repo):
    """Si el poller pierde corridas, cada versión observada puede traer más de una pasada."""
    ev = [(T0 + timedelta(hours=1.5 * i), 2) for i in range(40)]
    salidas, _, cubiertas = simular_nti(repo, ev)
    assert len(salidas) > 60
    assert salidas - cubiertas == set()


def _eventos_con_hueco():
    ev = [(T0 + timedelta(hours=2 * i), 1) for i in range(6)]
    ev.append((T0 + timedelta(hours=40), 14))          # 14 pasadas de golpe: 4 ya no se ven
    ev += [(T0 + timedelta(hours=41 + 2 * i), 1) for i in range(8)]
    return ev


def test_nti_mirova_calla_y_se_pone_al_dia_no_pierde_mas_que_guardando_todo(repo):
    """MIROVA calla 30 h y después publica 14 pasadas de una vez. Guardando todo se pierden 4;
    la regla no debe perder NINGUNA más: el mosaico omitido antes del silencio se rescata."""
    salidas, cubiertas_todo, cubiertas = simular_nti(repo, _eventos_con_hueco())
    assert len(salidas - cubiertas_todo) == 4
    assert (salidas & cubiertas_todo) - cubiertas == set()


def test_nti_poller_caido_riesgo_residual_acotado(repo):
    """Límite conocido y aceptado: si el POLLER se cae justo después de omitir un mosaico y en su
    ausencia entran más de ~7 pasadas, se pierden las pasadas de los mosaicos omitidos pendientes
    (como mucho NTI_CADA_N_VERSIONES - 1 versiones). Guardando todo se habrían visto."""
    salidas, cubiertas_todo, cubiertas = simular_nti(repo, _eventos_con_hueco(), poller_vivo=False)
    extra = (salidas & cubiertas_todo) - cubiertas
    assert len(extra) <= poll.NTI_CADA_N_VERSIONES - 1


def test_nti_modis_ritmo_lento_tambien_cubre(repo):
    ev = [(T0 + timedelta(hours=h), 1) for h in (1, 3, 13, 15, 25, 27, 37, 39, 49, 51, 61, 63, 73)]
    salidas, _, cubiertas = simular_nti(repo, ev)
    assert salidas and salidas - cubiertas == set()


# ---------------------------------------------------------------------------
# Gráficos acumulativos: uno por día


@pytest.mark.parametrize("sensor,kinds", [
    ("VIIRS375", ["VRP", "logVRP", "Dist"]),
    ("COMB", ["VRP", "logVRP", "Dist"]),
])
def test_graficos_uno_por_dia_y_el_indice_registra_lo_omitido(repo, sensor, kinds):
    tmp_path, srv, mp = repo
    horas = [0.7, 3.1, 5.9, 9.4, 17.2, 20.8, 23.9]
    n = 0
    for d in range(3):
        for h in horas:
            lm = T0.replace(hour=0, minute=0) + timedelta(days=d, hours=h)
            n += 1
            for k in kinds:
                srv.publicar(url(sensor, k), lm, SIG + json.dumps({"k": k, "n": n}).encode())
            consultar(mp, sensor)
    filas = filas_indice(tmp_path)
    for k in kinds:
        archivos = guardados(tmp_path, sensor, k)
        dias = [f.name[:8] for f in archivos]
        assert len(archivos) == 3 and len(set(dias)) == 3, dias
        mias = [r for r in filas if r["sensor"] == sensor and r["kind"] == k]
        # una fila por versión publicada: lo omitido queda registrado, no desaparece
        assert len(mias) == 3 * len(horas)
        omitidas = [r for r in mias if r.get("omitido")]
        assert len(omitidas) == 3 * len(horas) - 3
        assert all(r["png_path"] == "" for r in omitidas)
        assert all(r["md5"] and r["last_modified_utc"] for r in omitidas)
        assert all(not r.get("omitido") and r["png_path"] for r in mias if r not in omitidas)


def test_version_omitida_no_se_vuelve_a_bajar_si_no_cambia(repo):
    tmp_path, srv, mp = repo
    srv.publicar(url("VIIRS375", "VRP"), T0, SIG + b"a")
    consultar(mp, "VIIRS375")
    srv.publicar(url("VIIRS375", "VRP"), T0 + timedelta(hours=2), SIG + b"b")
    consultar(mp, "VIIRS375")
    consultar(mp, "VIIRS375")   # nada cambió en MIROVA
    filas = [r for r in filas_indice(tmp_path) if r["kind"] == "VRP"]
    assert len(filas) == 2


def test_dry_run_no_escribe(repo):
    tmp_path, srv, mp = repo
    srv.publicar(url("VIIRS375", "Latest10NTI"), T0, SIG + b"x")
    consultar(mp, "VIIRS375", dry_run=True)
    assert not (tmp_path / "index_png.csv").exists()
    assert guardados(tmp_path, "VIIRS375", "Latest10NTI") == []


def test_indice_viejo_se_migra_sin_tocar_sus_filas(repo):
    """El índice real tiene 166 mil filas con 8 columnas. Agregar `omitido` sólo cambia la
    cabecera: las filas viejas quedan byte a byte iguales y se leen con `omitido` vacío."""
    tmp_path, srv, mp = repo
    viejo = (
        "captured_at_utc,volcano,sensor,kind,last_modified_utc,md5,size_bytes,png_path\r\n"
        f"2026-10-31T23:00:00+00:00,{VOL},VIIRS375,VRP,2026-10-31T22:00:00+00:00,"
        f"abc,10,data/png/{VOL}/20261031_220000_VIIRS375_VRP.png\r\n"
    )
    (tmp_path / "index_png.csv").write_bytes(viejo.encode())
    srv.publicar(url("VIIRS375", "VRP"), T0, SIG + b"nuevo")
    consultar(mp, "VIIRS375")
    raw = (tmp_path / "index_png.csv").read_bytes().decode()
    lineas = raw.split("\r\n")
    assert lineas[0].endswith(",omitido")
    assert lineas[1] == viejo.split("\r\n")[1]
    filas = filas_indice(tmp_path)
    assert len(filas) == 2
    assert not filas[0].get("omitido")
    # día nuevo respecto de la última guardada: se guarda
    assert filas[1]["png_path"] and not filas[1].get("omitido")
