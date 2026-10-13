# -*- coding: utf-8 -*-
"""AI analysis of pending entries, confirmed by the user.

For an entry of "O pendientes sin banco" the AI explains why it was left
without a counterpart in the statement. Then the user ALWAYS confirms:
  - "Sí, está bien" -> the explanation becomes a rule: entries with the same
    signature are annotated automatically in future runs.
  - "No" + the client's correction -> also learned: the correction is saved,
    shown on similar entries and passed to the AI as context in the next
    analyses.
"""
import json
import os
from datetime import date

from engine import rules
from engine import ai_assist
from engine import presupuesto_ia

_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "datos")
DEFAULT_PATH = os.path.join(_DATA, "analisis_aprendidos.json")
CACHE_PATH = os.path.join(_DATA, "analisis_cache.json")
USAGE_PATH = os.path.join(_DATA, "analisis_uso.json")

# Haiku 4.5: $1/$5 per million tokens — an analysis costs ~$0.003.
MODEL = presupuesto_ia.modelo_analisis()   # IA_MODELO_ANALISIS (server only)
# cap of API calls per day (beyond it, internal analysis without AI)
MAX_CALLS_PER_DAY = int(os.environ.get("ANALISIS_MAX_DIA", "150"))
MAX_CACHE = 5000


def load(path: str = DEFAULT_PATH) -> list[dict]:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save(items: list[dict], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def learn(signature: str, ai_explanation: str, ok: bool, correction: str = "",
          path: str = DEFAULT_PATH) -> dict:
    """Records the user's verdict on an analysis."""
    items = load(path)
    verdict = "ok" if ok else "corregido"
    for it in items:
        if it["firma"] == signature:
            it["veredicto"] = verdict
            it["explicacion"] = ai_explanation[:600]
            if correction:
                it["correccion"] = correction.strip()[:600]
            it["veces"] = it.get("veces", 1) + 1
            it["actualizada"] = date.today().isoformat()
            _save(items, path)
            return it
    new = {"firma": signature, "veredicto": verdict,
           "explicacion": ai_explanation[:600],
           "correccion": (correction or "").strip()[:600],
           "veces": 1, "actualizada": date.today().isoformat()}
    items.append(new)
    _save(items, path)
    return new


def find(signature: str, items: list[dict]) -> dict | None:
    return next((it for it in items if it["firma"] == signature), None)


def annotate_residuals(data: dict, path: str = DEFAULT_PATH):
    """Flags the residual entries whose signature already has an analysis
    confirmed or corrected by the user (the analysis 'rule')."""
    items = load(path)
    if not items:
        return
    for list_name in ("e_sin_banco", "o_pendientes_sin_banco"):
        for x in data.get(list_name, []):
            signature = rules.ledger_signature(x.get("referencia"), x.get("comentario"))
            it = find(signature, items)
            if it:
                x["analisis_previo"] = {
                    "veredicto": it["veredicto"],
                    "nota": it.get("correccion") or it.get("explicacion", ""),
                }


SYSTEM = """Sos un analista contable experto en conciliaciones bancarias de
Argentina. Se te muestra UN asiento del libro mayor que quedó SIN contrapartida
en el extracto bancario después de la conciliación automática (la cuenta O son
órdenes de pago y recibos pendientes de confirmación; la E son confirmados).

Explicá en 2 a 4 frases, en español claro para un contador, por qué ese asiento
puede haber quedado pendiente: el pago todavía no salió del banco, salió con
otro importe o con comisión descontada, corresponde a otra cuenta bancaria, es
una anulación o duplicado, el banco lo agrupa con otros movimientos, etc.
Usá los movimientos residuales del extracto como evidencia: si ves un candidato
probable para el cruce, decilo explícitamente con su fecha e importe. Si el
cliente ya corrigió análisis de asientos parecidos, respetá ese criterio.
No inventes datos que no estén en el contexto."""


def _read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def entry_key(a: dict) -> str:
    return (f'{a.get("hoja")}|{a.get("asiento")}|{a.get("fecha")}|'
            f'{a.get("debe") or 0:.2f}|{a.get("haber") or 0:.2f}|'
            f'{(a.get("referencia") or "")[:30]}')


def _take_budget() -> bool:
    """True if there is still AI call budget left for today."""
    usage = _read_json(USAGE_PATH, {})
    today = date.today().isoformat()
    if usage.get("fecha") != today:
        usage = {"fecha": today, "llamadas": 0}
    if usage["llamadas"] >= MAX_CALLS_PER_DAY:
        return False
    usage["llamadas"] += 1
    _write_json(USAGE_PATH, usage)
    return True


def _internal(entry: dict, bank: list[dict], reason: str = "") -> str:
    """Deterministic analysis without AI: best effort with the local data."""
    amount = (entry.get("debe") or 0) or (entry.get("haber") or 0)
    bank_side = "credito" if (entry.get("debe") or 0) else "debito"
    parts = []
    if reason:
        parts.append(reason)
    parts.append(
        "Este asiento no encontró en el extracto ningún movimiento del mismo "
        f"importe (${amount:,.2f}) y lado dentro de la tolerancia de fechas.")
    candidates = sorted(
        (b for b in bank
         if (b.get("credito") if bank_side == "credito" else b.get("debito"))),
        key=lambda b: abs(((b.get("credito") or 0) or (b.get("debito") or 0)) - amount))[:2]
    for c in candidates:
        ic = (c.get("credito") or 0) or (c.get("debito") or 0)
        diff = ic - amount
        if abs(diff) < amount * 0.05 or abs(diff) < 5000:
            parts.append(
                f'El residual más parecido es "{c.get("descripcion")}" del '
                f'{c.get("fecha")} por ${ic:,.2f} (diferencia ${diff:+,.2f}): '
                "podría ser el mismo pago con comisión o redondeo — conviene "
                "revisarlo en la solapa de conciliación manual.")
            break
    else:
        parts.append(
            "Lo más probable es que el pago aún no se haya ejecutado en el "
            "banco, corresponda a otra cuenta, o el banco lo agrupe con otros "
            "movimientos.")
    return "Análisis interno (sin IA): " + " ".join(parts)


def _simulated(entry: dict, bank: list[dict]) -> str:
    amount = (entry.get("debe") or 0) or (entry.get("haber") or 0)
    cand = min(bank, key=lambda b: abs(((b.get("credito") or 0) or (b.get("debito") or 0)) - amount)) \
        if bank else None
    extra = ""
    if cand:
        ic = (cand.get("credito") or 0) or (cand.get("debito") or 0)
        extra = (f' El residual más cercano del banco es "{cand.get("descripcion")}" '
                 f'del {cand.get("fecha")} por $ {ic:,.2f}.')
    return ("SIMULACIÓN (sin IA): este asiento no encontró un movimiento del "
            "extracto con el mismo importe y lado dentro de la tolerancia de "
            "fechas; lo más probable es que el pago aún no se haya ejecutado "
            "en el banco o haya salido con otro importe." + extra)


def analyze_entry(entry: dict, bank_residual: list[dict],
                  corrections: list[dict], account: str = "",
                  simulate: bool = False) -> dict:
    """Explanation of why the entry was left pending.

    Cost cascade: persistent cache (same entry in previous runs, zero API)
    -> daily budget -> AI (Haiku, trimmed prompt) -> internal analysis
    without AI on any problem.
    Returns {"texto", "origen"} with origen in ia|cache|interno|simulado."""
    if simulate:
        return {"texto": _simulated(entry, bank_residual), "origen": "simulado"}

    key = entry_key(entry)
    cache = _read_json(CACHE_PATH, {})
    if key in cache:
        return {"texto": cache[key]["texto"], "origen": "cache"}

    if not ai_assist.available():
        return {"texto": _internal(entry, bank_residual,
                                   "No hay credenciales de IA configuradas."),
                "origen": "interno"}
    presupuesto_ia.verificar()          # daily token cap: PresupuestoExcedido (429)
    if not _take_budget():
        return {"texto": _internal(
            entry, bank_residual,
            f"Se alcanzó el tope diario de {MAX_CALLS_PER_DAY} análisis con IA "
            "(protección de costos)."), "origen": "interno"}

    try:
        import anthropic

        amount = (entry.get("debe") or 0) or (entry.get("haber") or 0)
        nearest = sorted(
            bank_residual,
            key=lambda b: abs(((b.get("credito") or 0) or (b.get("debito") or 0)) - amount))[:12]
        bank_rows = [{k: v for k, v in {
            "fecha": b.get("fecha"),
            "descripcion": (b.get("descripcion") or "")[:80],
            "detalle": (b.get("detalle") or "")[:60] or None,
            "comprobante": b.get("comprobante"),
            "credito": b.get("credito") or None,
            "debito": b.get("debito") or None,
        }.items() if v} for b in nearest]

        prompt = (
            (f"CUENTA: {account}\n" if account else "")
            + "ASIENTO PENDIENTE A EXPLICAR:\n"
            + json.dumps({k: entry.get(k) for k in
                          ("hoja", "asiento", "fecha", "referencia", "comentario",
                           "debe", "haber")}, ensure_ascii=False)
            + "\n\nRESIDUALES DEL EXTRACTO MÁS CERCANOS POR IMPORTE:\n"
            + json.dumps(bank_rows, ensure_ascii=False)
        )
        if corrections:
            prompt += ("\n\nCORRECCIONES PREVIAS DEL CLIENTE (criterio a respetar):\n"
                       + json.dumps([{"asiento_tipo": c["firma"],
                                      "correccion": (c.get("correccion") or "")[:200]}
                                     for c in corrections[:5]], ensure_ascii=False))

        ai_assist._load_key()
        client = anthropic.Anthropic(timeout=60, max_retries=2)
        r = client.messages.create(
            model=MODEL, max_tokens=400, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        from engine import ai_usage
        ai_usage.record("analisis", MODEL, getattr(r, "usage", None))
        text = next((b.text for b in r.content if b.type == "text"), "").strip()
        if not text:
            return {"texto": _internal(entry, bank_residual,
                                       "La IA no devolvió análisis."), "origen": "interno"}
        cache[key] = {"texto": text, "fecha": date.today().isoformat(),
                      "modelo": MODEL}
        if len(cache) > MAX_CACHE:      # cap the file size
            oldest = sorted(cache, key=lambda k: cache[k].get("fecha", ""))
            for k in oldest[:len(cache) - MAX_CACHE]:
                cache.pop(k, None)
        _write_json(CACHE_PATH, cache)
        return {"texto": text, "origen": "ia"}
    except presupuesto_ia.PresupuestoExcedido:
        raise                           # the endpoint answers 429
    except Exception as exc:  # noqa: BLE001 — never break: internal analysis
        return {"texto": _internal(entry, bank_residual,
                                   f"La IA no respondió ({exc})."),
                "origen": "interno"}
