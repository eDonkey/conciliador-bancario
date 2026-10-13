"""Test setup. The app keeps its state as JSON files in datos/ next to app.py and has no setting to move
it, so the tests run a throwaway copy of the repo: whatever they write lands in a temp folder. No hub
database, no Anthropic key, no telemetry, no DEMO_MODE. The bank account seed is one fictitious account
written here; synthetic statements and ledgers come from engine/simulacion.py."""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_DIR = tempfile.mkdtemp(prefix="conciliador-tests-")
APP_DIR = os.path.join(TMP_DIR, "app")
shutil.copytree(ROOT, APP_DIR, ignore=shutil.ignore_patterns(
    ".git", ".venv", "venv", "datos", "tests", "__pycache__", ".env", ".env.*", "cuentas_semilla.json"))

SEED_ACCOUNT = {"empresa": "Rumbo Motors", "banco": "ciudad", "numero": "350123456789", "moneda": "ARS"}
SEED_PATH = os.path.join(TMP_DIR, "cuentas_semilla.json")
with open(SEED_PATH, "w", encoding="utf-8") as f:
    json.dump([SEED_ACCOUNT], f)

os.environ["MONITOR_ACTIVO"] = "0"
os.environ["CUENTAS_SEMILLA_RUTA"] = SEED_PATH
for var in ("DATABASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DEMO_MODE", "PORT",
            "NEW_RELIC_LICENSE_KEY", "OTEL_EXPORTER_OTLP_ENDPOINT", "ANALISIS_MAX_DIA", "RAILWAY_GIT_COMMIT_SHA",
            "FBS_SQL_SERVIDOR", "FBS_SQL_PUERTO", "FBS_SQL_BASE", "FBS_SQL_USUARIO", "FBS_SQL_CLAVE",
            "APP_PASSWORD", "SESSION_SECRET", "HUB_CLAVE", "HUB_CLAVE_ANTERIOR", "HUB_FIRMA_CLAVE",
            "ORBIT_FIRMA_CLAVE"):
    os.environ.pop(var, None)
sys.path.insert(0, APP_DIR)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402

DATA_DIR = app_module.DATA_DIR


def _empty_data_dir():
    for name in os.listdir(DATA_DIR):
        path = os.path.join(DATA_DIR, name)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    for store in (app_module.RESULTS, app_module.PROGRESS, app_module.STAGING):
        store.clear()


@pytest.fixture
def client():
    """A client on the app's own machine (loopback, no proxy headers): allowed without APP_PASSWORD."""
    _empty_data_dir()
    with TestClient(app_module.app, client=("127.0.0.1", 50000)) as c:
        yield c
