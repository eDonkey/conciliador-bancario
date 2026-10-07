# -*- coding: utf-8 -*-
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import app as app_mod
from engine import seguridad as seg

CLAVE = "clave-de-prueba-larga"
REMOTO = ("203.0.113.5", 50000)
LOCAL = ("127.0.0.1", 50000)


def cliente(origen=REMOTO, **kw):
    return TestClient(app_mod.app, client=origen, follow_redirects=False, **kw)


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("PROXY_MODE", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    seg.LIMITE_LOGIN.__init__()
    yield


def falsa(peer, **headers):
    return SimpleNamespace(client=SimpleNamespace(host=peer),
                           headers={k.lower().replace("_", "-"): v for k, v in headers.items()})


# ---- sin APP_PASSWORD: solo la propia máquina ------------------------------

def test_sin_clave_rechaza_remoto():
    r = cliente().get("/api/equivalencias")
    assert r.status_code == 503
    assert "APP_PASSWORD" in r.json()["error"]
    assert cliente().get("/").status_code == 503


def test_sin_clave_permite_loopback_directo():
    assert cliente(LOCAL).get("/api/equivalencias").status_code == 200


def test_sin_clave_loopback_detras_de_proxy_se_rechaza():
    # el hub o el túnel conectan por loopback pero agregan encabezados de proxy
    c = cliente(LOCAL)
    assert c.get("/api/equivalencias", headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 503
    assert c.get("/api/equivalencias", headers={"CF-Connecting-IP": "8.8.8.8"}).status_code == 503
    assert c.get("/api/equivalencias", headers={"X-Forwarded-Host": "x"}).status_code == 503


def test_health_es_publico():
    assert cliente().get("/health").json() == {"ok": True}


# ---- con APP_PASSWORD ------------------------------------------------------

def test_con_clave_exige_login(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    c = cliente(LOCAL)
    assert c.get("/api/equivalencias").status_code == 401
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "login"
    r = c.get("/diario")
    assert r.headers["location"] == "login?next=diario"
    assert c.post("/api/conciliar").status_code == 401
    assert c.get("/static/index.html").status_code == 303
    assert c.get("/login").status_code == 200


def test_login_ok_y_cookie(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    c = cliente()
    r = c.post("/login", content="clave=" + CLAVE,
               headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 303 and r.headers["location"] == "./"
    setc = r.headers["set-cookie"]
    assert "HttpOnly" in setc and "samesite=lax" in setc.lower() and "Max-Age" in setc
    assert c.get("/api/equivalencias").status_code == 200


def test_login_https_marca_secure(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    r = cliente().post("/login", json={"clave": CLAVE},
                       headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200 and "Secure" in r.headers["set-cookie"]


def test_login_clave_incorrecta(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    c = cliente()
    r = c.post("/login", json={"clave": "mala"})
    assert r.status_code == 401
    assert "set-cookie" not in r.headers


def test_login_limita_intentos_y_no_confia_en_xff(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    monkeypatch.setenv("PROXY_MODE", "railway")
    c = cliente(("10.0.0.1", 1))
    # el atacante cambia el primer valor de X-Forwarded-For en cada intento,
    # pero Railway agrega su IP real al final: es la que cuenta
    for i in range(10):
        r = c.post("/login", json={"clave": "mala"},
                   headers={"X-Forwarded-For": f"1.1.1.{i}, 198.51.100.7"})
        assert r.status_code == 401
    r = c.post("/login", json={"clave": CLAVE},
               headers={"X-Forwarded-For": "9.9.9.9, 198.51.100.7"})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_sesion_vencida_o_adulterada(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    ahora = time.time()
    vieja = seg.emitir_sesion(ahora - 13 * 3600)       # vida 12 h
    assert seg.leer_sesion(vieja) is None
    buena = seg.emitir_sesion()
    assert seg.leer_sesion(buena)
    cuerpo, firma = buena.split(".")
    cambiada = ("B" if firma[0] == "A" else "A") + firma[1:]
    assert seg.leer_sesion(cuerpo + "." + cambiada) is None
    assert seg.leer_sesion("x." + firma) is None
    c = cliente()
    c.cookies.set(seg.COOKIE, vieja)
    assert c.get("/api/equivalencias").status_code == 401
    c.cookies.set(seg.COOKIE, buena)
    assert c.get("/api/equivalencias").status_code == 200


def test_sesion_tope_absoluto(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    ahora = time.time()
    s = seg.emitir_sesion(ahora, inicio=ahora - 8 * 86400)   # tope 7 días
    assert seg.leer_sesion(s) is None


def test_cambiar_la_clave_invalida_sesiones(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    s = seg.emitir_sesion()
    monkeypatch.setenv("APP_PASSWORD", "otra-clave")
    assert seg.leer_sesion(s) is None


def test_csrf_origen_distinto(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    c = cliente()
    c.cookies.set(seg.COOKIE, seg.emitir_sesion())
    r = c.post("/api/migracion/importar", json={"archivos": {}},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_ruta_con_traversal(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    c = cliente(LOCAL)
    assert c.get("/api/resultado/..%5Csecreto").status_code == 400


# ---- IP del cliente --------------------------------------------------------

def test_ip_railway_usa_el_ultimo_valor(monkeypatch):
    monkeypatch.setenv("PROXY_MODE", "railway")
    r = falsa("100.64.0.2", x_forwarded_for="6.6.6.6, 5.5.5.5, 198.51.100.7")
    assert seg.ip_cliente(r) == "198.51.100.7"
    assert seg.ip_cliente(falsa("100.64.0.2", x_forwarded_for="basura")) == "100.64.0.2"
    # CF-Connecting-IP no vale en Railway (lo puede mandar cualquiera)
    assert seg.ip_cliente(falsa("100.64.0.2", cf_connecting_ip="6.6.6.6")) == "100.64.0.2"


def test_ip_cloudflare_solo_desde_loopback(monkeypatch):
    monkeypatch.setenv("PROXY_MODE", "cloudflare")
    assert seg.ip_cliente(falsa("127.0.0.1", cf_connecting_ip="198.51.100.9")) == "198.51.100.9"
    assert seg.ip_cliente(falsa("203.0.113.5", cf_connecting_ip="198.51.100.9")) == "203.0.113.5"
    assert seg.ip_cliente(falsa("127.0.0.1", x_forwarded_for="6.6.6.6")) == "127.0.0.1"


def test_modo_proxy_autodetecta_railway(monkeypatch):
    assert seg.modo_proxy() == "cloudflare"
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "x")
    assert seg.modo_proxy() == "railway"
