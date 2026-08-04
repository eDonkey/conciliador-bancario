# -*- coding: utf-8 -*-
"""Gastos bancarios definidos por el usuario.

El matcher trae de fábrica un patrón de gastos/impuestos bancarios (GASTO_RE),
pero aparecen conceptos nuevos ("Percep perc rg 5617 30%") que el usuario debe
poder incorporar desde la app sin tocar código. Cada término se guarda sin
números ni signos (así "rg 5617 30%" generaliza a cualquier alícuota futura) y
se aplica en todas las conciliaciones siguientes.
"""
import json
import os
import re
import uuid
from datetime import date

from engine.equivalencias import contiene, _norm

RUTA_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "datos", "gastos_usuario.json")


def limpiar_termino(texto: str) -> str:
    """Concepto sin números/signos: 'Percep perc rg 5617 30%' -> 'percep perc rg'."""
    t = re.sub(r'[\d$.,/%-]+', ' ', (texto or '').lower())
    return ' '.join(t.split())


def cargar(ruta: str = RUTA_DEFAULT) -> list[dict]:
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _guardar(terminos: list[dict], ruta: str):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(terminos, f, ensure_ascii=False, indent=1)


def agregar(texto: str, ruta: str = RUTA_DEFAULT) -> tuple[list[dict], str | None]:
    termino = limpiar_termino(texto)
    if len(termino) < 4:
        return cargar(ruta), "El concepto necesita al menos 4 caracteres (sin contar números)"
    terminos = cargar(ruta)
    if any(_norm(t["termino"]) == _norm(termino) for t in terminos):
        return terminos, "Ese concepto ya está registrado como gasto bancario"
    terminos.append({"id": uuid.uuid4().hex[:8], "termino": termino,
                     "creada": date.today().isoformat()})
    _guardar(terminos, ruta)
    return terminos, None


def eliminar(term_id: str, ruta: str = RUTA_DEFAULT) -> list[dict]:
    terminos = [t for t in cargar(ruta) if t["id"] != term_id]
    _guardar(terminos, ruta)
    return terminos


def es_gasto(descripcion: str, terminos: list[dict]) -> bool:
    return any(contiene(descripcion, t["termino"]) for t in terminos or [])
