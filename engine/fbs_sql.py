# -*- coding: utf-8 -*-
"""Direct connection to the FBS SQL Server: instead of uploading the exported
"Consulta del Mayor" reports, a query runs for a date range and the entries of
accounts O and E come straight from the database.

Configuration in datos/fbs_sql.json (editable from the UI) with an override
through the environment variables FBS_SQL_SERVIDOR / FBS_SQL_PUERTO /
FBS_SQL_BASE / FBS_SQL_USUARIO / FBS_SQL_CLAVE. The password is never returned
by the API.

The query is free (written by whoever knows the FBS schema) but it MUST return
these columns, with these aliases:
    hoja        'E' or 'O' (which account of the pair the entry belongs to)
    codigo      FBS ledger account code (e.g. 1141121) — maps it to the bank
                account, same as with the files
    asiento     entry number
    fecha       entry date (date/datetime)
    referencia  reference (e.g. 'PO 00810')
    comentario  comment/legend
    debe        debit amount (0 otherwise)
    haber       credit amount (0 otherwise)
    nombre_fbs  (optional) internal name of the account in FBS
and accept the parameters %(desde)s and %(hasta)s (ISO dates) in the WHERE.

The result is grouped by (codigo, hoja) and fed into the daily flow with the
SAME shape as an identified FBS file: learned mappings, matched memory,
carry-over and brand work the same.
"""
import json
import os
import re
import ipaddress
import socket
from datetime import date, datetime

from engine import telemetria
from engine.hub_cifrado import descifrar_fbs
from parsers.ledger_xlsx import LedgerEntry

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "datos")
CONFIG_PATH = os.path.join(_DATA, "fbs_sql.json")

SAMPLE_QUERY = (
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

COLUMNS = ("hoja", "codigo", "asiento", "fecha", "referencia",
           "comentario", "debe", "haber")


def simulated() -> bool:
    """Demo environment (DEMO_MODE): there is no SQL Server, the ledger comes from
    engine/simulacion.py — the same simulation that generates the statements."""
    return os.environ.get("DEMO_MODE", "").strip().lower() in ("1", "true", "on", "si")

# ---- READ-ONLY (hard guarantee): the app NEVER writes to FBS -----------------
# Layer 1: the query is validated before it runs — a single statement that
#   starts with SELECT/WITH and has no write or execution keyword. The check
#   reads the text with a reader that understands literals, identifiers in []
#   or "", and comments (-- and nested /* */) the way SQL Server does: so a
#   '--' inside a literal can't hide a second statement.
# Layer 2: everything runs inside a transaction WITHOUT commit and with an
#   explicit rollback at the end: even if something slipped through, it's undone.
# Layer 3: the login the reconciler uses can NOT have write permissions: before
#   the first query of each connection it is checked (sysadmin / db_owner /
#   db_datawriter) and rejected if it has them.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute"
    r"|grant|revoke|deny|into|backup|restore|shutdown|dbcc|kill|use"
    r"|sp_\w+|xp_\w+|fn_\w+|openrowset|opendatasource|openquery|openxml"
    r"|writetext|updatetext|readtext|bulk|disable|enable"
    r"|declare|set|waitfor|raiserror|throw|print|begin|while|goto|return"
    r"|commit|rollback|save|transaction|tran|cursor|reconfigure|checkpoint"
    r"|go"
    # lock hints: inside the transaction they lock the table for FBS until the
    # rollback
    r"|tablockx|tablock|xlock|updlock|holdlock|paglock|serializable|repeatableread)\b", re.IGNORECASE)


def _without_literals(query: str) -> str:
    """The query text without comments, literals or quoted identifiers, read the
    way SQL Server reads it. Raises ValueError if something is left unclosed
    (no guessing)."""
    out, i, n = [], 0, len(query)
    while i < n:
        c = query[i]
        if c == "-" and query.startswith("--", i):
            j = query.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif c == "/" and query.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if query.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif query.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise ValueError("Protección de solo lectura: comentario /* sin cerrar.")
            out.append(" ")
        elif c in ("'", '"', "["):
            closer = "]" if c == "[" else c
            i += 1
            while True:
                if i >= n:
                    raise ValueError("Protección de solo lectura: hay un texto o "
                                     f"identificador sin cerrar ({c}).")
                if query[i] == closer:
                    if i + 1 < n and query[i + 1] == closer:   # escaped '' ]] ""
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


def _validate_read_only(query: str):
    """Raises ValueError if the query is not a read-only query."""
    body = _without_literals(query or "").strip().rstrip(";").strip()
    if not body:
        raise ValueError("La query del FBS está vacía.")
    if ";" in body:
        raise ValueError(
            "Protección de solo lectura: la query del FBS debe ser UNA sola "
            "sentencia (hay un ';' en el medio).")
    first = body.split(None, 1)[0].lower()
    if first not in ("select", "with"):
        raise ValueError(
            "Protección de solo lectura: la query del FBS tiene que empezar "
            f"con SELECT (o WITH), no con {first.upper()}.")
    m = _FORBIDDEN.search(body)
    if m:
        raise ValueError(
            "Protección de solo lectura: la query del FBS contiene "
            f'"{m.group(0).upper()}", que no está permitido. Solo consultas.')


# real FBS column names -> canonical name (compared in lowercase; so the query
# doesn't need to rename anything but hoja and codigo)
COLUMN_SYNONYMS = {"detmovnro": "asiento", "detfecha": "fecha", "detref": "referencia",
                   "detcomenta": "comentario", "detdebe": "debe", "dethaber": "haber"}


_BLOCK_RE = re.compile(
    r"'([EO])'\s+AS\s+hoja\s*,\s*'(\d+)'\s+AS\s+codigo", re.IGNORECASE)


def _validate_unique_blocks(query: str):
    """A paste accident that repeats an account's block duplicates ALL its
    entries (UNION ALL doesn't filter). It is rejected, naming the account,
    before running."""
    pairs = [(h.upper(), c) for h, c in _BLOCK_RE.findall(query or "")]
    repeated = sorted({p for p in pairs if pairs.count(p) > 1})
    if repeated:
        det = ", ".join(f"({h}) ({c})" for h, c in repeated)
        raise ValueError(
            f"La query trae MÁS DE UNA VEZ el bloque de la cuenta {det}: "
            "eso duplica todos sus asientos. Dejá un solo SELECT por cuenta "
            "y hoja (parece un pegado repetido).")


def _normalize_row(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        kk = str(k).strip().lower()
        out.setdefault(COLUMN_SYNONYMS.get(kk, kk), v)
    return out


def _amount(v) -> float:
    """Amount from numeric/Decimal/float or text with a decimal comma."""
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


def _file_config() -> dict:
    """The config saved in datos/fbs_sql.json, without the environment overrides."""
    conf = {"servidor": "", "puerto": 1433, "base": "", "usuario": "",
            "clave": "", "query": SAMPLE_QUERY}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                conf.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return conf


def load_config() -> dict:
    conf = _file_config()
    # the environment overrides the file (deploys without touching datos/)
    conf["servidor"] = os.environ.get("FBS_SQL_SERVIDOR", conf["servidor"])
    conf["puerto"] = int(os.environ.get("FBS_SQL_PUERTO", conf["puerto"] or 1433))
    conf["base"] = os.environ.get("FBS_SQL_BASE", conf["base"])
    conf["usuario"] = os.environ.get("FBS_SQL_USUARIO", conf["usuario"])
    conf["clave"] = os.environ.get("FBS_SQL_CLAVE", conf["clave"])
    return conf


# ---- connection destination: which server the app may connect to -----------
# The manual connection can be edited from the UI/API; without checks, whoever
# managed to edit it could point the reconciler at a server of their own and
# receive the stored password. Rules:
#  * the destination can only change from the machine itself or with
#    FBS_SQL_EDITABLE=1 (a deployment decision), never anonymously;
#  * if FBS_SQL_SERVIDOR is in the environment, the destination is fixed;
#  * changing server/port/database/user without sending a new password clears
#    the stored one (it is never sent to a destination other than its own);
#  * the host must look like a name/IP and resolve to a private address
#    (RFC 1918 or 100.64/10); any other one (public, loopback) must be listed
#    in FBS_SQL_HOSTS_PERMITIDOS (comma-separated names, IPs or CIDR).
#    Link-local (169.254.x: cloud metadata), multicast and 0.0.0.0 are never
#    accepted.
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_$#-]{1,64}$")
_FORBIDDEN_PORTS = {22, 23, 25, 53, 80, 110, 143, 443, 445, 3389}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def destination_locked() -> bool:
    """True if the destination is fixed by the environment (not editable)."""
    return bool(os.environ.get("FBS_SQL_SERVIDOR", "").strip())


def _allowed_hosts() -> list:
    rules = []
    for item in os.environ.get("FBS_SQL_HOSTS_PERMITIDOS", "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        try:
            rules.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            rules.append(item)
    return rules


def _in_allowed_list(host: str, ips: list) -> bool:
    for r in _allowed_hosts():
        if isinstance(r, str):
            if r == host.lower():
                return True
        elif any(ip in r for ip in ips):
            return True
    return False


def validate_destination(server: str, port) -> None:
    """Raises ValueError if the destination is not acceptable (see above)."""
    server = (server or "").strip()
    host, _, instance = server.partition("\\")
    if not server or not _HOST_RE.match(host) or (
            instance and not _INSTANCE_RE.match(instance)):
        raise ValueError("El servidor del FBS no es válido: usá un nombre de host "
                         "o una IP (con \\instancia si corresponde).")
    try:
        port = int(port or 1433)
    except (TypeError, ValueError):
        raise ValueError("El puerto del FBS no es un número.") from None
    if not 1 <= port <= 65535 or port in _FORBIDDEN_PORTS:
        raise ValueError(f"El puerto {port} no es aceptable para una conexión SQL Server.")
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = sorted({ipaddress.ip_address(i[4][0])
                          for i in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
        except OSError:
            raise ValueError(f'El servidor "{host}" del FBS no resuelve.') from None
    for ip in ips:
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError(f"El servidor del FBS apunta a una dirección no permitida ({ip}).")
    if _in_allowed_list(host, ips):
        return
    for ip in ips:
        if ip.is_loopback or not (ip.is_private or ip in _CGNAT):
            raise ValueError(
                f"El servidor del FBS ({host} → {ip}) no es una dirección privada. "
                "Si es correcto, agregalo a FBS_SQL_HOSTS_PERMITIDOS en el servidor.")


def save_config(data: dict, allow_destination: bool = True):
    """Saves the manual connection. `allow_destination=False` (the caller is not
    local and FBS_SQL_EDITABLE is off) only lets the query through."""
    if str(data.get("query") or "").strip():
        _validate_read_only(str(data["query"]))
        _validate_unique_blocks(str(data["query"]))
    current = load_config()
    # what comes from the environment (FBS_SQL_*, password included) is not
    # copied to the file: it keeps what was in the file plus what changed
    conf = _file_config()
    previous = (current["servidor"], current["puerto"], current["base"], current["usuario"])
    if "query" in data:
        conf["query"] = str(data["query"] or "").strip()
    new = (str(data.get("servidor", current["servidor"]) or "").strip(),
             int(data.get("puerto") or current["puerto"] or 1433),
             str(data.get("base", current["base"]) or "").strip(),
             str(data.get("usuario", current["usuario"]) or "").strip())
    destination_changes = new != previous or bool(data.get("clave"))
    if destination_changes:
        if destination_locked():
            raise PermissionError(
                "El destino del FBS está fijado por el servidor (FBS_SQL_SERVIDOR) "
                "y no se puede cambiar desde la interfaz.")
        if not allow_destination:
            raise PermissionError(
                "Cambiar el servidor del FBS no está habilitado desde acá: hacelo "
                "en el propio servidor o activá FBS_SQL_EDITABLE=1.")
        conf["servidor"], conf["puerto"], conf["base"], conf["usuario"] = new
        validate_destination(conf["servidor"], conf["puerto"])
        if new != previous:
            conf["clave"] = ""          # never send the old password to another destination
        if data.get("clave"):          # write-only: empty = keep the current one
            conf["clave"] = str(data["clave"])
    os.makedirs(_DATA, exist_ok=True)
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=1)
    try:
        os.chmod(tmp_path, 0o600)       # the password must not be readable by others
    except OSError:
        pass
    os.replace(tmp_path, CONFIG_PATH)
    return conf


def is_configured() -> bool:
    c = load_config()
    return bool(c["servidor"] and c["base"] and c["usuario"])


def public_config(brand: str = "") -> dict:
    """Config without secrets, for the UI. With a brand, the hub accounts are
    limited to that brand (the mode is still hub if ANY account is configured,
    even when the requested brand has none)."""
    c = load_config()
    everything = hub_fbs_accounts()
    cfgs = _filter_configs(everything, brand)
    return {"configurado": is_configured() or bool(cfgs),
            "modo": "hub" if everything else "manual",
            "cuentas_hub": len(cfgs),
            "cuentas": [{"cuenta_id": x["cuenta_id"], "etiqueta": x["etiqueta"]}
                        for x in sorted(cfgs, key=lambda x: x["etiqueta"])],
            "conexiones_hub": sorted({x["conexion_nombre"] for x in cfgs}),
            "servidor": c["servidor"],
            "puerto": c["puerto"], "base": c["base"], "usuario": c["usuario"],
            "clave_presente": bool(c["clave"]), "query": c["query"]}


def _ipv4(host: str) -> str:
    """Resolves the name to IPv4: FreeTDS fails when the machine name resolves
    to IPv6 (typical on Windows networks). If it doesn't resolve, the name is
    left as is."""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def _connect_pymssql(conf: dict):
    import pymssql   # lazy import: the app starts even without the driver
    kwargs = dict(database=conf["base"], user=conf["usuario"],
                  password=descifrar_fbs(conf["clave"]), login_timeout=10, timeout=60,
                  charset="UTF-8")
    if os.environ.get("FBS_SQL_TDS"):        # e.g. 7.0 / 7.2 / 7.4
        kwargs["tds_version"] = os.environ["FBS_SQL_TDS"]
    server = (conf["servidor"] or "").strip()
    if "\\" in server:
        # named instance (HOST\SQLEXPRESS): the port is resolved by the
        # server's SQL Browser (UDP 1434) — no fixed port is passed
        host, instance = server.split("\\", 1)
        kwargs["server"] = f"{_ipv4(host)}\\{instance}"
    else:
        kwargs["server"] = _ipv4(server)
        kwargs["port"] = conf["puerto"]
    return pymssql.connect(**kwargs)


def _query_pymssql(conf: dict, query: str, params: dict) -> list[dict]:
    con = _connect_pymssql(conf)
    try:
        con.autocommit(False)   # open transaction, never committed
        cur = con.cursor(as_dict=True)
        cur.execute(query, params) if params else cur.execute(query)
        rows = cur.fetchall()
    finally:
        try:
            con.rollback()      # read-only: any effect is undone
        finally:
            con.close()
    return rows


def _query_pyodbc(conf: dict, query: str, params: dict) -> list[dict]:
    """Through Windows' native ODBC driver (the same SSMS uses): it handles
    mandatory encryption and named instances much better than FreeTDS."""
    import pyodbc
    candidates = [d for d in pyodbc.drivers() if "SQL Server" in d]
    if not candidates:
        raise ImportError("sin driver ODBC de SQL Server instalado")
    preference = ("ODBC Driver 18", "ODBC Driver 17", "ODBC Driver 13",
                  "SQL Server Native Client", "SQL Server")
    driver = min(candidates,
                 key=lambda d: next((i for i, p in enumerate(preference) if d.startswith(p)), 99))
    server = (conf["servidor"] or "").strip()
    srv = server if "\\" in server else f"{server},{conf['puerto']}"
    conn_str = (f"DRIVER={{{driver}}};SERVER={srv};DATABASE={conf['base']};"
                f"UID={conf['usuario']};PWD={descifrar_fbs(conf['clave'])};")
    conn_str += "ApplicationIntent=ReadOnly;"
    if driver.startswith("ODBC Driver"):
        # the FBS SQL Server uses a self-signed certificate
        conn_str += "Encrypt=yes;TrustServerCertificate=yes;"
    # pyodbc uses positional '?' parameters: translate %(desde)s / %(hasta)s
    names = []
    q = re.sub(r"%\((desde|hasta)\)s",
               lambda m: (names.append(m.group(1)), "?")[1], query)
    con = pyodbc.connect(conn_str, timeout=10, autocommit=False)
    try:
        cur = con.cursor()
        cur.execute(q, *[params[n] for n in names]) if names else cur.execute(q)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, f, strict=False)) for f in cur.fetchall()]
    finally:
        try:
            con.rollback()      # read-only
        finally:
            con.close()
    return rows


_DRIVER_USED = {"nombre": None}


def _query_for_log(query: str, params: dict) -> str:
    """The query with the parameters substituted, ready to paste into SSMS."""
    q = query
    for k, v in (params or {}).items():
        q = q.replace(f"%({k})s", f"'{v}'")
    return q


_VERIFIED_LOGINS: set = set()
_PRIVILEGES_SQL = (
    "SELECT ISNULL(IS_SRVROLEMEMBER('sysadmin'), 0) AS sysadmin, "
    "ISNULL(IS_MEMBER('db_owner'), 0) AS db_owner, "
    "ISNULL(IS_MEMBER('db_datawriter'), 0) AS db_datawriter, "
    "ISNULL(IS_MEMBER('db_ddladmin'), 0) AS db_ddladmin")


def write_login_allowed() -> bool:
    return os.environ.get("FBS_SQL_PERMITIR_LOGIN_ESCRITURA", "").strip().lower() in (
        "1", "true", "on", "si")


def _check_read_only_login(conf: dict):
    """Layer 3 of the read-only guarantee: the login the reconciler connects
    with can't be an administrator nor able to write. Checked once per
    (server, database, user) and rejected if it has sysadmin, db_owner,
    db_datawriter or db_ddladmin. FBS_SQL_PERMITIR_LOGIN_ESCRITURA=1 lets it
    through (a conscious exception, while a read-only login is created)."""
    if simulated() or write_login_allowed():
        return
    key = (conf.get("servidor"), conf.get("puerto"), conf.get("base"),
           conf.get("usuario"))
    if key in _VERIFIED_LOGINS:
        return
    rows = _query_with_driver(conf, _PRIVILEGES_SQL, {})
    row = {str(k).lower(): v for k, v in (rows[0] if rows else {}).items()}
    granted = [k for k in ("sysadmin", "db_owner", "db_datawriter", "db_ddladmin")
                   if row.get(k) == 1]
    if granted or not row:
        raise ValueError(
            "Protección de solo lectura: el usuario del FBS tiene permisos de "
            f"escritura o administración ({', '.join(granted) or 'no se pudo verificar'}). "
            "Creá un login con solo db_datareader y usalo acá.")
    _VERIFIED_LOGINS.add(key)


def _query(conf: dict, query: str, params: dict) -> list[dict]:
    """_query_with_driver measured for monitoring: each FBS query is a span
    (server, database, driver, rows, latency and the error if it fails)."""
    if simulated():
        # the demo never reaches a SQL Server: the hub mode uses the simulation
        # and the manual connection doesn't exist there
        raise ValueError("En la demo no hay conexión a un FBS real.")
    validate_destination(conf.get("servidor"), conf.get("puerto"))
    _check_read_only_login(conf)
    with telemetria.tramo("FBS consulta", tipo="cliente", **{
            "db.system.name": "mssql",
            "server.address": str(conf.get("servidor") or ""),
            "db.namespace": str(conf.get("base") or ""),
            "fbs.conexion": conf.get("nombre")}) as span:
        rows = _query_with_driver(conf, query, params)
        span.set_attribute("fbs.filas", len(rows))
        span.set_attribute("fbs.driver", _DRIVER_USED.get("nombre") or "")
        return rows


def _query_with_driver(conf: dict, query: str, params: dict) -> list[dict]:
    """Runs the query with the best driver available: Windows' native ODBC
    first, FreeTDS (pymssql) as the fallback. FBS_SQL_DRIVER (pyodbc|pymssql)
    forces just one."""
    print("[fbs] Query a ejecutar (copiable a SSMS):\n"
          "-------------------------------------------\n"
          + _query_for_log(query, params)
          + "\n-------------------------------------------", flush=True)
    chosen = (os.environ.get("FBS_SQL_DRIVER") or "auto").lower()
    errors = []
    if chosen in ("auto", "pyodbc"):
        try:
            rows = _query_pyodbc(conf, query, params)
            _DRIVER_USED["nombre"] = "pyodbc"
            return rows
        except ImportError as exc:
            if chosen == "pyodbc":
                raise ValueError(f"pyodbc no disponible: {exc}") from exc
            errors.append(f"[odbc] {exc}")
        except Exception as exc:  # noqa: BLE001
            if chosen == "pyodbc":
                raise
            errors.append(f"[odbc] {exc}")
    if chosen in ("auto", "pymssql"):
        try:
            rows = _query_pymssql(conf, query, params)
            _DRIVER_USED["nombre"] = "pymssql"
            return rows
        except Exception as exc:  # noqa: BLE001
            errors.append(f"[pymssql] {exc}")
    raise RuntimeError(" — ".join(str(e) for e in errors))


def _log_rows(rows: list[dict]):
    print(f"[fbs] La consulta devolvió {len(rows)} fila(s) "
          f"(driver {_DRIVER_USED['nombre']})", flush=True)


def test_connection() -> dict:
    """Tests the connection (SELECT 1). In hub mode it tests ALL the configured
    connections; in manual mode, the one of the modal."""
    cfgs = hub_fbs_accounts()
    if cfgs:
        seen, detail = set(), []
        for c in cfgs:
            if c["conexion_id"] in seen:
                continue
            seen.add(c["conexion_id"])
            if simulated():
                detail.append({"nombre": c["conexion_nombre"], "ok": True,
                               "driver": "simulado (demo)"})
                continue
            try:
                _query(c["conexion"], "SELECT 1 AS uno", {})
                detail.append({"nombre": c["conexion_nombre"], "ok": True,
                               "driver": _DRIVER_USED["nombre"]})
            except Exception as exc:  # noqa: BLE001
                detail.append({"nombre": c["conexion_nombre"], "ok": False,
                               "error": str(exc)})
        return {"ok": all(d["ok"] for d in detail), "modo": "hub",
                "conexiones": detail,
                "driver": "simulado (demo)" if simulated() else _DRIVER_USED["nombre"],
                "error": " | ".join(f'{d["nombre"]}: {d["error"]}'
                                    for d in detail if not d["ok"]) or None}
    conf = load_config()
    if not is_configured():
        return {"ok": False, "error": "Faltan servidor, base o usuario en la configuración"}
    try:
        _query(conf, "SELECT 1 AS uno", {})
        return {"ok": True, "modo": "manual", "driver": _DRIVER_USED["nombre"]}
    except Exception as exc:  # noqa: BLE001 — the error is shown to the user
        return {"ok": False, "error": str(exc)}


def _parse_date(v):
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


def fetch(date_from: str, date_to: str, brand: str = "",
          only_accounts: list[str] | None = None) -> list[dict]:
    """Runs the FBS query for the range [date_from, date_to] (ISO) and returns
    a list of 'virtual files' with the same shape that
    parsers.daily_files.identify() returns for an FBS report: one per ledger
    account and sheet. Raises ValueError with a clear message if anything fails.

    Hub mode (preferred): if the hub has accounts with an FBS connection and
    E/O plan ids, the query is generated on DetMov, one query per connection.
    If nothing is configured in the hub, the modal's manual query is used
    (advanced mode)."""
    everything = hub_fbs_accounts()
    if everything:
        cfgs = _filter_configs(everything, brand)
        if only_accounts:
            chosen = set(only_accounts)
            cfgs = [c for c in cfgs if c["cuenta_id"] in chosen]
        if not cfgs:
            raise ValueError(
                "Ninguna de las cuentas elegidas tiene FBS configurado en el "
                "hub" + (f" para la marca {brand}" if brand else "") + ".")
        return _fetch_from_hub(cfgs, date_from, date_to)
    conf = load_config()
    if not is_configured():
        raise ValueError(
            "No hay conexión al FBS: configurala en el hub (pestaña "
            "Conexiones FBS + IDs E/O en cada cuenta) o cargá la query "
            "manual en este modal.")
    _validate_read_only(conf["query"])
    _validate_unique_blocks(conf["query"])
    try:
        rows = _query(conf, conf["query"], {"desde": date_from, "hasta": date_to})
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"La consulta al FBS falló: {exc}") from exc
    _log_rows(rows)
    return _infos_from_rows(rows)


def _infos_from_rows(rows: list[dict],
                     account_by_code: dict | None = None) -> list[dict]:
    """Groups the raw rows by (codigo, hoja) and builds the 'virtual files'.
    If the codigo -> bank account link is known (hub config), it travels in
    cuenta_id_config and no manual assignment is needed."""
    rows = [_normalize_row(f) for f in rows]
    if rows:
        missing = [c for c in COLUMNS if c not in rows[0]]
        if missing:
            raise ValueError(
                "A la query del FBS le faltan columnas: " + ", ".join(missing)
                + ". Las del FBS (DetMovNro, DetFecha, DetRef, DetComenta, "
                "DetDebe, DetHaber) se reconocen solas; hoja y codigo se "
                "agregan como literales: SELECT 'O' AS hoja, '109' AS codigo, …")

    groups: dict[tuple, dict] = {}
    for f in rows:
        letter = str(f.get("hoja") or "").strip().upper()[:1]
        code = str(f.get("codigo") or "").strip()
        if letter not in ("E", "O") or not code:
            continue
        g = groups.setdefault((code, letter), {
            "asientos": [], "nombre_fbs": None})
        if f.get("nombre_fbs") and not g["nombre_fbs"]:
            g["nombre_fbs"] = str(f["nombre_fbs"]).strip()[:80]
        n = len(g["asientos"]) + 1
        g["asientos"].append(LedgerEntry(
            id=f"{letter}#{n}", sheet=letter,
            number=str(f.get("asiento") or "").strip() or n,
            date=_parse_date(f.get("fecha")),
            reference=str(f.get("referencia") or "").strip(),
            comment=str(f.get("comentario") or "").strip(),
            debit=round(_amount(f.get("debe")), 2),
            credit=round(_amount(f.get("haber")), 2)))

    out = []
    for (code, letter), g in sorted(groups.items()):
        dates = [a.date for a in g["asientos"] if a.date]
        out.append({
            "tipo": "fbs", "hoja": letter, "codigo_fbs": code,
            "codigos": {letter: code}, "nombre_fbs": g["nombre_fbs"],
            "asientos": g["asientos"],
            "desde": min(dates) if dates else None,
            "hasta": max(dates) if dates else None,
            "cantidad": len(g["asientos"]),
            "archivo": f"FBS directo · ({letter}) ({code})",
            "cuenta_id_config": (account_by_code or {}).get(code),
        })
    return out


# ---- hub mode: the config (connections + E/O ids per account) lives in the
# shared Postgres and the query is generated on DetMov -----------------------

def hub_fbs_accounts() -> list[dict]:
    """Hub bank accounts that have an FBS connection + plan ids.
    Returns [] if the hub has nothing configured yet (or there is no DB)."""
    from engine import accounts as accounts_mod
    url = accounts_mod._database_url()
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
            rows = cur.fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — no hub configured is not an error
        print(f"[fbs] No pude leer la config FBS del hub ({exc})")
        _HUB_STATE["error"] = str(exc)
        return []
    _HUB_STATE["error"] = None
    out = []
    for db_id, bank, number, currency, pe, po, fid, fname, srv, prt, base, usr, password, brand in rows:
        out.append({
            "cuenta_id": (f"{accounts_mod._bank_key(bank)}-"
                          f"{accounts_mod._digits(number)}-{(currency or '').lower()}"),
            "db_id": db_id, "banco": accounts_mod._bank_key(bank), "numero": number,
            "moneda": (currency or "").upper(),
            "empresa": brand,
            "etiqueta": (f"{brand} — " if brand else "") + f"{bank} {number} ({currency})",
            "plan_e": str(pe or "").strip(), "plan_o": str(po or "").strip(),
            "conexion_id": fid, "conexion_nombre": fname,
            "conexion": {"servidor": srv or "", "puerto": prt or 1433,
                         "base": base or "", "usuario": usr or "",
                         "clave": password or "", "query": ""},
        })
    return out


_HUB_STATE = {"error": None}


def diagnostics() -> dict:
    """State of the hub mode, for GET /api/diagnostico."""
    from engine import accounts as accounts_mod
    cfgs = hub_fbs_accounts()
    return {
        "database_url_presente": bool(accounts_mod._database_url()),
        "cuentas_configuradas": len(cfgs),
        "cuentas": [x["etiqueta"] for x in cfgs],
        "conexiones": sorted({x["conexion_nombre"] for x in cfgs}),
        "error_lectura_hub": _HUB_STATE["error"],
        "modo": "hub" if cfgs else "manual",
        "simulado": simulated(),
    }


def _filter_configs(cfgs: list[dict], brand: str) -> list[dict]:
    """FBS accounts of a brand (same flexible criterion as the rest)."""
    if not brand:
        return cfgs
    from engine import accounts as accounts_mod
    m = accounts_mod.slug(brand)
    return [c for c in cfgs if m in accounts_mod.slug(c.get("empresa") or "")]


def _query_detmov(plans: list[tuple[str, int]]) -> str:
    """Query generated on DetMov: one block per (sheet, plan id)."""
    blocks = [
        (f"SELECT '{sheet}' AS hoja, '{plan}' AS codigo, "
         "DetMovNro, DetFecha, DetRef, DetComenta, DetDebe, DetHaber "
         f"FROM DetMov WHERE DetPlan = {plan} "
         "AND DetFecha BETWEEN %(desde)s AND %(hasta)s")
        for sheet, plan in plans]
    return "\nUNION ALL\n".join(blocks)


def _fetch_from_hub(cfgs: list[dict], date_from: str, date_to: str) -> list[dict]:
    """One generated query per FBS connection, with all its E/O pairs."""
    by_connection: dict = {}
    for c in cfgs:
        g = by_connection.setdefault(c["conexion_id"], {
            "conf": c["conexion"], "nombre": c["conexion_nombre"],
            "planes": [], "cuenta_por_codigo": {}})
        for sheet, plan in (("E", c["plan_e"]), ("O", c["plan_o"])):
            if not plan:
                continue
            try:
                plan_n = int(plan)
            except ValueError:
                raise ValueError(
                    f'El ID de la cuenta {sheet} de {c["etiqueta"]} configurado '
                    f'en el hub no es numérico: "{plan}".') from None
            if (sheet, plan_n) in g["planes"]:
                # two hub accounts with the same plan id: a single block,
                # otherwise every entry would come twice
                print(f"[fbs] Aviso: el plan {plan_n} ({sheet}) está configurado "
                      f"en más de una cuenta del hub; se consulta una sola vez",
                      flush=True)
                continue
            g["planes"].append((sheet, plan_n))
            g["cuenta_por_codigo"][str(plan_n)] = c["cuenta_id"]

    # yyyymmdd: the only date format SQL Server reads the same in any
    # language/regional setting
    params = {"desde": date_from.replace("-", ""), "hasta": date_to.replace("-", "")}
    out, errors = [], []
    if simulated():
        # demo: there is no SQL Server; the DetMov rows come from the simulation,
        # the same one that builds the statements (engine/simulacion.py)
        from engine import simulacion
        d1, d2 = date.fromisoformat(date_from), date.fromisoformat(date_to)
        accounts = simulacion.con_roles(cfgs, hub_fbs_accounts())
        for connection_id, g in by_connection.items():
            rows = [f for c in accounts if c["conexion_id"] == connection_id
                    for f in simulacion.filas_detmov(c, d1, d2)]
            print(f"[fbs] (demo) {g['nombre']}: {len(rows)} fila(s) simuladas", flush=True)
            out.extend(_infos_from_rows(rows, g["cuenta_por_codigo"]))
        return out
    for g in by_connection.values():
        if not g["planes"]:
            continue
        query = _query_detmov(g["planes"])
        _validate_read_only(query)   # defense in depth
        try:
            rows = _query(g["conf"], query, params)
        except Exception as exc:  # noqa: BLE001
            errors.append(f'{g["nombre"]}: {exc}')
            continue
        _log_rows(rows)
        out.extend(_infos_from_rows(rows, g["cuenta_por_codigo"]))
    if errors and not out:
        raise ValueError("La consulta al FBS falló: " + " | ".join(errors))
    if errors:
        print(f"[fbs] Conexiones con error (parcial): {errors}")
    return out
