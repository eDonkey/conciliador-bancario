# -*- coding: utf-8 -*-
"""Cuentas bancarias del grupo Nave y su mapeo contra el sistema FBS.

La tabla se siembra con las cuentas informadas por el cliente (agosto 2026) y
se persiste en datos/cuentas_nave.json. El número interno que usa el FBS no
coincide con el número de cuenta bancaria, así que el vínculo se hace por el
código de cuenta contable que viene en la hoja del reporte ("(E) (1141121)"):
la primera vez el usuario asigna el FBS a una cuenta y el mapeo queda
aprendido para siempre.
"""
import json
import os
import re

RUTA_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "datos", "cuentas_nave.json")

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


def cargar(ruta: str = RUTA_DEFAULT) -> list[dict]:
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


def guardar(cuentas: list[dict], ruta: str = RUTA_DEFAULT):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(cuentas, f, ensure_ascii=False, indent=1)


def etiqueta(c: dict) -> str:
    return f'{c["empresa"]} — {BANCOS.get(c["banco"], c["banco"])} {c["numero"]} ({c["moneda"]})'


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
