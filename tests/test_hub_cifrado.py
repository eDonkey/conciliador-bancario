# -*- coding: utf-8 -*-
"""Descifrado de las contraseñas de FBS que cifra el hub (hub/cifrado.js, HUB_FBS_CIFRADO=1).
El vector fijo lo genera el hub (hub/pruebas/generar-vector-fbs.js) con su propio cifrar():
si Python lo descifra, el formato coincide byte a byte."""
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import hub_cifrado as hc  # noqa: E402

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vector_fbs_cifrado.json"), encoding="utf-8") as f:
    V = json.load(f)


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setenv("HUB_CLAVE", V["hub_clave"])
    monkeypatch.delenv("HUB_CLAVE_ANTERIOR", raising=False)


@pytest.mark.parametrize("caso", V["casos"])
def test_descifra_lo_que_cifra_el_hub(caso):
    assert caso["cifrado_actual"].startswith("v1:")
    assert hc.descifrar_fbs(caso["cifrado_actual"]) == caso["claro"]


def test_texto_plano_pasa_igual(monkeypatch):
    assert hc.descifrar_fbs("sin-prefijo") == "sin-prefijo"
    assert hc.descifrar_fbs("") == ""
    assert hc.descifrar_fbs(None) == ""
    monkeypatch.delenv("HUB_CLAVE")          # el texto plano no necesita clave
    assert hc.descifrar_fbs("sin-prefijo") == "sin-prefijo"


def test_cifrado_con_clave_anterior(monkeypatch):
    c = V["cifrado_con_anterior"]
    with pytest.raises(hc.ErrorCifrado):
        hc.descifrar_fbs(c["cifrado"])
    monkeypatch.setenv("HUB_CLAVE_ANTERIOR", V["hub_clave_anterior"])
    assert hc.descifrar_fbs(c["cifrado"]) == c["claro"]
    assert hc.descifrar_fbs(V["casos"][0]["cifrado_actual"]) == V["casos"][0]["claro"]


def test_rotacion_solo_la_vieja_en_hub_clave(monkeypatch):
    monkeypatch.setenv("HUB_CLAVE", V["hub_clave_anterior"])
    monkeypatch.setenv("HUB_CLAVE_ANTERIOR", V["hub_clave"])
    assert hc.descifrar_fbs(V["casos"][1]["cifrado_actual"]) == "simple"


def test_falta_hub_clave_error_claro_sin_secretos(monkeypatch):
    monkeypatch.delenv("HUB_CLAVE")
    caso = V["casos"][0]
    with pytest.raises(hc.ErrorCifrado) as e:
        hc.descifrar_fbs(caso["cifrado_actual"])
    assert "HUB_CLAVE" in str(e.value)
    assert caso["cifrado_actual"] not in str(e.value) and caso["claro"] not in str(e.value)


def test_clave_equivocada_o_dato_danado_error_sin_secretos(monkeypatch):
    caso = V["casos"][0]
    monkeypatch.setenv("HUB_CLAVE", base64.b64encode(b"z" * 32).decode())
    with pytest.raises(hc.ErrorCifrado) as e:
        hc.descifrar_fbs(caso["cifrado_actual"])
    msg = str(e.value)
    assert V["hub_clave"] not in msg and caso["claro"] not in msg and caso["cifrado_actual"] not in msg
    monkeypatch.setenv("HUB_CLAVE", V["hub_clave"])
    roto = caso["cifrado_actual"][:-4] + "AAAA"          # tag/datos alterados
    with pytest.raises(hc.ErrorCifrado):
        hc.descifrar_fbs(roto)
    with pytest.raises(hc.ErrorCifrado):
        hc.descifrar_fbs("v1:no-es-base64!")
    with pytest.raises(hc.ErrorCifrado):
        hc.descifrar_fbs("v1:AAAA")                      # más corto que iv + tag


def test_hub_clave_invalida_no_vale(monkeypatch):
    monkeypatch.setenv("HUB_CLAVE", "esto no es base64!")
    with pytest.raises(hc.ErrorCifrado):
        hc.descifrar_fbs(V["casos"][0]["cifrado_actual"])


def test_la_conexion_usa_la_clave_descifrada(monkeypatch):
    """El módulo que conecta al FBS recibe la clave del hub tal cual (cifrada) y la descifra al conectar."""
    import types
    from engine import fbs_sql as mod
    visto = {}
    falso = types.SimpleNamespace(connect=lambda **kw: visto.update(kw) or "con")
    monkeypatch.setitem(sys.modules, "pymssql", falso)
    conf = {"servidor": "127.0.0.1", "puerto": 1433, "base": "b", "usuario": "u",
            "clave": V["casos"][0]["cifrado_actual"]}
    assert mod._conectar(conf) == "con"
    assert visto["password"] == V["casos"][0]["claro"]
    assert conf["clave"].startswith("v1:")                   # el dict en memoria sigue cifrado
    visto.clear()
    mod._conectar({**conf, "clave": "plana"})
    assert visto["password"] == "plana"


def test_la_config_publica_no_expone_la_clave(monkeypatch):
    from engine import fbs_sql
    cif = V["casos"][0]["cifrado_actual"]
    cfg = {"cuenta_id": "x-1-ars", "etiqueta": "X", "conexion_nombre": "C", "empresa": "",
           "conexion": {"servidor": "s", "puerto": 1433, "base": "b", "usuario": "u", "clave": cif}}
    monkeypatch.setattr(fbs_sql, "cuentas_fbs", lambda: [cfg])
    salida = json.dumps(fbs_sql.publica(""), ensure_ascii=False)
    assert cif not in salida and V["casos"][0]["claro"] not in salida
