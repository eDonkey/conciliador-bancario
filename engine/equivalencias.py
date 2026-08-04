# -*- coding: utf-8 -*-
"""Diccionario de equivalencias de vocabulario entre el extracto y el sistema.

El banco y el sistema contable suelen llamar distinto al mismo concepto:
"PAGO HABERES" en el extracto son los "SUELDOS" del mayor. El usuario carga
estos pares desde la interfaz y el motor los usa para conciliar movimientos
cuyo texto no coincide pero cuyo concepto sí (con importes que cierren).
También se le pasan a la IA como glosario.

Cada par es direccional: `extracto` debe aparecer en el texto del movimiento
bancario y `sistema` en el texto del asiento del mayor. `extracto` admite
VARIOS términos separados por coma ("compras en el exterior, compras tarjeta
debito, reintegro tarjeta" ≈ "gasto de representacion"): cualquier movimiento
que contenga alguno de ellos participa, y los grupos pueden mezclar términos.
"""
import json
import os
import re
import unicodedata
import uuid
from collections import defaultdict
from datetime import date

RUTA_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "datos", "equivalencias.json")

# Pares con los que arranca el diccionario la primera vez
SEMILLA = [{"extracto": "haberes", "sistema": "sueldos"}]


def _norm(texto: str) -> str:
    """Minúsculas y sin acentos, para comparar términos con tolerancia."""
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def contiene(texto: str, termino: str) -> bool:
    """El término aparece como palabra(s) dentro del texto (sin acentos)."""
    term = _norm(termino).strip()
    if not term:
        return False
    return re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', _norm(texto)) is not None


def cargar(ruta: str = RUTA_DEFAULT) -> list[dict]:
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    # primera vez: sembrar los pares por defecto para que el diccionario
    # nunca esté vacío en la demo
    pares = [dict(p, id=uuid.uuid4().hex[:8], origen="predefinida",
                  creada=date.today().isoformat()) for p in SEMILLA]
    _guardar(pares, ruta)
    return pares


def _guardar(pares: list[dict], ruta: str):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(pares, f, ensure_ascii=False, indent=1)


def terminos_extracto(par: dict) -> list[str]:
    """Términos del lado extracto (admite varios separados por coma)."""
    return [t.strip() for t in (par.get("extracto") or "").split(",") if t.strip()]


def agregar(extracto: str, sistema: str, ruta: str = RUTA_DEFAULT) -> tuple[list[dict], str | None]:
    """Agrega un par al diccionario. Devuelve (lista, error)."""
    partes = [t.strip() for t in (extracto or "").split(",") if t.strip()]
    extracto = ", ".join(partes)
    sistema = (sistema or "").strip()
    if not partes or any(len(t) < 3 for t in partes) or len(sistema) < 3:
        return cargar(ruta), "Cada término necesita al menos 3 caracteres"
    pares = cargar(ruta)
    if any(_norm(p["extracto"]) == _norm(extracto) and _norm(p["sistema"]) == _norm(sistema)
           for p in pares):
        return pares, "Esa equivalencia ya existe"
    pares.append({"id": uuid.uuid4().hex[:8], "extracto": extracto, "sistema": sistema,
                  "origen": "manual", "creada": date.today().isoformat()})
    _guardar(pares, ruta)
    return pares, None


def eliminar(eq_id: str, ruta: str = RUTA_DEFAULT) -> list[dict]:
    pares = [p for p in cargar(ruta) if p["id"] != eq_id]
    _guardar(pares, ruta)
    return pares


def _texto_banco(m) -> str:
    return f"{m.comprobante or ''} {m.descripcion} {m.detalle}"


def _texto_mayor(a) -> str:
    return f"{a.referencia} {a.comentario}"


def aplicar(banco_libres, asiento_libres, pares):
    """Concilia residuales usando las equivalencias de términos.

    Igual espíritu que reglas.aplicar: 1 a 1 por importe exacto (la
    equivalencia es el desempate, sin límite de fecha) y grupos por día cuya
    suma cierra. Devuelve (matches, ids_banco_usados, ids_mayor_usados)."""
    if not pares:
        return [], set(), set()

    matches = []
    usados_b, usados_a = set(), set()

    # a qué pares responde cada movimiento/asiento (extracto admite varios términos)
    pares_b = {m.id: [p for p in pares
                      if any(contiene(_texto_banco(m), t) for t in terminos_extracto(p))]
               for m in banco_libres}
    pares_a = {a.id: {p["id"] for p in pares if contiene(_texto_mayor(a), p["sistema"])}
               for a in asiento_libres}

    def etiqueta(p, grupo=None):
        terms = terminos_extracto(p)
        lado_b = terms[0] + ", …" if len(terms) > 1 else terms[0]
        base = f'equivalencia: {lado_b} ≈ {p["sistema"]}'
        return base + (f" (grupo {grupo})" if grupo else "")

    # --- 1 a 1: mismo importe/lado + equivalencia --------------------------
    idx = defaultdict(list)
    for a in asiento_libres:
        lado_banco = 'credito' if a.lado == 'debe' else 'debito'
        idx[(lado_banco, round(a.importe, 2))].append(a)
    for m in banco_libres:
        for p in pares_b[m.id]:
            candidato = next((a for a in idx.get((m.lado, round(m.importe, 2)), [])
                              if a.id not in usados_a and p["id"] in pares_a[a.id]), None)
            if candidato is not None:
                matches.append({"banco": m, "asiento": candidato, "metodo": etiqueta(p)})
                usados_b.add(m.id)
                usados_a.add(candidato.id)
                break

    # --- grupos: N banco -> 1 asiento, primero por día y después por mes ---
    # (los resúmenes tipo "gasto de representación" agrupan un mes entero de
    # compras de tarjeta en un solo asiento)
    for nombre_ventana, clave_fecha in (("día", lambda f: f),
                                        ("mes", lambda f: (f.year, f.month))):
        grupos_b = defaultdict(list)
        for m in banco_libres:
            if m.id in usados_b or m.fecha is None:
                continue
            for p in pares_b[m.id]:
                grupos_b[(p["id"], clave_fecha(m.fecha))].append((p, m))
        for (pid, _), items in grupos_b.items():
            if len(items) < 2:
                continue
            p = items[0][0]
            movs = [m for _, m in items]
            if any(m.id in usados_b for m in movs):
                continue
            neto = round(sum(x.credito - x.debito for x in movs), 2)
            for a in asiento_libres:
                if a.id in usados_a or pid not in pares_a[a.id]:
                    continue
                neto_a = round(a.debe - a.haber, 2)
                if abs(neto - neto_a) < 0.01 and neto != 0:
                    for x in movs:
                        matches.append({"banco": x, "asiento": a,
                                        "metodo": etiqueta(p, grupo=nombre_ventana)})
                        usados_b.add(x.id)
                    usados_a.add(a.id)
                    break

    # --- grupos por día: 1 banco -> N asientos ------------------------------
    grupos_a = defaultdict(list)
    for a in asiento_libres:
        if a.id in usados_a:
            continue
        for pid in pares_a[a.id]:
            grupos_a[(pid, a.fecha)].append(a)
    for (pid, fecha), items in grupos_a.items():
        if len(items) < 2 or fecha is None:
            continue
        if any(a.id in usados_a for a in items):
            continue
        p = next(x for x in pares if x["id"] == pid)
        neto = round(sum(x.debe - x.haber for x in items), 2)
        for m in banco_libres:
            if m.id in usados_b or not any(x["id"] == pid for x in pares_b[m.id]):
                continue
            neto_b = round(m.credito - m.debito, 2)
            if abs(neto - neto_b) < 0.01 and neto != 0:
                for x in items:
                    matches.append({"banco": m, "asiento": x, "metodo": etiqueta(p, grupo="día")})
                    usados_a.add(x.id)
                usados_b.add(m.id)
                break

    return matches, usados_b, usados_a
