# -*- coding: utf-8 -*-
"""Log of EVERY call the app makes to the Anthropic API, with the real tokens
and an estimated cost. It is there to audit the spend: if the bill goes up and
this log doesn't show it, the usage came from somewhere else.
Read through GET /api/diagnostico (field ia_uso)."""
import json
import os
import time
from datetime import date

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "datos", "ia_uso_log.json")
MAX_ENTRIES = 2000

# USD per million tokens (input, output)
PRICES = {"claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
          "claude-opus-5": (5.0, 25.0)}


def _read() -> list[dict]:
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def record(module: str, model: str, usage) -> None:
    """Records one call. `usage` is the SDK's response.usage (or None)."""
    try:
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        p_in, p_out = PRICES.get(model, (5.0, 25.0))   # worst case if it's another one
        from engine import presupuesto_ia
        presupuesto_ia.registrar(tokens_in, tokens_out)
        entries = _read()
        entries.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "modulo": module, "modelo": model,
            "tokens_entrada": tokens_in, "tokens_salida": tokens_out,
            "usd": round((tokens_in * p_in + tokens_out * p_out) / 1_000_000, 5),
        })
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(entries[-MAX_ENTRIES:], f, ensure_ascii=False)
    except Exception:   # noqa: BLE001 — the log never breaks a successful call
        pass


def summary(days: int = 7) -> dict:
    """Spend per day (the last `days`) and the latest calls, for diagnostics."""
    entries = _read()
    per_day: dict[str, dict] = {}
    for e in entries:
        d = (e.get("ts") or "")[:10]
        agg = per_day.setdefault(d, {"llamadas": 0, "usd": 0.0})
        agg["llamadas"] += 1
        agg["usd"] = round(agg["usd"] + (e.get("usd") or 0), 4)
    ordered_days = sorted(per_day)[-days:]
    return {"hoy": per_day.get(date.today().isoformat(), {"llamadas": 0, "usd": 0.0}),
            "por_dia": {d: per_day[d] for d in ordered_days},
            "ultimas": entries[-10:]}
