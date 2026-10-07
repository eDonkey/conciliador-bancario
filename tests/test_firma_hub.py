# -*- coding: utf-8 -*-
"""Identidad firmada del hub (hub/docs/FIRMA_ORBIT.md): un pedido con firma valida y un usuario entra sin
APP_PASSWORD; sin firma valida solo vale el login propio (contrasena)."""
import base64
import time
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

import app as app_mod
from engine import seguridad as seg

CLAVE_FIRMA = b"clave-de-prueba"
CLAVE_APP = "clave-de-prueba-larga"
REMOTO = ("203.0.113.5", 50000)
RUTA = "/api/equivalencias"
USUARIO = quote("jperez (Juan Pérez)")


def cliente(origen=REMOTO):
    return TestClient(app_mod.app, client=origen, follow_redirects=False)


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    for k in ("APP_PASSWORD", "SESSION_SECRET", "PROXY_MODE", "RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT",
              "HUB_FIRMA_CLAVE", "ORBIT_FIRMA_CLAVE", "HUB_CLAVE", "HUB_CLAVE_ANTERIOR", "ORBIT_FIRMA_TOLERANCIA"):
        monkeypatch.delenv(k, raising=False)
    seg.LIMITE_LOGIN.__init__()
    yield


def del_hub(metodo="GET", ruta=RUTA, clave=CLAVE_FIRMA, ahora=None, **extra):
    """Las cabeceras que mandaria el hub (con su firma), como las arma el proxy."""
    h = {"x-orbit": "1", "x-orbit-usuario": USUARIO, "x-orbit-permisos": "cargar,admin",
         "x-orbit-grupo": "Nave", **extra}
    return {**h, **seg.firmar(h, metodo, ruta, clave, ahora)}


# ---- contrato ---------------------------------------------------------------

def test_vector_de_prueba_del_hub():
    """El mismo vector fijo que hub/pruebas/seguridad.js y las demas apps."""
    h = {"x-orbit": "1", "x-orbit-usuario": "Ana%20P", "x-orbit-permisos": "cargar,ver"}
    assert seg.firmar(h, "GET", "/api/yo?x=1", CLAVE_FIRMA, 1790000000) == {
        "x-orbit-timestamp": "1790000000",
        "x-orbit-firma": "v1=9678680753278d1dbddc6b9a70a4e98df719a9e1845e4733110dfde1192cf3dc"}


def test_clave_derivada_de_hub_clave_igual_a_la_del_hub(monkeypatch):
    monkeypatch.setenv("HUB_CLAVE", base64.b64encode(b"x" * 32).decode())
    assert seg.claves_de_firma() == [bytes.fromhex(
        "8fa8b04e2c374760adee3c5c9254355c12133de737dab96125338ce07f7ccbd2")]


def test_rotacion_acepta_la_clave_anterior(monkeypatch):
    nueva, vieja = base64.b64encode(b"n" * 32).decode(), base64.b64encode(b"v" * 32).decode()
    monkeypatch.setenv("HUB_CLAVE", nueva)
    monkeypatch.setenv("HUB_CLAVE_ANTERIOR", vieja)
    ks = seg.claves_de_firma()
    assert len(ks) == 2
    monkeypatch.setenv("APP_PASSWORD", CLAVE_APP)
    c = cliente()
    assert c.get(RUTA, headers=del_hub(clave=ks[0])).status_code == 200
    assert c.get(RUTA, headers=del_hub(clave=ks[1])).status_code == 200
    assert c.get(RUTA, headers=del_hub(clave=b"otra")).status_code == 401


# ---- el hub abre la app sin contrasena ----------------------------------------

def test_con_firma_valida_entra_sin_app_password(monkeypatch):
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    monkeypatch.setenv("APP_PASSWORD", CLAVE_APP)
    c = cliente()
    assert c.get(RUTA).status_code == 401                              # sin identidad, login propio
    assert c.get(RUTA, headers=del_hub()).status_code == 200


def test_entra_aunque_no_haya_app_password(monkeypatch):
    """Antes el hub (y el tunel) recibian 503 sin APP_PASSWORD: con identidad firmada pasa."""
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    c = cliente()
    assert c.get(RUTA).status_code == 503
    assert c.get(RUTA, headers=del_hub()).status_code == 200
    assert c.get(RUTA, headers=del_hub(**{"x-forwarded-for": "8.8.8.8"})).status_code == 200


def test_la_pagina_y_los_estaticos_tambien(monkeypatch):
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    monkeypatch.setenv("APP_PASSWORD", CLAVE_APP)
    c = cliente()
    assert c.get("/", headers=del_hub(ruta="/")).status_code == 200
    assert c.get("/diario", headers=del_hub(ruta="/diario")).status_code == 200
    # /login con identidad del hub no tiene sentido: vuelve a la app
    r = c.get("/login", headers=del_hub(ruta="/login"))
    assert r.status_code == 303 and r.headers["location"] == "./"


def test_la_ruta_incluye_la_query(monkeypatch):
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    monkeypatch.setenv("APP_PASSWORD", CLAVE_APP)
    c = cliente()
    assert c.get(RUTA + "?marca=Fiat%20Verona", headers=del_hub(ruta=RUTA + "?marca=Fiat%20Verona")).status_code == 200
    # firmada para otra query: no sirve
    assert c.get(RUTA + "?marca=Jeep", headers=del_hub(ruta=RUTA + "?marca=Fiat")).status_code == 401


def test_la_identidad_se_lee_decodificada(monkeypatch):
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    h = {"x-orbit": "1", "x-orbit-usuario": USUARIO, "x-orbit-permisos": "cargar, admin"}
    h.update(seg.firmar(h, "GET", RUTA, CLAVE_FIRMA))
    req = type("R", (), {"headers": h, "method": "GET", "scope": {"raw_path": RUTA.encode(), "query_string": b""},
                         "url": type("U", (), {"path": RUTA})()})()
    assert seg.identidad_hub(req) == {"usuario": "jperez (Juan Pérez)", "permisos": ["cargar", "admin"]}


# ---- sin firma valida solo vale el login propio ---------------------------------

def test_sin_clave_de_firma_las_cabeceras_no_valen(monkeypatch):
    """Sin clave configurada nadie puede verificar al hub: X-Orbit-Usuario es un dato cualquiera."""
    monkeypatch.setenv("APP_PASSWORD", CLAVE_APP)
    c = cliente()
    assert c.get(RUTA, headers=del_hub(clave=CLAVE_FIRMA)).status_code == 401
    assert c.get(RUTA, headers={"x-orbit": "1", "x-orbit-usuario": USUARIO}).status_code == 401


def test_firma_mala_vencida_o_ajena(monkeypatch):
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    monkeypatch.setenv("APP_PASSWORD", CLAVE_APP)
    c = cliente()
    assert c.get(RUTA, headers=del_hub(clave=b"otra-clave")).status_code == 401
    assert c.get(RUTA, headers=del_hub(ahora=time.time() - 3600)).status_code == 401
    assert c.get(RUTA, headers=del_hub(ruta="/api/otra")).status_code == 401
    assert c.get(RUTA, headers={**del_hub(), "x-orbit-timestamp": "ayer"}).status_code == 401
    assert c.get(RUTA, headers={"x-orbit": "1", "x-orbit-usuario": USUARIO}).status_code == 401   # sin firma
    # el usuario o los permisos cambiados despues de firmar
    assert c.get(RUTA, headers={**del_hub(), "x-orbit-usuario": quote("root")}).status_code == 401
    assert c.get(RUTA, headers={**del_hub(), "x-orbit-permisos": "admin,todo"}).status_code == 401
    # la firma es del metodo: un GET firmado no sirve para un POST
    assert c.post("/api/conciliar", headers=del_hub()).status_code == 401


def test_sin_usuario_no_entra(monkeypatch):
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    monkeypatch.setenv("APP_PASSWORD", CLAVE_APP)
    h = {"x-orbit": "1", "x-orbit-permisos": "cargar"}
    h.update(seg.firmar(h, "GET", RUTA, CLAVE_FIRMA))
    assert cliente().get(RUTA, headers=h).status_code == 401
    # y sin X-Orbit: 1 tampoco
    h = {"x-orbit-usuario": USUARIO}
    h.update(seg.firmar(h, "GET", RUTA, CLAVE_FIRMA))
    assert cliente().get(RUTA, headers=h).status_code == 401


def test_el_login_propio_sigue_andando(monkeypatch):
    """Acceso directo (sin hub): contrasena y cookie como siempre, con o sin clave de firma."""
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    monkeypatch.setenv("APP_PASSWORD", CLAVE_APP)
    c = cliente()
    r = c.post("/login", json={"clave": CLAVE_APP})
    assert r.status_code == 200 and "set-cookie" in r.headers
    assert c.get(RUTA).status_code == 200
    assert cliente().post("/login", json={"clave": "mala"}).status_code == 401


def test_el_login_propio_sin_ninguna_clave_de_firma(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE_APP)
    c = cliente()
    assert c.post("/login", json={"clave": CLAVE_APP}).status_code == 200
    assert c.get(RUTA).status_code == 200


def test_csrf_sigue_valiendo_con_identidad_del_hub(monkeypatch):
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    c = cliente()
    h = del_hub("POST", "/api/migracion/importar")
    r = c.post("/api/migracion/importar", json={"archivos": {}}, headers={**h, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_el_navegador_no_puede_hacerse_pasar_por_el_hub(monkeypatch):
    """X-Orbit-* inventados, sin la clave: no entra, ni desde loopback detras de un proxy."""
    monkeypatch.setenv("HUB_FIRMA_CLAVE", CLAVE_FIRMA.decode())
    monkeypatch.setenv("APP_PASSWORD", CLAVE_APP)
    inventado = {"x-orbit": "1", "x-orbit-usuario": USUARIO, "x-orbit-permisos": "admin",
                 "x-orbit-timestamp": str(int(time.time())), "x-orbit-firma": "v1=" + "0" * 64}
    assert cliente(("127.0.0.1", 1)).get(RUTA, headers=inventado).status_code == 401
