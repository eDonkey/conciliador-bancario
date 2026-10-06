"""Security fixes from the platform-standards audit (docs/AUDIT-standards.md): ids can't escape datos/, the FBS
password from the environment is never written to disk, the demo can't export data or reach a real FBS, headers,
hidden OpenAPI, JS-safe inline handler arguments."""
import json
import os

import pytest

import app as app_module
from conftest import DATA_DIR
from engine import fbs_sql

OUTSIDE = os.path.join(os.path.dirname(DATA_DIR), "secreto.json")


@pytest.fixture
def outside_file():
    with open(OUTSIDE, "w", encoding="utf-8") as f:
        json.dump({"banco_sin_contabilizar": [], "gastos_bancarios": [], "resumen": {}, "secreto": 1}, f)
    yield OUTSIDE
    if os.path.exists(OUTSIDE):
        os.remove(OUTSIDE)


@pytest.mark.parametrize("bad_id", ["..\\secreto", "fbs_sql", "cuentas_nave", "ABCDEF123456", "0123456789abc"])
def test_result_ids_must_be_job_ids(client, outside_file, bad_id):
    # a backslash is already refused by the access middleware (400); the rest by the id check (404)
    assert client.get(f"/api/resultado/{bad_id}").status_code in (400, 404)
    assert client.get(f"/api/exportar/{bad_id}").status_code in (400, 404)


def test_job_id_in_body_cannot_reach_files_outside_datos(client, outside_file):
    r = client.post("/api/gastos", json={"termino": "comision rara", "job_id": "../secreto"})
    assert r.status_code == 200
    assert r.json()["datos"] is None
    with open(outside_file, encoding="utf-8") as f:
        assert json.load(f)["secreto"] == 1


@pytest.mark.parametrize("bad_id", ["..\\secreto", "..%5Csecreto", "memoria0000"])
def test_daily_run_ids_are_validated(client, outside_file, bad_id):
    assert client.get(f"/api/diario/{bad_id}").status_code in (400, 404)
    assert client.delete(f"/api/diario/{bad_id}").status_code in (400, 404)
    assert os.path.exists(outside_file)


def test_fbs_password_from_env_is_not_written_to_disk(client, monkeypatch):
    for var, value in (("FBS_SQL_SERVIDOR", "10.0.0.5"), ("FBS_SQL_BASE", "FBS"),
                       ("FBS_SQL_USUARIO", "lector"), ("FBS_SQL_CLAVE", "de-entorno")):
        monkeypatch.setenv(var, value)
    r = client.post("/api/fbs-sql", json={"query": "SELECT 'E' AS hoja, '1' AS codigo, 1 AS asiento"})
    assert r.status_code == 200, r.text
    with open(fbs_sql.RUTA_CONF, encoding="utf-8") as f:
        saved = f.read()
    assert "de-entorno" not in saved
    assert "10.0.0.5" not in saved


def test_demo_does_not_export_nor_reach_a_real_fbs(client, monkeypatch):
    monkeypatch.setattr(app_module, "DEMO_MODE", True)
    monkeypatch.setenv("DEMO_MODE", "true")
    assert client.get("/api/migracion/exportar").status_code == 404
    assert client.post("/api/migracion/importar", json={"archivos": {}}).status_code == 404
    conf = {"servidor": "10.0.0.5", "puerto": 1433, "base": "FBS", "usuario": "lector", "clave": "x"}
    with pytest.raises(ValueError):
        fbs_sql._consultar(conf, "SELECT 1 AS uno", {})


def test_security_headers_and_no_api_description(client):
    r = client.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "same-origin"
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404


def test_inline_handlers_get_js_safe_arguments():
    """Bank descriptions and ids reach inline onclick handlers: they must go through jsArg()."""
    for page in ("index.html", "diario.html"):
        with open(os.path.join(app_module.BASE_DIR, "static", page), encoding="utf-8") as f:
            html = f.read()
        assert "const jsArg = " in html
        assert "onclick=\"marcarGasto('" not in html
        assert "('${" not in html
