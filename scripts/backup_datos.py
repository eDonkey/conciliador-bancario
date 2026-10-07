# -*- coding: utf-8 -*-
"""Copia de seguridad de datos/ (todo el estado de la app: conciliaciones,
reglas aprendidas, memoria de conciliados, cuentas, arrastre diario).

Uso:
    python scripts/backup_datos.py [--destino C:\backups] [--dias 30] [--con-secretos]

Genera <destino>/conciliador_datos_AAAAMMDD_HHMMSS.zip y borra los .zip de
este mismo nombre con más de --dias días. Por defecto NO incluye los archivos
con credenciales (anthropic_key.txt, fbs_sql.json): se vuelven a cargar desde
el entorno; --con-secretos los incluye (el zip queda sensible: protegerlo).

Pensado para colgarse del backup nocturno del servidor (ver AUDIT.md). Si
datos/ vive en un volumen (Railway), correrlo desde un shell del servicio o
programarlo con el cron del proveedor y bajar el zip fuera de Railway.
"""
import argparse
import os
import sys
import time
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATOS = os.environ.get("DATOS_DIR") or os.path.join(BASE, "datos")
SECRETOS = {"anthropic_key.txt", "fbs_sql.json"}
PREFIJO = "conciliador_datos_"


def hacer_backup(destino: str, dias: int = 30, con_secretos: bool = False,
                 origen: str = DATOS) -> str:
    if not os.path.isdir(origen):
        raise SystemExit(f"No existe la carpeta de datos: {origen}")
    os.makedirs(destino, exist_ok=True)
    nombre = os.path.join(destino, PREFIJO + time.strftime("%Y%m%d_%H%M%S") + ".zip")
    tmp = nombre + ".tmp"
    n = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for raiz, _, archivos in os.walk(origen):
            for a in archivos:
                if a in SECRETOS and not con_secretos:
                    continue
                ruta = os.path.join(raiz, a)
                z.write(ruta, os.path.relpath(ruta, origen))
                n += 1
    os.replace(tmp, nombre)
    limite = time.time() - dias * 86400
    for f in os.listdir(destino):
        if f.startswith(PREFIJO) and f.endswith(".zip"):
            ruta = os.path.join(destino, f)
            if os.path.getmtime(ruta) < limite:
                os.remove(ruta)
    print(f"Backup listo: {nombre} ({n} archivos)")
    return nombre


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--destino", default=os.environ.get("BACKUP_DESTINO", r"C:\backups"))
    ap.add_argument("--dias", type=int, default=30)
    ap.add_argument("--con-secretos", action="store_true")
    a = ap.parse_args()
    hacer_backup(a.destino, a.dias, a.con_secretos)
    sys.exit(0)
