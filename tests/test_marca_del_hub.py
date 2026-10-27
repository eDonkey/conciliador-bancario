# -*- coding: utf-8 -*-
"""La marca con la que entró el usuario manda sobre el ?marca= de la URL.

Reportado desde Nave: estando en LEAP, la lista de conciliaciones anteriores
mostraba las de todas las cuentas de todas las empresas. El filtro dependía
solo de la query, así que alcanzaba con que un link la perdiera. La marca de
la ruta por marca del hub viaja firmada en X-Orbit-Marca en TODOS los pedidos.
"""
import json
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

import app as app_mod
from engine import cuentas as cuentas_mod
from engine import seguridad as seg

CLAVE_FIRMA = b"clave-de-prueba"
USUARIO = quote("jperez (Juan Pérez)")

CUENTAS = [
    {"id": "frances-1090648589", "empresa": "LEAP - Lemans", "banco": "frances",
     "numero": "109-064858/9", "moneda": "ARS"},
    {"id": "frances-1090355313", "empresa": "Renault - Lumiere", "banco": "frances",
     "numero": "109-035531/3", "moneda": "ARS"},
]

JOBS = {
    "aaaaaaaaaaaa": {"marca": "LEAP - Lemans", "resumen": {"movimientos_banco": 63},
                     "extractos": [{"cuenta": "1090648589", "banco": "frances",
                                    "desde": "2026-09-01", "hasta": "2026-09-30"}]},
    "bbbbbbbbbbbb": {"marca": "Renault - Lumiere", "resumen": {"movimientos_banco": 124},
                     "extractos": [{"cuenta": "1090355313", "banco": "frances",
                                    "desde": "2026-09-01", "hasta": "2026-09-30"}]},
}


@pytest.fixture(autouse=True)
def entorno(monkeypatch, tmp_path):
    for k in ("APP_PASSWORD", "SESSION_SECRET", "HUB_FIRMA_CLAVE", "HUB_CLAVE",
              "ORBIT_FIRMA_CLAVE", "DEMO_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    for nombre, datos in JOBS.items():
        (tmp_path / f"{nombre}.json").write_text(json.dumps(datos), encoding="utf-8")
    monkeypatch.setattr(app_mod, "DATOS_DIR", str(tmp_path))
    monkeypatch.setattr(cuentas_mod, "cargar", lambda: CUENTAS)
    yield


def pedir(ruta, marca=""):
    """Un GET como lo manda el hub por la ruta de una marca."""
    h = {"x-orbit": "1", "x-orbit-usuario": USUARIO, "x-orbit-permisos": "cargar"}
    if marca:
        h["x-orbit-marca"] = quote(marca)
    h.update(seg.firmar(h, "GET", ruta, CLAVE_FIRMA))
    c = TestClient(app_mod.app, client=("203.0.113.5", 50000), follow_redirects=False)
    return c.get(ruta, headers=h)


def cuentas_del_historial(r):
    return sorted(c for f in r.json()["conciliaciones"] for c in f["cuentas"])


def test_el_historial_se_acota_aunque_la_url_pierda_la_marca():
    r = pedir("/api/historial", marca="LEAP - Lemans")
    assert r.status_code == 200
    assert cuentas_del_historial(r) == ["1090648589"]


def test_sin_marca_del_hub_se_ven_todas():
    assert cuentas_del_historial(pedir("/api/historial")) == ["1090355313", "1090648589"]


def test_la_marca_de_la_url_no_puede_ampliar_la_del_hub():
    # entrando como LEAP, pedir otra marca a mano no muestra sus conciliaciones
    r = pedir("/api/historial?marca=Renault%20-%20Lumiere", marca="LEAP - Lemans")
    assert cuentas_del_historial(r) == ["1090648589"]


def test_las_cuentas_tambien_se_acotan():
    r = pedir("/api/cuentas", marca="LEAP - Lemans")
    assert [c["id"] for c in r.json()["cuentas"]] == ["frances-1090648589"]
    assert r.json()["marca_nombre"] == "LEAP - Lemans"


def test_sin_hub_sigue_valiendo_la_query(monkeypatch):
    monkeypatch.delenv("HUB_FIRMA_CLAVE", raising=False)
    c = TestClient(app_mod.app, client=("127.0.0.1", 50000), follow_redirects=False)
    r = c.get("/api/historial?marca=Renault%20-%20Lumiere")
    assert cuentas_del_historial(r) == ["1090355313"]
