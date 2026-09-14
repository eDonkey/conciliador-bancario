# -*- coding: utf-8 -*-
"""Copia SOLO los aprendizajes desde el conciliador de Railway (nube) al
conciliador de esta PC: equivalencias de vocabulario, terminos de gastos,
reglas aprendidas, analisis confirmados, reclasificaciones, vetos y mapeos
de cuentas. NO toca las corridas locales (tableros, memoria, arrastre).

Uso (con el conciliador local corriendo):
    python migrar_aprendizajes.py                  -> importa en localhost:8766
    python migrar_aprendizajes.py http://otra:8765 -> otro destino
"""
import json
import sys
import urllib.request

ORIGEN = "https://conciliador-bancario-production-bdae.up.railway.app"
DESTINO = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8766").rstrip("/")

APRENDIZAJES = (
    "equivalencias.json",        # vocabulario extracto <-> sistema (haberes=sueldos, etc.)
    "gastos_usuario.json",       # terminos de gastos bancarios cargados por el cliente
    "reglas_aprendidas.json",    # reglas de matching aprendidas de cruces manuales
    "analisis_aprendidos.json",  # veredictos/correcciones de los analisis con IA
    "analisis_cache.json",       # analisis ya pagados (para no volver a gastar API)
    "reclasificaciones.json",    # movimientos transferidos a mano entre solapas
    "cruces_vetados.json",       # pares anulados que no deben volver a cruzarse
    "cuentas_nave.json",         # mapeos cuenta contable <-> cuenta bancaria
)

print(f"Descargando aprendizajes de {ORIGEN} ...")
with urllib.request.urlopen(f"{ORIGEN}/api/migracion/exportar", timeout=90) as r:
    bundle = json.load(r)

archivos = {k: v for k, v in bundle.get("archivos", {}).items() if k in APRENDIZAJES}
if not archivos:
    sys.exit("El export de Railway no trajo ningun archivo de aprendizaje.")
for k in APRENDIZAJES:
    v = archivos.get(k)
    estado = f"{len(v)} entradas" if isinstance(v, (list, dict)) else "no existe en Railway"
    print(f"  {k:26s} {estado}")

print(f"\nImportando en {DESTINO} (solo estos archivos; las corridas locales no se tocan) ...")
cuerpo = json.dumps({"archivos": archivos, "forzar": True}).encode("utf-8")
req = urllib.request.Request(f"{DESTINO}/api/migracion/importar", data=cuerpo,
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=90) as r:
        res = json.load(r)
except Exception as exc:  # noqa: BLE001
    sys.exit(f"No pude importar en {DESTINO}: {exc}\n"
             "¿Esta corriendo el conciliador en esta PC (iniciar_8766.bat)?")
print(f"Listo: {res.get('archivos_importados')} archivos importados. "
      "Recargá la página del conciliador y volvé a correr la conciliación.")
