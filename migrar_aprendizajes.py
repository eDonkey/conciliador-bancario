# -*- coding: utf-8 -*-
"""Copies ONLY the learned data from the reconciler on Railway (cloud) to the
reconciler on this PC: vocabulary equivalences, fee terms, learned rules,
confirmed analyses, reclassifications, vetoes and account mappings. It does
NOT touch the local runs (boards, memory, carry-over).

Usage (with the local reconciler running):
    python migrar_aprendizajes.py                  -> imports into localhost:8766
    python migrar_aprendizajes.py http://otra:8765 -> another destination
"""
import json
import sys
import urllib.request

SOURCE = "https://conciliador-bancario-production-bdae.up.railway.app"
DESTINATION = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8766").rstrip("/")

LEARNED_FILES = (
    "equivalencias.json",        # statement <-> system vocabulary (haberes=sueldos, etc.)
    "gastos_usuario.json",       # bank fee terms loaded by the client
    "reglas_aprendidas.json",    # matching rules learned from manual matches
    "analisis_aprendidos.json",  # verdicts/corrections of the AI analyses
    "analisis_cache.json",       # analyses already paid for (so the API isn't spent again)
    "reclasificaciones.json",    # movements moved by hand between tabs
    "cruces_vetados.json",       # cancelled pairs that must not be matched again
    "cuentas_nave.json",         # ledger account <-> bank account mappings
)

print(f"Descargando aprendizajes de {SOURCE} ...")
with urllib.request.urlopen(f"{SOURCE}/api/migracion/exportar", timeout=90) as r:
    bundle = json.load(r)

files = {k: v for k, v in bundle.get("archivos", {}).items() if k in LEARNED_FILES}
if not files:
    sys.exit("El export de Railway no trajo ningun archivo de aprendizaje.")
for k in LEARNED_FILES:
    v = files.get(k)
    state = f"{len(v)} entradas" if isinstance(v, (list, dict)) else "no existe en Railway"
    print(f"  {k:26s} {state}")

print(f"\nImportando en {DESTINATION} (solo estos archivos; las corridas locales no se tocan) ...")
body = json.dumps({"archivos": files, "forzar": True}).encode("utf-8")
req = urllib.request.Request(f"{DESTINATION}/api/migracion/importar", data=body,
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=90) as r:
        res = json.load(r)
except Exception as exc:  # noqa: BLE001
    sys.exit(f"No pude importar en {DESTINATION}: {exc}\n"
             "¿Esta corriendo el conciliador en esta PC (iniciar_8766.bat)?")
print(f"Listo: {res.get('archivos_importados')} archivos importados. "
      "Recargá la página del conciliador y volvé a correr la conciliación.")
