# -*- coding: utf-8 -*-
r"""Variables de entorno desde un archivo fuera del repo.

El conciliador toma su configuración del ENTORNO del proceso (APP_PASSWORD, HUB_CLAVE, FBS_SQL_*, ...). Para no tener
que ponerlas a mano en cada consola, al arrancar lee un archivo de texto con líneas NOMBRE=valor:

    CONCILIADOR_ENV=<ruta>        archivo a leer (si no, <carpeta padre>\<nombre de la carpeta>.env,
                                  p. ej. C:\apps\conciladorfbs.env para C:\apps\conciladorfbs)

Reglas: las variables que ya están en el entorno ganan (el archivo solo completa); las líneas vacías y las que
empiezan con # se ignoran; se acepta `set NOMBRE=valor` y `export NOMBRE=valor`; el valor puede ir entre comillas; un
# dentro de un valor es parte del valor (una contraseña puede tenerlo). Solo se imprimen NOMBRES, nunca valores.
Tiene que cargarse antes que cualquier otro módulo: se llama en las primeras líneas de app.py.
"""
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NOMBRE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_ESTADO = {"archivo": None, "cargadas": [], "respetadas": [], "invalidas": []}


def ruta_por_defecto() -> str:
    return os.path.join(os.path.dirname(BASE_DIR), os.path.basename(BASE_DIR) + ".env")


def leer(ruta: str) -> tuple[dict, list]:
    """Devuelve ({nombre: valor}, [números de línea inválidos]). Lanza OSError si no se puede abrir."""
    valores, invalidas = {}, []
    with open(ruta, encoding="utf-8-sig") as f:
        for n, crudo in enumerate(f, 1):
            lin = crudo.strip()
            if not lin or lin.startswith("#"):
                continue
            for prefijo in ("export ", "set "):
                if lin.lower().startswith(prefijo):
                    lin = lin[len(prefijo):].lstrip()
                    break
            nombre, igual, valor = lin.partition("=")
            nombre, valor = nombre.strip(), valor.strip()
            if not igual or not _NOMBRE.match(nombre):
                invalidas.append(n)
                continue
            if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in ("'", '"'):
                valor = valor[1:-1]
            valores[nombre] = valor
    return valores, invalidas


def cargar(entorno=None) -> dict:
    """Carga el archivo en el entorno (por defecto os.environ) y devuelve el estado. Nunca lanza: un archivo
    ilegible se avisa y la app arranca con lo que tenga el entorno."""
    entorno = os.environ if entorno is None else entorno
    explicito = (entorno.get("CONCILIADOR_ENV") or "").strip()
    ruta = explicito or ruta_por_defecto()
    _ESTADO.update(archivo=None, cargadas=[], respetadas=[], invalidas=[])
    if not os.path.isfile(ruta):
        if explicito:
            print(f"[entorno] CONCILIADOR_ENV apunta a {ruta}, que no existe: se arranca solo con el entorno.",
                  flush=True)
        return _ESTADO
    try:
        valores, invalidas = leer(ruta)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"[entorno] no se pudo leer {ruta}: {type(exc).__name__}. Se arranca solo con el entorno.", flush=True)
        return _ESTADO
    _ESTADO["archivo"] = ruta
    for nombre, valor in valores.items():
        if entorno.get(nombre):
            _ESTADO["respetadas"].append(nombre)
        else:
            entorno[nombre] = valor
            _ESTADO["cargadas"].append(nombre)
    _ESTADO["invalidas"] = invalidas
    msg = f"[entorno] {len(_ESTADO['cargadas'])} variable(s) de {ruta}: {', '.join(_ESTADO['cargadas']) or '-'}"
    if _ESTADO["respetadas"]:
        msg += f" | ya definidas en el entorno (se respetan): {', '.join(_ESTADO['respetadas'])}"
    if invalidas:
        msg += f" | líneas no válidas (se ignoran): {', '.join(map(str, invalidas))}"
    print(msg, flush=True)
    return _ESTADO


def estado() -> dict:
    """Para /api/diagnostico: archivo y NOMBRES de las variables (nunca valores)."""
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _ESTADO.items()}
