# -*- coding: utf-8 -*-
"""Conexión directa al SQL Server del FBS: en lugar de subir los reportes
"Consulta del Mayor" exportados, se corre una query por rango de fechas y se
traen los asientos de las cuentas O y E directo de la base.

Configuración en datos/fbs_sql.json (editable desde la UI) con override por
variables de entorno FBS_SQL_SERVIDOR / FBS_SQL_PUERTO / FBS_SQL_BASE /
FBS_SQL_USUARIO / FBS_SQL_CLAVE. La clave nunca se devuelve por API.

La query es libre (la escribe quien conoce el esquema del FBS) pero DEBE
devolver estas columnas, con estos alias:
    hoja        'E' u 'O' (a qué cuenta del par pertenece el asiento)
    codigo      código de la cuenta contable FBS (p. ej. 1141121) — con esto
                se mapea a la cuenta bancaria, igual que con los archivos
    asiento     número de asiento
    fecha       fecha del asiento (date/datetime)
    referencia  referencia (p. ej. 'PO 00810')
    comentario  comentario/leyenda
    debe        importe al debe (0 si no)
    haber       importe al haber (0 si no)
    nombre_fbs  (opcional) nombre interno de la cuenta en el FBS
y aceptar los parámetros %(desde)s y %(hasta)s (fechas ISO) en el WHERE.

El resultado se agrupa por (codigo, hoja) y se inyecta al flujo diario con la
MISMA forma que un archivo FBS identificado: mapeos aprendidos, memoria de
conciliados, arrastre y marca funcionan idénticos.
"""
import json
import os
import re
from datetime import date, datetime

from parsers.mayor_xlsx import AsientoMayor

_DATOS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "datos")
RUTA_CONF = os.path.join(_DATOS, "fbs_sql.json")

QUERY_EJEMPLO = (
    "-- Un bloque por cuenta del mayor: las dos primeras columnas dicen si es\n"
    "-- la cuenta O o la E y su número interno (el mismo del WHERE). El resto\n"
    "-- son las columnas del FBS tal cual (DetMovNro, DetFecha, DetRef,\n"
    "-- DetComenta, DetDebe, DetHaber).\n"
    "SELECT 'O' AS hoja, '109' AS codigo,\n"
    "       DetMovNro, DetFecha, DetRef, DetComenta, DetDebe, DetHaber\n"
    "  FROM ...\n"
    " WHERE ... = 109 AND DetFecha BETWEEN %(desde)s AND %(hasta)s\n"
    "UNION ALL\n"
    "SELECT 'E' AS hoja, '110' AS codigo,\n"
    "       DetMovNro, DetFecha, DetRef, DetComenta, DetDebe, DetHaber\n"
    "  FROM ...\n"
    " WHERE ... = 110 AND DetFecha BETWEEN %(desde)s AND %(hasta)s"
)

COLUMNAS = ("hoja", "codigo", "asiento", "fecha", "referencia",
            "comentario", "debe", "haber")

# ---- SOLO LECTURA (garantía dura): la app JAMÁS escribe en el FBS ----------
# Capa 1: la query se valida antes de ejecutarse — una sola sentencia, que
#   empiece en SELECT/WITH y sin ninguna palabra de escritura o ejecución.
# Capa 2: todo corre dentro de una transacción SIN commit y con rollback
#   explícito al final: aunque algo lograra colarse, se deshace.
# Capa 3 (del lado del servidor, recomendada): el login que usa el
#   conciliador debe ser db_datareader + db_denydatawriter.
_PROHIBIDAS = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute"
    r"|grant|revoke|deny|into|backup|restore|shutdown|dbcc|kill|use"
    r"|sp_\w+|xp_\w+|openrowset|opendatasource|openquery"
    r"|writetext|updatetext|bulk|disable|enable)\b", re.IGNORECASE)


def _validar_solo_lectura(query: str):
    """Lanza ValueError si la query no es una consulta de solo lectura."""
    limpio = re.sub(r"--[^\n]*", " ", query or "")
    limpio = re.sub(r"/\*.*?\*/", " ", limpio, flags=re.S)
    limpio = re.sub(r"'(?:[^']|'')*'", "''", limpio)   # literales fuera
    cuerpo = limpio.strip().rstrip(";").strip()
    if not cuerpo:
        raise ValueError("La query del FBS está vacía.")
    if ";" in cuerpo:
        raise ValueError(
            "Protección de solo lectura: la query del FBS debe ser UNA sola "
            "sentencia (hay un ';' en el medio).")
    primera = cuerpo.split(None, 1)[0].lower()
    if primera not in ("select", "with"):
        raise ValueError(
            "Protección de solo lectura: la query del FBS tiene que empezar "
            f"con SELECT (o WITH), no con {primera.upper()}.")
    m = _PROHIBIDAS.search(cuerpo)
    if m:
        raise ValueError(
            "Protección de solo lectura: la query del FBS contiene "
            f'"{m.group(0).upper()}", que no está permitido. Solo consultas.')


# nombres reales de las columnas del FBS -> nombre canónico (se comparan en
# minúsculas; así la query no necesita renombrar nada salvo hoja y codigo)
SINONIMOS = {"detmovnro": "asiento", "detfecha": "fecha", "detref": "referencia",
             "detcomenta": "comentario", "detdebe": "debe", "dethaber": "haber"}


def _normalizar_fila(fila: dict) -> dict:
    out = {}
    for k, v in fila.items():
        kk = str(k).strip().lower()
        out.setdefault(SINONIMOS.get(kk, kk), v)
    return out


def _importe(v) -> float:
    """Importe desde numeric/Decimal/float o texto con coma decimal."""
    if v is None:
        return 0.0
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0.0
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        return float(s)
    return float(v)


def cargar_conf() -> dict:
    conf = {"servidor": "", "puerto": 1433, "base": "", "usuario": "",
            "clave": "", "query": QUERY_EJEMPLO}
    if os.path.exists(RUTA_CONF):
        try:
            with open(RUTA_CONF, encoding="utf-8") as f:
                conf.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    # el entorno pisa al archivo (deploys sin tocar datos/)
    conf["servidor"] = os.environ.get("FBS_SQL_SERVIDOR", conf["servidor"])
    conf["puerto"] = int(os.environ.get("FBS_SQL_PUERTO", conf["puerto"] or 1433))
    conf["base"] = os.environ.get("FBS_SQL_BASE", conf["base"])
    conf["usuario"] = os.environ.get("FBS_SQL_USUARIO", conf["usuario"])
    conf["clave"] = os.environ.get("FBS_SQL_CLAVE", conf["clave"])
    return conf


def guardar_conf(datos: dict):
    if str(datos.get("query") or "").strip():
        _validar_solo_lectura(str(datos["query"]))
    conf = cargar_conf()
    for k in ("servidor", "base", "usuario", "query"):
        if k in datos:
            conf[k] = str(datos[k] or "").strip()
    if "puerto" in datos:
        conf["puerto"] = int(datos["puerto"] or 1433)
    if datos.get("clave"):          # write-only: vacío = conservar la actual
        conf["clave"] = str(datos["clave"])
    os.makedirs(_DATOS, exist_ok=True)
    with open(RUTA_CONF, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=1)
    return conf


def configurado() -> bool:
    c = cargar_conf()
    return bool(c["servidor"] and c["base"] and c["usuario"])


def publica() -> dict:
    """Config sin secretos, para la UI."""
    c = cargar_conf()
    return {"configurado": configurado(), "servidor": c["servidor"],
            "puerto": c["puerto"], "base": c["base"], "usuario": c["usuario"],
            "clave_presente": bool(c["clave"]), "query": c["query"]}


def _conectar(conf: dict):
    import pymssql   # import perezoso: la app arranca aunque falte el driver
    kwargs = dict(database=conf["base"], user=conf["usuario"],
                  password=conf["clave"], login_timeout=10, timeout=60,
                  charset="UTF-8")
    servidor = (conf["servidor"] or "").strip()
    if "\\" in servidor:
        # instancia nombrada (HOST\SQLEXPRESS): el puerto lo resuelve el
        # SQL Browser del servidor (UDP 1434) — no se pasa puerto fijo
        kwargs["server"] = servidor
    else:
        kwargs["server"] = servidor
        kwargs["port"] = conf["puerto"]
    return pymssql.connect(**kwargs)


def probar() -> dict:
    """Prueba la conexión (SELECT 1). Devuelve {ok} o {ok: False, error}."""
    conf = cargar_conf()
    if not configurado():
        return {"ok": False, "error": "Faltan servidor, base o usuario en la configuración"}
    try:
        con = _conectar(conf)
        try:
            cur = con.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        finally:
            con.close()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 — el error se muestra al usuario
        return {"ok": False, "error": str(exc)}


def _fecha(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if v:
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            pass
    return None


def traer(desde: str, hasta: str) -> list[dict]:
    """Corre la query del FBS para el rango [desde, hasta] (ISO) y devuelve
    una lista de 'archivos virtuales' con la misma forma que devuelve
    parsers.diarios.identificar() para un reporte FBS: uno por cuenta
    contable y hoja. Lanza ValueError con mensaje claro si algo falla."""
    conf = cargar_conf()
    if not configurado():
        raise ValueError("La conexión al FBS no está configurada "
                         "(servidor, base y usuario).")
    _validar_solo_lectura(conf["query"])
    try:
        con = _conectar(conf)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"No pude conectarme al SQL Server del FBS: {exc}")
    try:
        con.autocommit(False)   # transacción abierta, jamás se commitea
        cur = con.cursor(as_dict=True)
        cur.execute(conf["query"], {"desde": desde, "hasta": hasta})
        filas = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        try:
            con.rollback()
        finally:
            con.close()
        raise ValueError(f"La query del FBS falló: {exc}")
    con.rollback()   # solo lectura: se deshace cualquier efecto colateral
    con.close()

    filas = [_normalizar_fila(f) for f in filas]
    if filas:
        faltan = [c for c in COLUMNAS if c not in filas[0]]
        if faltan:
            raise ValueError(
                "A la query del FBS le faltan columnas: " + ", ".join(faltan)
                + ". Las del FBS (DetMovNro, DetFecha, DetRef, DetComenta, "
                "DetDebe, DetHaber) se reconocen solas; hoja y codigo se "
                "agregan como literales: SELECT 'O' AS hoja, '109' AS codigo, …")

    grupos: dict[tuple, dict] = {}
    for f in filas:
        letra = str(f.get("hoja") or "").strip().upper()[:1]
        codigo = str(f.get("codigo") or "").strip()
        if letra not in ("E", "O") or not codigo:
            continue
        g = grupos.setdefault((codigo, letra), {
            "asientos": [], "nombre_fbs": None})
        if f.get("nombre_fbs") and not g["nombre_fbs"]:
            g["nombre_fbs"] = str(f["nombre_fbs"]).strip()[:80]
        n = len(g["asientos"]) + 1
        g["asientos"].append(AsientoMayor(
            id=f"{letra}#{n}", hoja=letra,
            asiento=str(f.get("asiento") or "").strip() or n,
            fecha=_fecha(f.get("fecha")),
            referencia=str(f.get("referencia") or "").strip(),
            comentario=str(f.get("comentario") or "").strip(),
            debe=round(_importe(f.get("debe")), 2),
            haber=round(_importe(f.get("haber")), 2)))

    salida = []
    for (codigo, letra), g in sorted(grupos.items()):
        fechas = [a.fecha for a in g["asientos"] if a.fecha]
        salida.append({
            "tipo": "fbs", "hoja": letra, "codigo_fbs": codigo,
            "codigos": {letra: codigo}, "nombre_fbs": g["nombre_fbs"],
            "asientos": g["asientos"],
            "desde": min(fechas) if fechas else None,
            "hasta": max(fechas) if fechas else None,
            "cantidad": len(g["asientos"]),
            "archivo": f"FBS directo · ({letra}) ({codigo})",
        })
    return salida
