# -*- coding: utf-8 -*-
"""Reglas aprendidas de las conciliaciones manuales.

Cada vez que el usuario concilia a mano (o acepta una sugerencia de la IA), se
extrae una regla generalizable: la "firma" del concepto bancario y la del
asiento del mayor. En conciliaciones futuras, los pares que cumplan una regla
(y cuyos importes cierren) se concilian automáticamente con método
"regla aprendida".

Tipos de regla:
  - "1a1":   un movimiento del banco ↔ un asiento (mismo importe, sin límite
             de fecha ni desempate: la regla es el desempate).
  - "grupo": N movimientos del mismo concepto y día ↔ 1 asiento (o al revés),
             cuando la suma cierra.
"""
import json
import os
import re
from datetime import date

RUTA_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "datos", "reglas_aprendidas.json")

STOP = {
    "santander", "santanter", "cuenta", "varios", "cuit", "para", "por",
    "banco", "pesos", "nota", "segun", "sobre", "desde", "hasta",
}


def firma_banco(descripcion: str) -> str:
    """Concepto bancario normalizado: sin números ni signos, minúsculas."""
    t = re.sub(r'[\d$.,/%-]+', ' ', (descripcion or '').lower())
    return ' '.join(t.split())


def firma_mayor(referencia: str, comentario: str) -> str:
    """Firma del asiento: prefijo alfabético de la referencia + palabras
    distintivas del comentario."""
    ref = re.sub(r'[^A-Za-z]', '', referencia or '').upper()
    tokens = [w for w in re.findall(r'[a-záéíóúñ]{4,}', (comentario or '').lower())
              if w not in STOP]
    return ref + '|' + ' '.join(sorted(set(tokens))[:4])


def cargar(ruta: str = RUTA_DEFAULT) -> list[dict]:
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _guardar(reglas: list[dict], ruta: str):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(reglas, f, ensure_ascii=False, indent=1)


def _derivar(match: dict) -> dict | None:
    """Deriva la regla de un match manual serializado ({'banco': [...dicts],
    'mayor': [...dicts]}). Devuelve None si no se puede generalizar."""
    firmas_b = sorted({firma_banco(b["descripcion"]) for b in match["banco"]})
    firmas_m = sorted({firma_mayor(a["referencia"], a["comentario"]) for a in match["mayor"]})
    if not firmas_b or not firmas_m:
        return None
    # solo se generaliza si cada lado tiene un único concepto
    if len(firmas_b) != 1 or len(firmas_m) != 1:
        return None
    tipo = "1a1" if len(match["banco"]) == 1 and len(match["mayor"]) == 1 else "grupo"
    return {"tipo": tipo, "banco": firmas_b[0], "mayor": firmas_m[0]}


def aprender(match: dict, ruta: str = RUTA_DEFAULT) -> dict | None:
    """Registra (o refuerza) la regla derivada de un match manual."""
    nueva = _derivar(match)
    if nueva is None:
        return None
    reglas = cargar(ruta)
    for r in reglas:
        if (r["tipo"], r["banco"], r["mayor"]) == (nueva["tipo"], nueva["banco"], nueva["mayor"]):
            r["veces"] = r.get("veces", 1) + 1
            r["actualizada"] = date.today().isoformat()
            _guardar(reglas, ruta)
            return r
    nueva["veces"] = 1
    nueva["actualizada"] = date.today().isoformat()
    reglas.append(nueva)
    _guardar(reglas, ruta)
    return nueva


def olvidar(match: dict, ruta: str = RUTA_DEFAULT):
    """Al deshacer un match manual, debilita/elimina la regla asociada."""
    regla = _derivar(match)
    if regla is None:
        return
    reglas = cargar(ruta)
    for r in list(reglas):
        if (r["tipo"], r["banco"], r["mayor"]) == (regla["tipo"], regla["banco"], regla["mayor"]):
            r["veces"] = r.get("veces", 1) - 1
            if r["veces"] <= 0:
                reglas.remove(r)
            _guardar(reglas, ruta)
            return


def aplicar(banco_libres, asiento_libres, reglas):
    """Aplica las reglas aprendidas sobre los residuales.

    Devuelve (pares, ids_banco_usados, ids_mayor_usados) donde pares es una
    lista de dicts {banco, asiento, metodo} (los grupos generan un par por
    movimiento, compartiendo el asiento)."""
    if not reglas:
        return [], set(), set()

    reglas_1a1 = {(r["banco"], r["mayor"]) for r in reglas if r["tipo"] == "1a1"}
    reglas_grupo = {(r["banco"], r["mayor"]) for r in reglas if r["tipo"] == "grupo"}
    # una regla 1a1 también habilita el emparejamiento dentro de un grupo trivial
    reglas_todas = reglas_1a1 | reglas_grupo

    pares = []
    usados_b, usados_a = set(), set()

    fb = {m.id: firma_banco(m.descripcion) for m in banco_libres}
    fa = {a.id: firma_mayor(a.referencia, a.comentario) for a in asiento_libres}

    # --- 1 a 1: mismo importe/lado + firma de la regla --------------------
    from collections import defaultdict
    idx = defaultdict(list)
    for a in asiento_libres:
        lado_banco = 'credito' if a.lado == 'debe' else 'debito'
        idx[(lado_banco, round(a.importe, 2))].append(a)
    for m in banco_libres:
        for a in idx.get((m.lado, round(m.importe, 2)), []):
            if a.id in usados_a:
                continue
            if (fb[m.id], fa[a.id]) in reglas_todas:
                pares.append({"banco": m, "asiento": a, "metodo": "regla aprendida"})
                usados_b.add(m.id)
                usados_a.add(a.id)
                break

    # --- grupos por día: N banco -> 1 asiento ------------------------------
    grupos_b = defaultdict(list)
    for m in banco_libres:
        if m.id not in usados_b:
            grupos_b[(fb[m.id], m.fecha)].append(m)
    for (firma_b, fecha), items in grupos_b.items():
        if len(items) < 2 or fecha is None:
            continue
        neto = round(sum(x.credito - x.debito for x in items), 2)
        for a in asiento_libres:
            if a.id in usados_a or (firma_b, fa[a.id]) not in reglas_grupo:
                continue
            neto_a = round(a.debe - a.haber, 2)
            if abs(neto - neto_a) < 0.01 and neto != 0:
                for x in items:
                    pares.append({"banco": x, "asiento": a,
                                  "metodo": "regla aprendida (grupo)"})
                    usados_b.add(x.id)
                usados_a.add(a.id)
                break

    # --- grupos por día: 1 banco -> N asientos ------------------------------
    grupos_a = defaultdict(list)
    for a in asiento_libres:
        if a.id not in usados_a:
            grupos_a[(fa[a.id], a.fecha)].append(a)
    for (firma_a, fecha), items in grupos_a.items():
        if len(items) < 2 or fecha is None:
            continue
        neto = round(sum(x.debe - x.haber for x in items), 2)
        for m in banco_libres:
            if m.id in usados_b or (fb[m.id], firma_a) not in reglas_grupo:
                continue
            neto_b = round(m.credito - m.debito, 2)
            if abs(neto - neto_b) < 0.01 and neto != 0:
                for x in items:
                    pares.append({"banco": m, "asiento": x,
                                  "metodo": "regla aprendida (grupo)"})
                    usados_a.add(x.id)
                usados_b.add(m.id)
                break

    return pares, usados_b, usados_a
