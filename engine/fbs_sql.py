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
import ipaddress
import socket
from datetime import date, datetime

from engine import telemetria
from engine.hub_cifrado import descifrar_fbs
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


def simulado() -> bool:
    """Ambiente de demo (DEMO_MODE): no hay SQL Server, el mayor sale de
    engine/simulacion.py — la misma simulación que genera los extractos."""
    return os.environ.get("DEMO_MODE", "").strip().lower() in ("1", "true", "on", "si")

# ---- SOLO LECTURA (garantía dura): la app JAMÁS escribe en el FBS ----------
# Capa 1: la query se valida antes de ejecutarse — una sola sentencia, que
#   empiece en SELECT/WITH y sin ninguna palabra de escritura o ejecución.
#   La validación recorre el texto con un lector que entiende literales,
#   identificadores entre [] o "", y comentarios (-- y /* */ anidados) igual
#   que SQL Server: así un '--' dentro de un literal no puede esconder una
#   segunda sentencia.
# Capa 2: todo corre dentro de una transacción SIN commit y con rollback
#   explícito al final: aunque algo lograra colarse, se deshace.
# Capa 3: el login que usa el conciliador NO puede tener permisos de
#   escritura: antes de la primera consulta de cada conexión se verifica
#   (sysadmin / db_owner / db_datawriter) y, si los tiene, se rechaza.
_PROHIBIDAS = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute"
    r"|grant|revoke|deny|into|backup|restore|shutdown|dbcc|kill|use"
    r"|sp_\w+|xp_\w+|fn_\w+|openrowset|opendatasource|openquery|openxml"
    r"|writetext|updatetext|readtext|bulk|disable|enable"
    r"|declare|set|waitfor|raiserror|throw|print|begin|while|goto|return"
    r"|commit|rollback|save|transaction|tran|cursor|reconfigure|checkpoint"
    r"|go)\b", re.IGNORECASE)


def _sin_literales(query: str) -> str:
    """El texto de la query sin comentarios, literales ni identificadores
    entrecomillados, leído como lo lee SQL Server. Lanza ValueError si queda
    algo sin cerrar (no se adivina)."""
    out, i, n = [], 0, len(query)
    while i < n:
        c = query[i]
        if c == "-" and query.startswith("--", i):
            j = query.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif c == "/" and query.startswith("/*", i):
            prof, i = 1, i + 2
            while i < n and prof:
                if query.startswith("/*", i):
                    prof, i = prof + 1, i + 2
                elif query.startswith("*/", i):
                    prof, i = prof - 1, i + 2
                else:
                    i += 1
            if prof:
                raise ValueError("Protección de solo lectura: comentario /* sin cerrar.")
            out.append(" ")
        elif c in ("'", '"', "["):
            cierre = "]" if c == "[" else c
            i += 1
            while True:
                if i >= n:
                    raise ValueError("Protección de solo lectura: hay un texto o "
                                     f"identificador sin cerrar ({c}).")
                if query[i] == cierre:
                    if i + 1 < n and query[i + 1] == cierre:   # '' ]] "" escapados
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''" if c == "'" else "x")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _validar_solo_lectura(query: str):
    """Lanza ValueError si la query no es una consulta de solo lectura."""
    cuerpo = _sin_literales(query or "").strip().rstrip(";").strip()
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


_BLOQUE_RE = re.compile(
    r"'([EO])'\s+AS\s+hoja\s*,\s*'(\d+)'\s+AS\s+codigo", re.IGNORECASE)


def _validar_bloques_unicos(query: str):
    """Un accidente de pegado que repita el bloque de una cuenta duplica
    TODOS sus asientos (UNION ALL no filtra). Se rechaza con nombre y
    apellido antes de ejecutar."""
    pares = [(h.upper(), c) for h, c in _BLOQUE_RE.findall(query or "")]
    repetidos = sorted({p for p in pares if pares.count(p) > 1})
    if repetidos:
        det = ", ".join(f"({h}) ({c})" for h, c in repetidos)
        raise ValueError(
            f"La query trae MÁS DE UNA VEZ el bloque de la cuenta {det}: "
            "eso duplica todos sus asientos. Dejá un solo SELECT por cuenta "
            "y hoja (parece un pegado repetido).")


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


# ---- destino de la conexión: a qué servidor puede conectarse la app ---------
# La conexión manual se puede editar desde la UI/API; sin controles, quien
# lograra editarla podría apuntar el conciliador a un servidor propio y
# recibir la clave guardada. Reglas:
#  * el destino solo se puede cambiar desde la propia máquina o con
#    FBS_SQL_EDITABLE=1 (decisión de despliegue), nunca anónimamente;
#  * si FBS_SQL_SERVIDOR está en el entorno, el destino queda fijo;
#  * cambiar de servidor/puerto/base/usuario sin mandar clave nueva borra la
#    clave guardada (no se reenvía a un destino distinto del que se guardó);
#  * el host debe tener forma de nombre/IP y resolver a una dirección
#    privada (RFC 1918 o 100.64/10); cualquier otra (pública, loopback)
#    exige estar en FBS_SQL_HOSTS_PERMITIDOS (lista separada por comas de
#    nombres, IPs o CIDR). Link-local (169.254.x: metadatos de la nube),
#    multicast y 0.0.0.0 nunca se aceptan.
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
_INSTANCIA_RE = re.compile(r"^[A-Za-z0-9_$#-]{1,64}$")
_PUERTOS_PROHIBIDOS = {22, 23, 25, 53, 80, 110, 143, 443, 445, 3389}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def destino_bloqueado() -> bool:
    """True si el destino viene fijado por el entorno (no editable)."""
    return bool(os.environ.get("FBS_SQL_SERVIDOR", "").strip())


def _permitidos() -> list:
    reglas = []
    for item in os.environ.get("FBS_SQL_HOSTS_PERMITIDOS", "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        try:
            reglas.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            reglas.append(item)
    return reglas


def _en_lista(host: str, ips: list) -> bool:
    for r in _permitidos():
        if isinstance(r, str):
            if r == host.lower():
                return True
        elif any(ip in r for ip in ips):
            return True
    return False


def validar_destino(servidor: str, puerto) -> None:
    """Lanza ValueError si el destino no es aceptable (ver arriba)."""
    servidor = (servidor or "").strip()
    host, _, instancia = servidor.partition("\\")
    if not servidor or not _HOST_RE.match(host) or (
            instancia and not _INSTANCIA_RE.match(instancia)):
        raise ValueError("El servidor del FBS no es válido: usá un nombre de host "
                         "o una IP (con \\instancia si corresponde).")
    try:
        puerto = int(puerto or 1433)
    except (TypeError, ValueError):
        raise ValueError("El puerto del FBS no es un número.")
    if not 1 <= puerto <= 65535 or puerto in _PUERTOS_PROHIBIDOS:
        raise ValueError(f"El puerto {puerto} no es aceptable para una conexión SQL Server.")
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = sorted({ipaddress.ip_address(i[4][0])
                          for i in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
        except OSError:
            raise ValueError(f'El servidor "{host}" del FBS no resuelve.')
    for ip in ips:
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError(f"El servidor del FBS apunta a una dirección no permitida ({ip}).")
    if _en_lista(host, ips):
        return
    for ip in ips:
        if ip.is_loopback or not (ip.is_private or ip in _CGNAT):
            raise ValueError(
                f"El servidor del FBS ({host} → {ip}) no es una dirección privada. "
                "Si es correcto, agregalo a FBS_SQL_HOSTS_PERMITIDOS en el servidor.")


def guardar_conf(datos: dict, permitir_destino: bool = True):
    """Guarda la conexión manual. `permitir_destino=False` (quien llama no es
    local ni tiene FBS_SQL_EDITABLE) deja pasar solo la query."""
    if str(datos.get("query") or "").strip():
        _validar_solo_lectura(str(datos["query"]))
        _validar_bloques_unicos(str(datos["query"]))
    conf = cargar_conf()
    previo = (conf["servidor"], conf["puerto"], conf["base"], conf["usuario"])
    if "query" in datos:
        conf["query"] = str(datos["query"] or "").strip()
    nuevo = (str(datos.get("servidor", conf["servidor"]) or "").strip(),
             int(datos.get("puerto") or conf["puerto"] or 1433),
             str(datos.get("base", conf["base"]) or "").strip(),
             str(datos.get("usuario", conf["usuario"]) or "").strip())
    cambios_destino = nuevo != previo or bool(datos.get("clave"))
    if cambios_destino:
        if destino_bloqueado():
            raise PermissionError(
                "El destino del FBS está fijado por el servidor (FBS_SQL_SERVIDOR) "
                "y no se puede cambiar desde la interfaz.")
        if not permitir_destino:
            raise PermissionError(
                "Cambiar el servidor del FBS no está habilitado desde acá: hacelo "
                "en el propio servidor o activá FBS_SQL_EDITABLE=1.")
        conf["servidor"], conf["puerto"], conf["base"], conf["usuario"] = nuevo
        validar_destino(conf["servidor"], conf["puerto"])
        if nuevo != previo:
            conf["clave"] = ""          # no reenviar la clave vieja a otro destino
        if datos.get("clave"):          # write-only: vacío = conservar la actual
            conf["clave"] = str(datos["clave"])
    os.makedirs(_DATOS, exist_ok=True)
    ruta_tmp = RUTA_CONF + ".tmp"
    with open(ruta_tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=1)
    try:
        os.chmod(ruta_tmp, 0o600)       # la clave no debe ser legible por otros
    except OSError:
        pass
    os.replace(ruta_tmp, RUTA_CONF)
    return conf


def configurado() -> bool:
    c = cargar_conf()
    return bool(c["servidor"] and c["base"] and c["usuario"])


def publica(marca: str = "") -> dict:
    """Config sin secretos, para la UI. Con marca, las cuentas del hub se
    limitan a esa marca (el modo sigue siendo hub si hay CUALQUIER cuenta
    configurada, aunque la marca pedida no tenga ninguna)."""
    c = cargar_conf()
    todas = cuentas_fbs()
    cfgs = _filtrar_cfgs(todas, marca)
    return {"configurado": configurado() or bool(cfgs),
            "modo": "hub" if todas else "manual",
            "cuentas_hub": len(cfgs),
            "cuentas": [{"cuenta_id": x["cuenta_id"], "etiqueta": x["etiqueta"]}
                        for x in sorted(cfgs, key=lambda x: x["etiqueta"])],
            "conexiones_hub": sorted({x["conexion_nombre"] for x in cfgs}),
            "servidor": c["servidor"],
            "puerto": c["puerto"], "base": c["base"], "usuario": c["usuario"],
            "clave_presente": bool(c["clave"]), "query": c["query"]}


def _ipv4(host: str) -> str:
    """Resuelve el nombre a IPv4: FreeTDS falla cuando el nombre de la
    máquina resuelve a IPv6 (típico en redes Windows). Si no resuelve,
    se deja el nombre tal cual."""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def _conectar(conf: dict):
    import pymssql   # import perezoso: la app arranca aunque falte el driver
    kwargs = dict(database=conf["base"], user=conf["usuario"],
                  password=descifrar_fbs(conf["clave"]), login_timeout=10, timeout=60,
                  charset="UTF-8")
    if os.environ.get("FBS_SQL_TDS"):        # p. ej. 7.0 / 7.2 / 7.4
        kwargs["tds_version"] = os.environ["FBS_SQL_TDS"]
    servidor = (conf["servidor"] or "").strip()
    if "\\" in servidor:
        # instancia nombrada (HOST\SQLEXPRESS): el puerto lo resuelve el
        # SQL Browser del servidor (UDP 1434) — no se pasa puerto fijo
        host, instancia = servidor.split("\\", 1)
        kwargs["server"] = f"{_ipv4(host)}\\{instancia}"
    else:
        kwargs["server"] = _ipv4(servidor)
        kwargs["port"] = conf["puerto"]
    return pymssql.connect(**kwargs)


def _consultar_pymssql(conf: dict, query: str, params: dict) -> list[dict]:
    con = _conectar(conf)
    try:
        con.autocommit(False)   # transacción abierta, jamás se commitea
        cur = con.cursor(as_dict=True)
        cur.execute(query, params) if params else cur.execute(query)
        filas = cur.fetchall()
    finally:
        try:
            con.rollback()      # solo lectura: se deshace cualquier efecto
        finally:
            con.close()
    return filas


def _consultar_pyodbc(conf: dict, query: str, params: dict) -> list[dict]:
    """Vía el driver ODBC nativo de Windows (el mismo que usa SSMS): maneja
    cifrado obligatorio e instancias nombradas mucho mejor que FreeTDS."""
    import pyodbc
    candidatos = [d for d in pyodbc.drivers() if "SQL Server" in d]
    if not candidatos:
        raise ImportError("sin driver ODBC de SQL Server instalado")
    orden = ("ODBC Driver 18", "ODBC Driver 17", "ODBC Driver 13",
             "SQL Server Native Client", "SQL Server")
    driver = min(candidatos,
                 key=lambda d: next((i for i, p in enumerate(orden) if d.startswith(p)), 99))
    servidor = (conf["servidor"] or "").strip()
    srv = servidor if "\\" in servidor else f"{servidor},{conf['puerto']}"
    cadena = (f"DRIVER={{{driver}}};SERVER={srv};DATABASE={conf['base']};"
              f"UID={conf['usuario']};PWD={descifrar_fbs(conf['clave'])};")
    cadena += "ApplicationIntent=ReadOnly;"
    if driver.startswith("ODBC Driver"):
        # el SQL Server del FBS usa certificado autofirmado
        cadena += "Encrypt=yes;TrustServerCertificate=yes;"
    # pyodbc usa parámetros posicionales '?': traducir %(desde)s / %(hasta)s
    nombres = []
    q = re.sub(r"%\((desde|hasta)\)s",
               lambda m: (nombres.append(m.group(1)), "?")[1], query)
    con = pyodbc.connect(cadena, timeout=10, autocommit=False)
    try:
        cur = con.cursor()
        cur.execute(q, *[params[n] for n in nombres]) if nombres else cur.execute(q)
        cols = [c[0] for c in cur.description]
        filas = [dict(zip(cols, f)) for f in cur.fetchall()]
    finally:
        try:
            con.rollback()      # solo lectura
        finally:
            con.close()
    return filas


_DRIVER_USADO = {"nombre": None}


def _query_para_log(query: str, params: dict) -> str:
    """La query con los parámetros sustituidos, lista para pegar en SSMS."""
    q = query
    for k, v in (params or {}).items():
        q = q.replace(f"%({k})s", f"'{v}'")
    return q


_LOGINS_VERIFICADOS: set = set()
_SQL_PRIVILEGIOS = (
    "SELECT ISNULL(IS_SRVROLEMEMBER('sysadmin'), 0) AS sysadmin, "
    "ISNULL(IS_MEMBER('db_owner'), 0) AS db_owner, "
    "ISNULL(IS_MEMBER('db_datawriter'), 0) AS db_datawriter, "
    "ISNULL(IS_MEMBER('db_ddladmin'), 0) AS db_ddladmin")


def permite_login_con_escritura() -> bool:
    return os.environ.get("FBS_SQL_PERMITIR_LOGIN_ESCRITURA", "").strip().lower() in (
        "1", "true", "on", "si")


def _verificar_login_solo_lectura(conf: dict):
    """Capa 3 de la garantía de solo lectura: el login con el que se conecta
    el conciliador no puede ser administrador ni poder escribir. Se chequea
    una vez por (servidor, base, usuario) y se rechaza si tiene sysadmin,
    db_owner, db_datawriter o db_ddladmin. FBS_SQL_PERMITIR_LOGIN_ESCRITURA=1
    lo deja pasar (excepción consciente, mientras se crea un login de solo
    lectura)."""
    if simulado() or permite_login_con_escritura():
        return
    clave = (conf.get("servidor"), conf.get("puerto"), conf.get("base"),
             conf.get("usuario"))
    if clave in _LOGINS_VERIFICADOS:
        return
    filas = _consultar_driver(conf, _SQL_PRIVILEGIOS, {})
    fila = {str(k).lower(): v for k, v in (filas[0] if filas else {}).items()}
    con_permiso = [k for k in ("sysadmin", "db_owner", "db_datawriter", "db_ddladmin")
                   if fila.get(k) == 1]
    if con_permiso or not fila:
        raise ValueError(
            "Protección de solo lectura: el usuario del FBS tiene permisos de "
            f"escritura o administración ({', '.join(con_permiso) or 'no se pudo verificar'}). "
            "Creá un login con solo db_datareader y usalo acá.")
    _LOGINS_VERIFICADOS.add(clave)


def _consultar(conf: dict, query: str, params: dict) -> list[dict]:
    """_consultar_driver medido para el monitoreo: cada consulta al FBS queda
    como un tramo (servidor, base, driver, filas, demora y el error si falla)."""
    validar_destino(conf.get("servidor"), conf.get("puerto"))
    _verificar_login_solo_lectura(conf)
    with telemetria.tramo("FBS consulta", tipo="cliente", **{
            "db.system.name": "mssql",
            "server.address": str(conf.get("servidor") or ""),
            "db.namespace": str(conf.get("base") or ""),
            "fbs.conexion": conf.get("nombre")}) as tramo:
        filas = _consultar_driver(conf, query, params)
        tramo.set_attribute("fbs.filas", len(filas))
        tramo.set_attribute("fbs.driver", _DRIVER_USADO.get("nombre") or "")
        return filas


def _consultar_driver(conf: dict, query: str, params: dict) -> list[dict]:
    """Ejecuta la consulta con el mejor driver disponible: ODBC nativo de
    Windows primero, FreeTDS (pymssql) como respaldo. FBS_SQL_DRIVER
    (pyodbc|pymssql) fuerza uno solo."""
    print("[fbs] Query a ejecutar (copiable a SSMS):\n"
          "-------------------------------------------\n"
          + _query_para_log(query, params)
          + "\n-------------------------------------------", flush=True)
    elegido = (os.environ.get("FBS_SQL_DRIVER") or "auto").lower()
    errores = []
    if elegido in ("auto", "pyodbc"):
        try:
            filas = _consultar_pyodbc(conf, query, params)
            _DRIVER_USADO["nombre"] = "pyodbc"
            return filas
        except ImportError as exc:
            if elegido == "pyodbc":
                raise ValueError(f"pyodbc no disponible: {exc}")
            errores.append(f"[odbc] {exc}")
        except Exception as exc:  # noqa: BLE001
            if elegido == "pyodbc":
                raise
            errores.append(f"[odbc] {exc}")
    if elegido in ("auto", "pymssql"):
        try:
            filas = _consultar_pymssql(conf, query, params)
            _DRIVER_USADO["nombre"] = "pymssql"
            return filas
        except Exception as exc:  # noqa: BLE001
            errores.append(f"[pymssql] {exc}")
    raise RuntimeError(" — ".join(str(e) for e in errores))


def _log_filas(filas: list[dict]):
    print(f"[fbs] La consulta devolvió {len(filas)} fila(s) "
          f"(driver {_DRIVER_USADO['nombre']})", flush=True)


def probar() -> dict:
    """Prueba la conexión (SELECT 1). En modo hub prueba TODAS las
    conexiones configuradas; en modo manual, la del modal."""
    cfgs = cuentas_fbs()
    if cfgs:
        vistas, detalle = set(), []
        for c in cfgs:
            if c["conexion_id"] in vistas:
                continue
            vistas.add(c["conexion_id"])
            if simulado():
                detalle.append({"nombre": c["conexion_nombre"], "ok": True,
                                "driver": "simulado (demo)"})
                continue
            try:
                _consultar(c["conexion"], "SELECT 1 AS uno", {})
                detalle.append({"nombre": c["conexion_nombre"], "ok": True,
                                "driver": _DRIVER_USADO["nombre"]})
            except Exception as exc:  # noqa: BLE001
                detalle.append({"nombre": c["conexion_nombre"], "ok": False,
                                "error": str(exc)})
        return {"ok": all(d["ok"] for d in detalle), "modo": "hub",
                "conexiones": detalle,
                "driver": "simulado (demo)" if simulado() else _DRIVER_USADO["nombre"],
                "error": " | ".join(f'{d["nombre"]}: {d["error"]}'
                                    for d in detalle if not d["ok"]) or None}
    conf = cargar_conf()
    if not configurado():
        return {"ok": False, "error": "Faltan servidor, base o usuario en la configuración"}
    try:
        _consultar(conf, "SELECT 1 AS uno", {})
        return {"ok": True, "modo": "manual", "driver": _DRIVER_USADO["nombre"]}
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


def traer(desde: str, hasta: str, marca: str = "",
          solo_cuentas: list[str] | None = None) -> list[dict]:
    """Corre la query del FBS para el rango [desde, hasta] (ISO) y devuelve
    una lista de 'archivos virtuales' con la misma forma que devuelve
    parsers.diarios.identificar() para un reporte FBS: uno por cuenta
    contable y hoja. Lanza ValueError con mensaje claro si algo falla.

    Modo hub (preferido): si en el hub hay cuentas con conexión FBS e IDs de
    plan E/O, la query se genera sola sobre DetMov, una consulta por
    conexión. Si no hay nada configurado en el hub, se usa la query manual
    del modal (modo avanzado)."""
    todas = cuentas_fbs()
    if todas:
        cfgs = _filtrar_cfgs(todas, marca)
        if solo_cuentas:
            elegidas = set(solo_cuentas)
            cfgs = [c for c in cfgs if c["cuenta_id"] in elegidas]
        if not cfgs:
            raise ValueError(
                "Ninguna de las cuentas elegidas tiene FBS configurado en el "
                "hub" + (f" para la marca {marca}" if marca else "") + ".")
        return _traer_hub(cfgs, desde, hasta)
    conf = cargar_conf()
    if not configurado():
        raise ValueError(
            "No hay conexión al FBS: configurala en el hub (pestaña "
            "Conexiones FBS + IDs E/O en cada cuenta) o cargá la query "
            "manual en este modal.")
    _validar_solo_lectura(conf["query"])
    _validar_bloques_unicos(conf["query"])
    try:
        filas = _consultar(conf, conf["query"], {"desde": desde, "hasta": hasta})
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"La consulta al FBS falló: {exc}")
    _log_filas(filas)
    return _infos_desde_filas(filas)


def _infos_desde_filas(filas: list[dict],
                       cuenta_por_codigo: dict | None = None) -> list[dict]:
    """Agrupa las filas crudas por (codigo, hoja) y arma los 'archivos
    virtuales'. Si se conoce el vínculo codigo -> cuenta bancaria (config
    del hub), viaja en cuenta_id_config y no hace falta asignar a mano."""
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
            "cuenta_id_config": (cuenta_por_codigo or {}).get(codigo),
        })
    return salida


# ---- modo hub: la config (conexiones + IDs E/O por cuenta) vive en el
# Postgres compartido y la query se genera sola sobre DetMov -----------------

def cuentas_fbs() -> list[dict]:
    """Cuentas bancarias del hub que tienen conexión FBS + IDs de plan.
    Devuelve [] si el hub todavía no tiene nada configurado (o no hay DB)."""
    from engine import cuentas as cuentas_mod
    url = cuentas_mod._database_url()
    if not url:
        return []
    try:
        import psycopg2
        con = psycopg2.connect(url, connect_timeout=5)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT c.id, c.banco, c.numero, c.moneda, c.fbs_plan_e, c.fbs_plan_o, "
                "       f.id, f.nombre, f.servidor, f.puerto, f.base, f.usuario, f.clave, "
                "       COALESCE(m.nombre, '') "
                "  FROM cuentas_bancarias c "
                "  JOIN fbs_conexiones f ON f.id = c.fbs_conexion_id "
                "  LEFT JOIN marcas m ON m.id = c.marca_id "
                " WHERE c.activa = true "
                "   AND (COALESCE(c.fbs_plan_e, '') <> '' OR COALESCE(c.fbs_plan_o, '') <> '') "
                " ORDER BY c.id")
            filas = cur.fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — sin hub configurado no es error
        print(f"[fbs] No pude leer la config FBS del hub ({exc})")
        _HUB_ESTADO["error"] = str(exc)
        return []
    _HUB_ESTADO["error"] = None
    salida = []
    for db_id, banco, numero, moneda, pe, po, fid, fnom, srv, prt, base, usu, clave, marca in filas:
        salida.append({
            "cuenta_id": (f"{cuentas_mod._banco_key(banco)}-"
                          f"{cuentas_mod._digits(numero)}-{(moneda or '').lower()}"),
            "db_id": db_id, "banco": cuentas_mod._banco_key(banco), "numero": numero,
            "moneda": (moneda or "").upper(),
            "empresa": marca,
            "etiqueta": (f"{marca} — " if marca else "") + f"{banco} {numero} ({moneda})",
            "plan_e": str(pe or "").strip(), "plan_o": str(po or "").strip(),
            "conexion_id": fid, "conexion_nombre": fnom,
            "conexion": {"servidor": srv or "", "puerto": prt or 1433,
                         "base": base or "", "usuario": usu or "",
                         "clave": clave or "", "query": ""},
        })
    return salida


_HUB_ESTADO = {"error": None}


def diagnostico() -> dict:
    """Estado del modo hub, para GET /api/diagnostico."""
    from engine import cuentas as cuentas_mod
    cfgs = cuentas_fbs()
    return {
        "database_url_presente": bool(cuentas_mod._database_url()),
        "cuentas_configuradas": len(cfgs),
        "cuentas": [x["etiqueta"] for x in cfgs],
        "conexiones": sorted({x["conexion_nombre"] for x in cfgs}),
        "error_lectura_hub": _HUB_ESTADO["error"],
        "modo": "hub" if cfgs else "manual",
        "simulado": simulado(),
    }


def _filtrar_cfgs(cfgs: list[dict], marca: str) -> list[dict]:
    """Cuentas FBS de una marca (mismo criterio flexible que el resto)."""
    if not marca:
        return cfgs
    from engine import cuentas as cuentas_mod
    m = cuentas_mod.slug(marca)
    return [c for c in cfgs if m in cuentas_mod.slug(c.get("empresa") or "")]


def _query_detmov(planes: list[tuple[str, int]]) -> str:
    """Query generada sobre DetMov: un bloque por (hoja, id de plan)."""
    bloques = [
        (f"SELECT '{hoja}' AS hoja, '{plan}' AS codigo, "
         "DetMovNro, DetFecha, DetRef, DetComenta, DetDebe, DetHaber "
         f"FROM DetMov WHERE DetPlan = {plan} "
         "AND DetFecha BETWEEN %(desde)s AND %(hasta)s")
        for hoja, plan in planes]
    return "\nUNION ALL\n".join(bloques)


def _traer_hub(cfgs: list[dict], desde: str, hasta: str) -> list[dict]:
    """Una consulta generada por conexión FBS, con todos sus pares E/O."""
    por_conexion: dict = {}
    for c in cfgs:
        g = por_conexion.setdefault(c["conexion_id"], {
            "conf": c["conexion"], "nombre": c["conexion_nombre"],
            "planes": [], "cuenta_por_codigo": {}})
        for hoja, plan in (("E", c["plan_e"]), ("O", c["plan_o"])):
            if not plan:
                continue
            try:
                plan_n = int(plan)
            except ValueError:
                raise ValueError(
                    f'El ID de la cuenta {hoja} de {c["etiqueta"]} configurado '
                    f'en el hub no es numérico: "{plan}".')
            if (hoja, plan_n) in g["planes"]:
                # dos cuentas del hub con el mismo ID de plan: un solo bloque,
                # si no cada asiento vendría duplicado
                print(f"[fbs] Aviso: el plan {plan_n} ({hoja}) está configurado "
                      f"en más de una cuenta del hub; se consulta una sola vez",
                      flush=True)
                continue
            g["planes"].append((hoja, plan_n))
            g["cuenta_por_codigo"][str(plan_n)] = c["cuenta_id"]

    # yyyymmdd: el único formato de fecha que SQL Server interpreta igual
    # en cualquier idioma/configuración regional
    params = {"desde": desde.replace("-", ""), "hasta": hasta.replace("-", "")}
    salida, errores = [], []
    if simulado():
        # demo: no hay SQL Server; las filas de DetMov salen de la simulación,
        # la misma que arma los extractos (engine/simulacion.py)
        from engine import simulacion
        d1, d2 = date.fromisoformat(desde), date.fromisoformat(hasta)
        cuentas = simulacion.con_roles(cfgs, cuentas_fbs())
        for conexion_id, g in por_conexion.items():
            filas = [f for c in cuentas if c["conexion_id"] == conexion_id
                     for f in simulacion.filas_detmov(c, d1, d2)]
            print(f"[fbs] (demo) {g['nombre']}: {len(filas)} fila(s) simuladas", flush=True)
            salida.extend(_infos_desde_filas(filas, g["cuenta_por_codigo"]))
        return salida
    for g in por_conexion.values():
        if not g["planes"]:
            continue
        query = _query_detmov(g["planes"])
        _validar_solo_lectura(query)   # defensa en profundidad
        try:
            filas = _consultar(g["conf"], query, params)
        except Exception as exc:  # noqa: BLE001
            errores.append(f'{g["nombre"]}: {exc}')
            continue
        _log_filas(filas)
        salida.extend(_infos_desde_filas(filas, g["cuenta_por_codigo"]))
    if errores and not salida:
        raise ValueError("La consulta al FBS falló: " + " | ".join(errores))
    if errores:
        print(f"[fbs] Conexiones con error (parcial): {errores}")
    return salida
