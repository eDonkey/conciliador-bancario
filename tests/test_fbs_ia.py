# -*- coding: utf-8 -*-
import json

import pytest
from starlette.testclient import TestClient

import app as app_mod
from engine import cuentas as cuentas_mod
from engine import fbs_sql, presupuesto_ia
from engine import seguridad as seg

LOCAL = ("127.0.0.1", 50000)
REMOTO = ("203.0.113.5", 50000)
CLAVE = "clave-de-prueba-larga"


@pytest.fixture(autouse=True)
def aislado(monkeypatch, tmp_path):
    for v in ("APP_PASSWORD", "FBS_SQL_SERVIDOR", "FBS_SQL_PUERTO", "FBS_SQL_BASE",
              "FBS_SQL_USUARIO", "FBS_SQL_CLAVE", "FBS_SQL_EDITABLE",
              "FBS_SQL_HOSTS_PERMITIDOS", "FBS_SQL_PERMITIR_LOGIN_ESCRITURA",
              "IA_TOKENS_DIA", "IA_MODELO", "IA_MODELO_ANALISIS", "DEMO_MODE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(fbs_sql, "RUTA_CONF", str(tmp_path / "fbs_sql.json"))
    monkeypatch.setattr(fbs_sql, "_DATOS", str(tmp_path))
    monkeypatch.setattr(presupuesto_ia, "RUTA", str(tmp_path / "ia_presupuesto.json"))
    fbs_sql._LOGINS_VERIFICADOS.clear()


# ---- solo lectura -----------------------------------------------------------

@pytest.mark.parametrize("q", [
    "SELECT 1", "select a from t where x='a;b' -- ok",
    "WITH x AS (SELECT 1 a) SELECT * FROM x", "SELECT [update] FROM t",
    fbs_sql.QUERY_EJEMPLO, fbs_sql._query_detmov([("E", 1), ("O", 2)])])
def test_consultas_validas(q):
    fbs_sql._validar_solo_lectura(q)


@pytest.mark.parametrize("q", [
    "SELECT '--' ; DROP TABLE x",                 # el '--' de un literal no esconde nada
    "SELECT '--'\n; DROP TABLE x",
    "SELECT 1; DELETE FROM t",
    "SELECT 1 /* /* */ */ ; drop table t",        # comentarios anidados
    "SELECT * INTO nueva FROM t",
    "UPDATE t SET a=1", "exec sp_who", "SELECT 1 WAITFOR DELAY '0:0:5'",
    "DECLARE @a int; SELECT 1", "SELECT 1 /* sin cerrar", "select 1 where 'a",
    "", "   ", "BEGIN TRAN SELECT 1", "SELECT * FROM OPENROWSET('x')"])
def test_consultas_rechazadas(q):
    with pytest.raises(ValueError):
        fbs_sql._validar_solo_lectura(q)


def test_login_con_escritura_se_rechaza(monkeypatch):
    conf = {"servidor": "192.168.1.5", "puerto": 1433, "base": "b", "usuario": "u"}
    monkeypatch.setattr(fbs_sql, "_consultar_driver",
                        lambda c, q, p: [{"sysadmin": 1, "db_owner": 1,
                                          "db_datawriter": 0, "db_ddladmin": 0}])
    with pytest.raises(ValueError, match="solo lectura"):
        fbs_sql._consultar(conf, "SELECT 1", {})
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA", "1")
    fbs_sql._verificar_login_solo_lectura(conf)      # excepción consciente


def test_login_solo_lectura_pasa_y_se_cachea(monkeypatch):
    conf = {"servidor": "192.168.1.5", "puerto": 1433, "base": "b", "usuario": "u"}
    llamadas = []

    def falso(c, q, p):
        llamadas.append(q)
        if "IS_SRVROLEMEMBER" in q:
            return [{"sysadmin": 0, "db_owner": 0, "db_datawriter": 0, "db_ddladmin": 0}]
        return [{"uno": 1}]
    monkeypatch.setattr(fbs_sql, "_consultar_driver", falso)
    fbs_sql._consultar(conf, "SELECT 1", {})
    fbs_sql._consultar(conf, "SELECT 1", {})
    assert sum("IS_SRVROLEMEMBER" in q for q in llamadas) == 1


# ---- destino ----------------------------------------------------------------

@pytest.mark.parametrize("srv,puerto", [
    ("192.168.1.10", 1433), ("10.0.0.5", 14330), ("100.100.1.1", 1433),
    ("172.16.0.9\\SQLEXPRESS", 1433)])
def test_destino_privado_ok(srv, puerto):
    fbs_sql.validar_destino(srv, puerto)


@pytest.mark.parametrize("srv,puerto", [
    ("8.8.8.8", 1433), ("169.254.169.254", 80), ("169.254.169.254", 1433),
    ("127.0.0.1", 1433), ("0.0.0.0", 1433), ("224.0.0.1", 1433),
    ("host con espacios", 1433), ("a;b", 1433), ("", 1433),
    ("192.168.1.10", 443), ("192.168.1.10", 70000),
    ("192.168.1.10\\ins;tancia", 1433)])
def test_destino_invalido(srv, puerto):
    with pytest.raises(ValueError):
        fbs_sql.validar_destino(srv, puerto)


def test_destino_publico_con_lista_permitida(monkeypatch):
    monkeypatch.setenv("FBS_SQL_HOSTS_PERMITIDOS", "8.8.8.8, 127.0.0.0/8")
    fbs_sql.validar_destino("8.8.8.8", 1433)
    fbs_sql.validar_destino("127.0.0.1", 14330)
    monkeypatch.setenv("FBS_SQL_HOSTS_PERMITIDOS", "169.254.169.254")
    with pytest.raises(ValueError):                 # link-local nunca, ni en la lista
        fbs_sql.validar_destino("169.254.169.254", 1433)


def test_guardar_conf_destino(monkeypatch):
    base = {"servidor": "192.168.1.10", "puerto": 1433, "base": "fbs", "usuario": "u",
            "clave": "secreta", "query": "SELECT 1"}
    fbs_sql.guardar_conf(base)
    assert fbs_sql.cargar_conf()["clave"] == "secreta"
    # sin permiso de destino: cambiar el servidor se rechaza, la query se acepta
    with pytest.raises(PermissionError):
        fbs_sql.guardar_conf({"servidor": "10.9.9.9"}, permitir_destino=False)
    fbs_sql.guardar_conf({**base, "clave": "", "query": "SELECT 2"}, permitir_destino=False)
    assert fbs_sql.cargar_conf()["query"] == "SELECT 2"
    # cambiar de servidor sin clave nueva borra la clave guardada
    fbs_sql.guardar_conf({"servidor": "10.9.9.9"})
    c = fbs_sql.cargar_conf()
    assert c["servidor"] == "10.9.9.9" and c["clave"] == ""
    # destino inválido
    with pytest.raises(ValueError):
        fbs_sql.guardar_conf({"servidor": "8.8.8.8"})
    # destino fijado por el entorno
    monkeypatch.setenv("FBS_SQL_SERVIDOR", "192.168.1.10")
    with pytest.raises(PermissionError):
        fbs_sql.guardar_conf({"servidor": "10.1.1.1"})


def test_api_fbs_destino_requiere_local_o_editable(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", CLAVE)
    c = TestClient(app_mod.app, client=REMOTO, follow_redirects=False)
    c.cookies.set(seg.COOKIE, seg.emitir_sesion())
    cuerpo = {"servidor": "10.9.9.9", "puerto": 1433, "base": "b", "usuario": "u"}
    r = c.post("/api/fbs-sql", json=cuerpo)
    assert r.status_code == 403
    monkeypatch.setenv("FBS_SQL_EDITABLE", "1")
    assert c.post("/api/fbs-sql", json=cuerpo).status_code == 200
    assert "clave" not in c.get("/api/fbs-sql").json()


def test_migracion_no_exporta_la_conexion_fbs():
    assert not app_mod._migrable("fbs_sql.json")
    assert not app_mod._migrable("ia_presupuesto.json")
    assert app_mod._migrable("equivalencias.json")


# ---- presupuesto de IA ------------------------------------------------------

def test_presupuesto_aviso_y_tope(monkeypatch, capsys):
    monkeypatch.setenv("IA_TOKENS_DIA", "1000")
    presupuesto_ia.verificar("2026-10-07")
    presupuesto_ia.registrar(500, 100, "2026-10-07")            # 60 %
    assert not presupuesto_ia.estado("2026-10-07")["aviso"]
    assert "AVISO" not in capsys.readouterr().out
    e = presupuesto_ia.registrar(150, 50, "2026-10-07")         # 80 %
    assert e["aviso"] and not e["excedido"]
    assert "AVISO" in capsys.readouterr().out
    presupuesto_ia.registrar(10, 0, "2026-10-07")
    assert "AVISO" not in capsys.readouterr().out               # una sola vez por día
    presupuesto_ia.verificar("2026-10-07")                       # todavía hay margen
    presupuesto_ia.registrar(300, 0, "2026-10-07")
    with pytest.raises(presupuesto_ia.PresupuestoExcedido):
        presupuesto_ia.verificar("2026-10-07")
    presupuesto_ia.verificar("2026-10-08")                       # día nuevo, contador en cero


def test_tope_cero_deshabilita_la_ia(monkeypatch):
    monkeypatch.setenv("IA_TOKENS_DIA", "0")
    with pytest.raises(presupuesto_ia.PresupuestoExcedido):
        presupuesto_ia.verificar()


def test_conciliar_responde_429_sin_presupuesto(monkeypatch):
    monkeypatch.setenv("IA_TOKENS_DIA", "10")
    presupuesto_ia.registrar(10, 0)
    c = TestClient(app_mod.app, client=LOCAL, follow_redirects=False)
    r = c.post("/api/conciliar", data={"usar_ia": "si"},
               files={"extractos": ("a.pdf", b"x", "application/pdf")})
    assert r.status_code == 429 and "tope diario" in r.json()["error"]


def test_analizar_responde_429(monkeypatch):
    monkeypatch.setenv("IA_TOKENS_DIA", "10")
    presupuesto_ia.registrar(10, 0)
    monkeypatch.setattr(app_mod, "_obtener", lambda j: {
        "e_sin_banco": [{"id": "E#1", "referencia": "x", "comentario": "y"}],
        "o_pendientes_sin_banco": [], "banco_sin_contabilizar": []})
    monkeypatch.setattr(app_mod.ai_assist, "disponible", lambda: True)
    monkeypatch.setattr(app_mod.analisis_mod, "cargar", lambda: [])
    c = TestClient(app_mod.app, client=LOCAL, follow_redirects=False)
    r = c.post("/api/analizar/abc", json={"id_mayor": "E#1"})
    assert r.status_code == 429


def test_modelo_solo_por_config_del_servidor(monkeypatch):
    assert presupuesto_ia.modelo_matching() == "claude-sonnet-5"
    monkeypatch.setenv("IA_MODELO", "claude-haiku-4-5")
    assert presupuesto_ia.modelo_matching() == "claude-haiku-4-5"
    import inspect
    # ningún endpoint recibe el modelo desde el pedido
    assert "modelo" not in inspect.getsource(app_mod.api_conciliar)
    assert "modelo" not in inspect.getsource(app_mod.api_analizar)


# ---- cuentas fuera del código -----------------------------------------------

def test_semilla_de_cuentas_fuera_del_codigo(monkeypatch, tmp_path):
    import inspect
    assert "SEMILLA = [" not in inspect.getsource(cuentas_mod)
    monkeypatch.setattr(cuentas_mod, "RUTA_SEMILLA", str(tmp_path / "no_existe.json"))
    assert cuentas_mod._leer_semilla() == []
    ruta = tmp_path / "s.json"
    ruta.write_text(json.dumps([{"empresa": "E", "banco": "macro", "numero": "1", "moneda": "ARS"}]))
    monkeypatch.setattr(cuentas_mod, "RUTA_SEMILLA", str(ruta))
    assert cuentas_mod._leer_semilla() == [("E", "macro", "1", "ARS")]
    ruta.write_text("{roto")
    assert cuentas_mod._leer_semilla() == []


# ---- override temporal del login con escritura --------------------------------------------------

from datetime import date as _date, timedelta as _td


def _conf_login():
    return {"servidor": "192.168.1.5", "puerto": 1433, "base": "b", "usuario": "consultoraread"}


def _con_escritura(monkeypatch):
    monkeypatch.setattr(fbs_sql, "_consultar_driver",
                        lambda c, q, p: [{"sysadmin": 0, "db_owner": 0, "db_datawriter": 1, "db_ddladmin": 0}])
    fbs_sql._LOGINS_VERIFICADOS.clear()
    fbs_sql._LOGINS_CON_OVERRIDE.clear()
    for v in ("FBS_SQL_PERMITIR_LOGIN_ESCRITURA", "FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA"):
        monkeypatch.delenv(v, raising=False)


def _en(dias: int) -> str:
    return (_date.today() + _td(days=dias)).isoformat()


def test_override_temporal_vigente_pasa_y_avisa(monkeypatch, capsys):
    _con_escritura(monkeypatch)
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(7))
    fbs_sql._verificar_login_solo_lectura(_conf_login())
    salida = capsys.readouterr().out
    assert "ADVERTENCIA SEGURIDAD" in salida and "db_datawriter" in salida and "consultoraread" in salida
    d = fbs_sql.diagnostico()["override_login_escritura"]
    assert d["activo"] and d["dias_restantes"] == 7
    assert d["logins_con_escritura"][0]["permisos"] == ["db_datawriter"]


def test_override_aviso_una_vez_por_dia(monkeypatch, capsys):
    _con_escritura(monkeypatch)
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(3))
    fbs_sql._verificar_login_solo_lectura(_conf_login())
    capsys.readouterr()
    fbs_sql._verificar_login_solo_lectura(_conf_login())
    assert "ADVERTENCIA" not in capsys.readouterr().out


def test_override_vence_y_el_rechazo_vuelve(monkeypatch):
    _con_escritura(monkeypatch)
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(-1))
    with pytest.raises(ValueError, match="venció"):
        fbs_sql._verificar_login_solo_lectura(_conf_login())
    assert not fbs_sql.permite_login_con_escritura()


def test_override_hasta_hoy_inclusive(monkeypatch):
    _con_escritura(monkeypatch)
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(0))
    fbs_sql._verificar_login_solo_lectura(_conf_login())


def test_override_se_corta_al_cambiar_el_dia(monkeypatch):
    _con_escritura(monkeypatch)
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(2))
    fbs_sql._verificar_login_solo_lectura(_conf_login())
    clave = next(iter(fbs_sql._LOGINS_CON_OVERRIDE))
    fbs_sql._LOGINS_CON_OVERRIDE[clave]["fecha"] = _en(-1)      # el último aviso fue ayer: se vuelve a chequear
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(-1))
    with pytest.raises(ValueError, match="solo lectura"):
        fbs_sql._verificar_login_solo_lectura(_conf_login())


@pytest.mark.parametrize("valor", ["mañana", "2026-13-40", "", "31/12/2026"])
def test_override_fecha_invalida_falla_cerrado(monkeypatch, valor):
    _con_escritura(monkeypatch)
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", valor)
    with pytest.raises(ValueError, match="solo lectura"):
        fbs_sql._verificar_login_solo_lectura(_conf_login())


def test_override_no_puede_ser_largo(monkeypatch):
    _con_escritura(monkeypatch)
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(fbs_sql._OVERRIDE_MAX_DIAS + 1))
    with pytest.raises(ValueError, match="temporal"):
        fbs_sql._verificar_login_solo_lectura(_conf_login())
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(fbs_sql._OVERRIDE_MAX_DIAS))
    fbs_sql._verificar_login_solo_lectura(_conf_login())


def test_override_con_fecha_vencida_gana_sobre_el_flag_viejo(monkeypatch):
    _con_escritura(monkeypatch)
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA", "1")
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(-2))
    with pytest.raises(ValueError):
        fbs_sql._verificar_login_solo_lectura(_conf_login())


def test_flag_viejo_sigue_andando_y_se_marca_sin_vencimiento(monkeypatch, capsys):
    _con_escritura(monkeypatch)
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA", "1")
    fbs_sql._verificar_login_solo_lectura(_conf_login())
    assert "SIN VENCIMIENTO" in capsys.readouterr().out
    assert fbs_sql.diagnostico()["override_login_escritura"]["sin_vencimiento"] is True


def test_override_no_toca_la_validacion_de_las_consultas(monkeypatch):
    _con_escritura(monkeypatch)
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(5))
    with pytest.raises(ValueError, match="solo lectura"):
        fbs_sql._validar_solo_lectura("UPDATE t SET a=1")


def test_login_limpio_con_override_no_avisa(monkeypatch, capsys):
    monkeypatch.setattr(fbs_sql, "_consultar_driver",
                        lambda c, q, p: [{"sysadmin": 0, "db_owner": 0, "db_datawriter": 0, "db_ddladmin": 0}])
    fbs_sql._LOGINS_VERIFICADOS.clear()
    fbs_sql._LOGINS_CON_OVERRIDE.clear()
    monkeypatch.setenv("FBS_SQL_PERMITIR_LOGIN_ESCRITURA_HASTA", _en(5))
    fbs_sql._verificar_login_solo_lectura(_conf_login())
    assert "ADVERTENCIA" not in capsys.readouterr().out
