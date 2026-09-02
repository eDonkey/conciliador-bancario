# -*- coding: utf-8 -*-
"""Registro de TODAS las llamadas a la API de Anthropic que hace la app,
con tokens reales y costo estimado. Sirve para auditar el gasto: si la
factura sube y este log no lo refleja, el consumo vino de otro lado.
Se consulta en GET /api/diagnostico (campo ia_uso)."""
import json
import os
import time
from datetime import date

RUTA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "datos", "ia_uso_log.json")
MAX_ENTRADAS = 2000

# USD por millón de tokens (entrada, salida)
PRECIOS = {"claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
           "claude-opus-5": (5.0, 25.0)}


def _leer() -> list[dict]:
    if os.path.exists(RUTA):
        try:
            with open(RUTA, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def registrar(modulo: str, modelo: str, usage) -> None:
    """Anota una llamada. `usage` es response.usage del SDK (o None)."""
    try:
        ent = getattr(usage, "input_tokens", 0) or 0
        sal = getattr(usage, "output_tokens", 0) or 0
        p_in, p_out = PRECIOS.get(modelo, (5.0, 25.0))   # peor caso si es otro
        entradas = _leer()
        entradas.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "modulo": modulo, "modelo": modelo,
            "tokens_entrada": ent, "tokens_salida": sal,
            "usd": round((ent * p_in + sal * p_out) / 1_000_000, 5),
        })
        os.makedirs(os.path.dirname(RUTA), exist_ok=True)
        with open(RUTA, "w", encoding="utf-8") as f:
            json.dump(entradas[-MAX_ENTRADAS:], f, ensure_ascii=False)
    except Exception:   # noqa: BLE001 — el log nunca rompe una llamada exitosa
        pass


def resumen(dias: int = 7) -> dict:
    """Gasto por día (últimos `dias`) y últimas llamadas, para diagnóstico."""
    entradas = _leer()
    por_dia: dict[str, dict] = {}
    for e in entradas:
        d = (e.get("ts") or "")[:10]
        agg = por_dia.setdefault(d, {"llamadas": 0, "usd": 0.0})
        agg["llamadas"] += 1
        agg["usd"] = round(agg["usd"] + (e.get("usd") or 0), 4)
    dias_orden = sorted(por_dia)[-dias:]
    return {"hoy": por_dia.get(date.today().isoformat(), {"llamadas": 0, "usd": 0.0}),
            "por_dia": {d: por_dia[d] for d in dias_orden},
            "ultimas": entradas[-10:]}
