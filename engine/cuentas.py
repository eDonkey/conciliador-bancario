# -*- coding: utf-8 -*-
"""Cuentas bancarias del grupo Nave y su mapeo contra el sistema FBS.

La fuente central de cuentas es el Postgres del hub (tablas marcas y
cuentas_bancarias, solo lectura): lo que el admin cambia ahí se refleja acá
sin redeploy (cache corto). Si la base no está disponible se usa la copia
local datos/cuentas_nave.json (o la semilla histórica) como fallback.

Los mapeos contra el FBS siguen siendo locales: el número interno del FBS no
coincide con el número de cuenta bancaria, así que el vínculo se hace por el
código de cuenta contable que viene en la hoja del reporte ("(E) (1141121)"):
la primera vez el usuario asigna el FBS a una cuenta y el mapeo queda
aprendido para siempre, colgado del id interno (banco-dígitos-moneda), que es
estable entre la semilla y la base.
"""
import json
import os
import re
import time
import unicodedata

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUTA_DEFAULT = os.path.join(BASE_DIR, "datos", "cuentas_nave.json")
RUTA_ENV = os.path.join(BASE_DIR, ".env")   # dev local, gitignoreado
CACHE_TTL = 60   # segundos; también evita martillar la base si está caída

# (empresa, banco, numero, moneda) — tabla pasada por el cliente; la cuenta
# BBVA 109-013562/1 de Lumiere no figuraba pero llegó extracto real de ella.
SEMILLA = [
    ("Le Mans", "santander", "043-016675/9", "USD"),
    ("Le Mans", "santander", "043-029406/7", "USD"),
    ("Le Mans", "santander", "043-036647/0", "ARS"),
    ("Le Mans", "santander", "250-755185/3", "ARS"),
    ("Le Mans", "santander", "250-755185/3", "USD"),
    ("Le Mans", "santander", "742-000483/5", "ARS"),
    ("Le Mans", "santander", "742-018737/2", "ARS"),
    ("Le Mans", "santander", "742-018752/5", "USD"),
    ("Lumiere", "santander", "250-755160/0", "ARS"),
    ("Lumiere", "santander", "250-755160/0", "USD"),
    ("Lumiere", "santander", "742-001061/2", "ARS"),
    ("Lumiere", "santander", "742-001087/2", "USD"),
    ("Gac - Kyoto", "santander", "043-037781/8", "ARS"),
    ("Gac - Kyoto", "santander", "043-037782/5", "USD"),
    ("Le Mans", "frances", "0109-010968/2", "ARS"),
    ("Le Mans", "frances", "0109-035581/8", "ARS"),
    ("Le Mans", "frances", "0109-037668/4", "ARS"),
    ("LEAP", "frances", "0109-064858/9", "ARS"),
    ("Le Mans", "frances", "0109-402090/5", "USD"),
    ("LEAP", "frances", "0109-402118/2", "USD"),
    ("Lumiere", "frances", "0109-013562/1", "ARS"),
    ("Le Mans", "galicia", "0000601-5 228-1", "ARS"),
    ("LEAP", "galicia", "0000266-2 702-7", "ARS"),
    ("Le Mans", "galicia", "0000830-1 228-1", "ARS"),
    ("Lumiere", "macro", "351909419853038", "ARS"),
    ("Lumiere", "macro", "230209557660631", "USD"),
    ("Le Mans", "macro", "351909419852998", "ARS"),
    ("Le Mans", "macro", "230209558049538", "USD"),
    ("Lumiere", "ciudad", "307300050211430", "ARS"),
    ("Le Mans", "ciudad", "307300050211454", "ARS"),
]

BANCOS = {"santander": "Santander/Río", "frances": "BBVA/Francés",
          "galicia": "Galicia", "macro": "Macro", "ciudad": "Ciudad"}


def _digits(s: str) -> str:
    return re.sub(r'\D', '', s or '')


def _banco_key(nombre: str) -> str:
    """'Francés' / 'Frances' -> 'frances' (el key interno histórico)."""
    plano = unicodedata.normalize("NFD", nombre or "")
    plano = "".join(ch for ch in plano if not unicodedata.combining(ch))
    return plano.strip().lower()


def _database_url() -> str | None:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    try:
        with open(RUTA_ENV, encoding="utf-8") as f:
            for lin in f:
                if lin.strip().startswith("DATABASE_URL="):
                    return lin.strip().split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


_cache = {"hasta": 0.0, "cuentas": None}


def _desde_db() -> list[dict] | None:
    """Cuentas activas desde el Postgres del hub. None si no hay URL o la base
    no responde; el resultado (bueno o malo) se cachea CACHE_TTL segundos."""
    ahora = time.time()
    if ahora < _cache["hasta"]:
        return _cache["cuentas"]
    _cache["hasta"] = ahora + CACHE_TTL
    _cache["cuentas"] = None
    url = _database_url()
    if not url:
        print("[cuentas] Sin DATABASE_URL: uso la copia local de cuentas")
        return None
    try:
        import psycopg2
        con = psycopg2.connect(url, connect_timeout=5)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT c.id, c.banco, c.numero, c.moneda, c.alias, m.nombre AS marca "
                "FROM cuentas_bancarias c JOIN marcas m ON m.id = c.marca_id "
                "WHERE c.activa = true ORDER BY m.nombre, c.banco, c.numero")
            filas = cur.fetchall()
        finally:
            con.close()
        cuentas = [{
            "id": f"{_banco_key(banco)}-{_digits(numero)}-{(moneda or '').lower()}",
            "empresa": marca, "banco": _banco_key(banco), "numero": numero,
            "moneda": moneda, "alias": (alias or "").strip() or None,
            "db_id": db_id, "fbs_e": None, "fbs_o": None, "fbs_nombre": None,
        } for db_id, banco, numero, moneda, alias, marca in filas]
        _cache["cuentas"] = cuentas or None
        return _cache["cuentas"]
    except Exception as e:
        print(f"[cuentas] No pude leer las cuentas del Postgres del hub ({e}); "
              "uso la copia local como fallback")
        return None


def _cargar_local(ruta: str) -> list[dict]:
    """Copia local: último snapshot guardado (con los mapeos FBS) o, si no
    existe, la semilla histórica."""
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    cuentas = [{
        "id": f"{banco}-{_digits(numero)}-{moneda.lower()}",
        "empresa": empresa, "banco": banco, "numero": numero, "moneda": moneda,
        "fbs_e": None, "fbs_o": None, "fbs_nombre": None,
    } for empresa, banco, numero, moneda in SEMILLA]
    guardar(cuentas, ruta)
    return cuentas


def cargar(ruta: str = RUTA_DEFAULT) -> list[dict]:
    locales = _cargar_local(ruta)
    base = _desde_db()
    if base is None:
        return locales
    # la identidad (qué cuentas existen, marca, alias) la manda la base;
    # los mapeos FBS aprendidos se conservan de la copia local, por id.
    # Copias: los llamadores mutan las filas y el cache debe quedar intacto.
    por_id = {c["id"]: c for c in locales}
    merged = []
    for c in base:
        c = dict(c)
        prev = por_id.get(c["id"])
        if prev:
            for k in ("fbs_e", "fbs_o", "fbs_nombre"):
                c[k] = prev.get(k)
        merged.append(c)
    return merged


def guardar(cuentas: list[dict], ruta: str = RUTA_DEFAULT):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(cuentas, f, ensure_ascii=False, indent=1)


def etiqueta(c: dict) -> str:
    base = f'{c["empresa"]} — {BANCOS.get(c["banco"], c["banco"])} {c["numero"]} ({c["moneda"]})'
    # el alias es descriptivo, no identificatorio (hay repetidos): se anexa
    return f'{base} · {c["alias"]}' if c.get("alias") else base


def _segmentos(s: str) -> tuple:
    """Segmentos numéricos como enteros: '742-000483/5' -> (742, 483, 5).
    Ignora los ceros de relleno que cada banco pone distinto."""
    return tuple(int(x) for x in re.findall(r'\d+', s or ''))


def buscar_por_numero(cuentas, banco, numero, moneda=None):
    """Matchea la cuenta detectada en un extracto contra la tabla: por dígitos
    sin ceros a la izquierda, o por segmentos numéricos ('742-483-5' matchea
    '742-000483/5')."""
    if not numero:
        return None
    d = _digits(numero).lstrip('0')
    seg = _segmentos(numero)
    for c in cuentas:
        if banco and c["banco"] != banco:
            continue
        if moneda and c["moneda"] != moneda:
            continue
        cd = _digits(c["numero"]).lstrip('0')
        if cd == d or (len(d) >= 6 and (cd.endswith(d) or d.endswith(cd))):
            return c
        if len(seg) >= 2 and seg == _segmentos(c["numero"]):
            return c
    return None


def buscar_por_fbs(cuentas, codigo):
    for c in cuentas:
        if codigo and codigo in (c.get("fbs_e"), c.get("fbs_o")):
            return c
    return None


BANCO_KW = {"santander": ("SANTANDER", "RIO ALEM"), "frances": ("BBVA", "FRANCES"),
            "galicia": ("GALICIA",), "macro": ("MACRO",), "ciudad": ("CIUDAD",)}


def buscar_por_nombre_fbs(cuentas, nombre):
    """Algunos FBS traen el número real de cuenta en el nombre interno
    ('CIT - BANCO SANTANDER CC $ 742-000483/5'): banco por palabra clave,
    moneda por el símbolo, número por segmentos."""
    if not nombre:
        return None
    up = nombre.upper()
    banco = next((b for b, kws in BANCO_KW.items()
                  if any(k in up for k in kws)), None)
    moneda = "USD" if re.search(r'U\$S|USD|DOLAR', up) else ("ARS" if "$" in up else None)
    m = re.search(r'\b(\d{3,4})[-. ](\d{1,6})[-/. ](\d)\b', nombre)
    if not m:
        return None
    return buscar_por_numero(cuentas, banco, "-".join(m.groups()), moneda)


def mapear_fbs(cuenta_id, hoja, codigo, nombre_fbs=None, ruta: str = RUTA_DEFAULT):
    """Aprende que el código FBS <codigo> (hoja E u O) es la cuenta <cuenta_id>."""
    cuentas = cargar(ruta)
    for c in cuentas:
        # un código pertenece a una sola cuenta: limpiar asignaciones viejas
        for k in ("fbs_e", "fbs_o"):
            if c.get(k) == codigo and c["id"] != cuenta_id:
                c[k] = None
    for c in cuentas:
        if c["id"] == cuenta_id:
            c["fbs_e" if hoja == "E" else "fbs_o"] = codigo
            if nombre_fbs:
                c["fbs_nombre"] = nombre_fbs
    guardar(cuentas, ruta)
    return cuentas
