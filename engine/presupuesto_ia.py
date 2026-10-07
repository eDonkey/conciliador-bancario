# -*- coding: utf-8 -*-
"""Presupuesto diario de tokens de IA.

Un error o un abuso no puede generar una factura grande: cada día hay un tope
de tokens (entrada + salida) para todas las llamadas a la API de Anthropic
de esta instalación.

* IA_TOKENS_DIA        tope diario de tokens (por defecto 2.000.000; 0 = IA
                       deshabilitada; el tope no se puede apagar).
* IA_AVISO_PORCENTAJE  % del tope en el que se avisa (por defecto 80).
* IA_MODELO            modelo del matching; IA_MODELO_ANALISIS el del análisis
                       de asientos. El modelo se define SOLO acá, en la
                       configuración del servidor: ningún pedido del navegador
                       lo puede cambiar.

El consumo del día vive en datos/ia_presupuesto.json. El aviso del 80 % se
escribe en el log una vez por día y se expone en /api/diagnostico (campo
ia_presupuesto); al pasar el tope, verificar() lanza PresupuestoExcedido y los
endpoints responden 429.
"""
import json
import os
import threading
from datetime import date

RUTA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "datos", "ia_presupuesto.json")
TOPE_POR_DEFECTO = 2_000_000
MODELO_MATCHING_DEFECTO = "claude-sonnet-5"
MODELO_ANALISIS_DEFECTO = "claude-haiku-4-5"

_lock = threading.Lock()


class PresupuestoExcedido(Exception):
    """Se alcanzó el tope diario de tokens de IA."""


def modelo_matching() -> str:
    return os.environ.get("IA_MODELO", "").strip() or MODELO_MATCHING_DEFECTO


def modelo_analisis() -> str:
    return os.environ.get("IA_MODELO_ANALISIS", "").strip() or MODELO_ANALISIS_DEFECTO


def tope() -> int:
    try:
        return max(0, int(float(os.environ.get("IA_TOKENS_DIA", "") or TOPE_POR_DEFECTO)))
    except ValueError:
        return TOPE_POR_DEFECTO


def _umbral_aviso() -> float:
    try:
        return min(100.0, max(1.0, float(os.environ.get("IA_AVISO_PORCENTAJE", "") or 80))) / 100
    except ValueError:
        return 0.8


def _leer(hoy: str) -> dict:
    try:
        with open(RUTA, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("fecha") == hoy:
            return d
    except (OSError, ValueError):
        pass
    return {"fecha": hoy, "tokens": 0, "avisado": False}


def _escribir(d: dict):
    os.makedirs(os.path.dirname(RUTA), exist_ok=True)
    tmp = RUTA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, RUTA)


def estado(hoy: str | None = None) -> dict:
    hoy = hoy or date.today().isoformat()
    with _lock:
        d = _leer(hoy)
    t = tope()
    return {"fecha": hoy, "tokens_usados": d["tokens"], "tope": t,
            "porcentaje": round(100 * d["tokens"] / t, 1) if t else 100.0,
            "aviso": bool(t) and d["tokens"] >= t * _umbral_aviso(),
            "excedido": d["tokens"] >= t}


def verificar(hoy: str | None = None):
    """Llamar ANTES de cada llamada a la API. Lanza PresupuestoExcedido."""
    e = estado(hoy)
    if e["excedido"]:
        raise PresupuestoExcedido(
            f"Se alcanzó el tope diario de IA ({e['tope']:,} tokens). "
            "Se habilita de nuevo mañana; la conciliación sin IA sigue disponible."
            .replace(",", "."))


def registrar(tokens_entrada: int, tokens_salida: int, hoy: str | None = None) -> dict:
    """Suma el consumo real de una llamada. Al cruzar el umbral avisa (una
    vez por día) en el log del servidor."""
    hoy = hoy or date.today().isoformat()
    total = int(tokens_entrada or 0) + int(tokens_salida or 0)
    with _lock:
        d = _leer(hoy)
        d["tokens"] += total
        t = tope()
        if t and not d.get("avisado") and d["tokens"] >= t * _umbral_aviso():
            d["avisado"] = True
            print(f"[ia] AVISO: el consumo de IA de hoy llegó a {d['tokens']:,} de "
                  f"{t:,} tokens ({100 * d['tokens'] / t:.0f} %).".replace(",", "."),
                  flush=True)
        try:
            _escribir(d)
        except OSError:
            pass
    return estado(hoy)
