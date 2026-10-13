# -*- coding: utf-8 -*-
"""Bank accounts of the Nave group and their mapping to the FBS system.

The central source of accounts is the hub's Postgres (tables marcas and
cuentas_bancarias, read-only): what the admin changes there shows up here
without a redeploy (short cache). If the database is not available, the local
copy datos/cuentas_nave.json (or the historical seed) is the fallback.

The FBS mappings are still local: the FBS internal number doesn't match the
bank account number, so the link is made through the ledger account code that
comes in the report's sheet ("(E) (1141121)"): the first time the user assigns
the FBS to an account and the mapping is learned forever, hanging from the
internal id (bank-digits-currency), which is stable between the seed and the
database.
"""
import json
import os
import re
import time
import unicodedata

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(BASE_DIR, "datos", "cuentas_nave.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")   # local dev, gitignored
CACHE_TTL = 60   # seconds; also avoids hammering the database if it is down

# Account seed (company, bank, number, currency): OUTSIDE the code. The
# central source is the hub's Postgres; this is only the fallback when the
# database doesn't answer. Read from CUENTAS_SEMILLA_RUTA or from
# config/cuentas_semilla.json (ignored by git: it has real numbers). The
# format is in config/cuentas_semilla.example.json. Without the file the seed
# is empty and the app depends on the hub.
SEED_PATH = os.environ.get("CUENTAS_SEMILLA_RUTA") or os.path.join(
    BASE_DIR, "config", "cuentas_semilla.json")


def _read_seed() -> list[tuple]:
    try:
        with open(SEED_PATH, encoding="utf-8") as f:
            rows = json.load(f)
        return [(c["empresa"], c["banco"], c["numero"], c["moneda"]) for c in rows]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[accounts] Unreadable seed in {SEED_PATH} ({exc}); it stays empty")
        return []


BANKS = {"santander": "Santander/Río", "frances": "BBVA/Francés",
         "galicia": "Galicia", "macro": "Macro", "ciudad": "Ciudad"}


def _digits(s: str) -> str:
    return re.sub(r'\D', '', s or '')


# The same bank is written differently in the hub depending on who loads it
# ("BBVA", "BBVA/Francés", "Banco Francés"). Since the account id is derived
# from the bank, without this a rename changes the id and orphans the saved runs.
_BANK_ALIASES = {
    "bbva": "frances", "bbvafrances": "frances", "bancofrances": "frances",
    "bbvabancofrances": "frances", "bbvaargentina": "frances",
    "santanderrio": "santander", "bancosantander": "santander", "santanderargentina": "santander",
    "bancogalicia": "galicia", "galiciamas": "galicia",
    "bancomacro": "macro", "bancociudad": "ciudad", "ciudaddebuenosaires": "ciudad",
}


def _bank_key(name: str) -> str:
    """'Francés' / 'Frances' / 'BBVA' -> 'frances' (the historical internal key)."""
    plain = unicodedata.normalize("NFD", name or "")
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    key = plain.strip().lower()
    return _BANK_ALIASES.get(re.sub(r'[^a-z0-9]', '', key), key)


def _database_url() -> str | None:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("DATABASE_URL="):
                    return line.strip().split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


_cache = {"hasta": 0.0, "cuentas": None}


def _from_db() -> list[dict] | None:
    """Active accounts from the hub's Postgres. None if there is no URL or the
    database doesn't answer; the result (good or bad) is cached CACHE_TTL seconds."""
    now = time.time()
    if now < _cache["hasta"]:
        return _cache["cuentas"]
    _cache["hasta"] = now + CACHE_TTL
    _cache["cuentas"] = None
    url = _database_url()
    if not url:
        print("[accounts] No DATABASE_URL: using the local copy of the accounts")
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
            rows = cur.fetchall()
        finally:
            con.close()
        accounts = [{
            "id": f"{_bank_key(bank)}-{_digits(number)}-{(currency or '').lower()}",
            "empresa": brand, "banco": _bank_key(bank), "numero": number,
            "moneda": currency, "alias": (alias or "").strip() or None,
            "db_id": db_id, "fbs_e": None, "fbs_o": None, "fbs_nombre": None,
        } for db_id, bank, number, currency, alias, brand in rows]
        _cache["cuentas"] = accounts or None
        return _cache["cuentas"]
    except Exception as e:
        print(f"[accounts] Could not read the accounts from the hub Postgres ({e}); "
              "falling back to the local copy")
        return None


def _load_local(path: str) -> list[dict]:
    """Local copy: the last saved snapshot (with the FBS mappings) or, if it
    doesn't exist, the historical seed."""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    accounts = [{
        "id": f"{bank}-{_digits(number)}-{currency.lower()}",
        "empresa": company, "banco": bank, "numero": number, "moneda": currency,
        "fbs_e": None, "fbs_o": None, "fbs_nombre": None,
    } for company, bank, number, currency in _read_seed()]
    save(accounts, path)
    return accounts


def load(path: str = DEFAULT_PATH) -> list[dict]:
    local = _load_local(path)
    base = _from_db()
    if base is None:
        return local
    # the identity (which accounts exist, brand, alias) comes from the database;
    # the learned FBS mappings are kept from the local copy, by id.
    # Copies: callers mutate the rows and the cache must stay intact.
    by_id = {c["id"]: c for c in local}
    # fallback by number + currency: if the bank is renamed in the hub, the id
    # changes but the account is the same and the FBS mappings are not lost
    by_number = {(account_number(c["id"]), (c.get("moneda") or "").lower()): c for c in local}
    merged = []
    for c in base:
        c = dict(c)
        prev = by_id.get(c["id"]) or by_number.get(
            (account_number(c["id"]), (c.get("moneda") or "").lower()))
        if prev:
            for k in ("fbs_e", "fbs_o", "fbs_nombre"):
                c[k] = prev.get(k)
        merged.append(c)
    return merged


def save(accounts: list[dict], path: str = DEFAULT_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=1)


def account_number(account_id: str) -> str:
    """The number inside the id ('frances-01090135621-ars' -> '1090135621'),
    without leading zeros. It's the part that does NOT change when the bank or
    the brand is renamed in the hub: that's why the history compares on it."""
    m = re.search(r'-(\d+)-', str(account_id or ""))
    return m.group(1).lstrip("0") if m else ""


def bank_name(account_id: str) -> str:
    """Bank name from the id, tolerating aliases ('bbva' = 'frances')."""
    key = str(account_id or "").split("-")[0]
    return BANKS.get(_BANK_ALIASES.get(re.sub(r'[^a-z0-9]', '', key.lower()), key), "")


def slug(s: str) -> str:
    """'Le Mans' / 'le-mans' / 'LEMANS' -> 'lemans' (to compare brands)."""
    plain = unicodedata.normalize("NFD", s or "")
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return re.sub(r'[^a-z0-9]', '', plain.lower())


def _words(s: str) -> set:
    """Normalized words of a brand name, without separators."""
    plain = unicodedata.normalize("NFD", s or "")
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return {p for p in re.split(r'[^a-z0-9]+', plain.lower()) if p}


def same_brand(a: str, b: str) -> bool:
    """Are two brand names the same? Tolerates abbreviations and renames that
    only change the order ('Renault - Lumiere' vs 'Lumiere - Renault'), like
    filter_brand. Without this, renaming a brand in the hub hid the runs
    already saved with the old name."""
    sa, sb = slug(a), slug(b)
    if not sa or not sb:
        return False
    if sa == sb or sa in sb or sb in sa:
        return True
    pa, pb = _words(a), _words(b)
    return bool(pa and pb) and (pa <= pb or pb <= pa)


def filter_brand(accounts: list[dict], brand: str) -> list[dict]:
    """Accounts of a brand. Matches the full or the abbreviated name:
    'gac-kyoto' is enough for 'Gac - Kyoto Driving Automoviles SA'.

    If that finds nothing, it compares the words regardless of their order:
    the name travels in the URL from the hub and a rename ('Renault - Lumiere'
    vs 'Lumiere - Renault') left the page without accounts or FBS connections.
    """
    m = slug(brand)
    if not m:
        return accounts
    exact = [c for c in accounts if m in slug(c["empresa"])]
    if exact:
        return exact
    wanted = _words(brand)
    return [c for c in accounts if wanted and wanted <= _words(c["empresa"])]


def brands_with_accounts(accounts: list[dict]) -> list[str]:
    """Brands that have accounts today (to explain an empty filter)."""
    return sorted({c["empresa"] for c in accounts if c.get("empresa")})


def label(c: dict) -> str:
    base = f'{c["empresa"]} — {BANKS.get(c["banco"], c["banco"])} {c["numero"]} ({c["moneda"]})'
    # the alias is descriptive, not identifying (there are repeats): appended
    return f'{base} · {c["alias"]}' if c.get("alias") else base


def _segments(s: str) -> tuple:
    """Numeric segments as integers: '742-000483/5' -> (742, 483, 5).
    Ignores the padding zeros each bank adds differently."""
    return tuple(int(x) for x in re.findall(r'\d+', s or ''))


def find_by_number(accounts, bank, number, currency=None):
    """Matches the account found in a statement against the table: by digits
    without leading zeros, or by numeric segments ('742-483-5' matches
    '742-000483/5')."""
    if not number:
        return None
    d = _digits(number).lstrip('0')
    seg = _segments(number)
    for c in accounts:
        if bank and c["banco"] != bank:
            continue
        if currency and c["moneda"] != currency:
            continue
        cd = _digits(c["numero"]).lstrip('0')
        if cd == d or (len(d) >= 6 and (cd.endswith(d) or d.endswith(cd))):
            return c
        if len(seg) >= 2 and seg == _segments(c["numero"]):
            return c
    return None


def find_by_fbs(accounts, code):
    for c in accounts:
        if code and code in (c.get("fbs_e"), c.get("fbs_o")):
            return c
    return None


BANK_KEYWORDS = {"santander": ("SANTANDER", "RIO ALEM"), "frances": ("BBVA", "FRANCES"),
                 "galicia": ("GALICIA",), "macro": ("MACRO",), "ciudad": ("CIUDAD",)}


def find_by_fbs_name(accounts, name):
    """Some FBS carry the real account number in the internal name
    ('CIT - BANCO SANTANDER CC $ 742-000483/5'): bank by keyword, currency by
    the symbol, number by segments."""
    if not name:
        return None
    up = name.upper()
    bank = next((b for b, kws in BANK_KEYWORDS.items()
                 if any(k in up for k in kws)), None)
    currency = "USD" if re.search(r'U\$S|USD|DOLAR', up) else ("ARS" if "$" in up else None)
    m = re.search(r'\b(\d{3,4})[-. ](\d{1,6})[-/. ](\d)\b', name)
    if not m:
        return None
    return find_by_number(accounts, bank, "-".join(m.groups()), currency)


def map_fbs(account_id, sheet, code, fbs_name=None, path: str = DEFAULT_PATH):
    """Learns that the FBS code <code> (sheet E or O) is the account <account_id>."""
    accounts = load(path)
    for c in accounts:
        # a code belongs to a single account: clear old assignments
        for k in ("fbs_e", "fbs_o"):
            if c.get(k) == code and c["id"] != account_id:
                c[k] = None
    for c in accounts:
        if c["id"] == account_id:
            c["fbs_e" if sheet == "E" else "fbs_o"] = code
            if fbs_name:
                c["fbs_nombre"] = fbs_name
    save(accounts, path)
    return accounts
