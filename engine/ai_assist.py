# -*- coding: utf-8 -*-
"""AI assistance (Claude) for the cases the deterministic reconciliation
couldn't solve: amount differences (deducted commissions), groupings (several
bank movements = one entry, or the other way around) and matches by
description/payee.

Needs ANTHROPIC_API_KEY in the environment (or an `ant auth login` session).
"""
import json
import os

from engine import presupuesto_ia

# Sonnet 5 ($2/$10 per MTok, ~60% cheaper than Opus) is more than enough to
# propose pairings; each batch costs ~$0.05-0.10. The model is set ONLY in the
# server configuration (IA_MODELO), never from the browser.
MODEL = presupuesto_ia.modelo_matching()
MAX_BANK = 80      # per-call limits so the context isn't exceeded
MAX_LEDGER = 400

KEY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "datos", "anthropic_key.txt")


def _load_key() -> bool:
    """Looks for credentials: environment variable or datos/anthropic_key.txt."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if os.path.exists(KEY_FILE):
        try:
            key = open(KEY_FILE, encoding="utf-8").read().strip()
        except OSError:
            return False
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
            return True
    return False

SCHEMA = {
    "type": "object",
    "properties": {
        "sugerencias": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ids_banco": {"type": "array", "items": {"type": "string"}},
                    "ids_mayor": {"type": "array", "items": {"type": "string"}},
                    "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
                    "motivo": {"type": "string"},
                },
                "required": ["ids_banco", "ids_mayor", "confianza", "motivo"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sugerencias"],
    "additionalProperties": False,
}

SYSTEM = """Sos un experto en conciliaciones bancarias de Argentina.
Recibís movimientos de un extracto de Banco Santander que no pudieron conciliarse
automáticamente, y asientos del libro mayor (cuenta E = confirmados, cuenta O =
pendientes de confirmación) que tampoco tienen contrapartida.

Convención: un crédito del banco corresponde a un Debe del mayor; un débito del
banco corresponde a un Haber del mayor.

Proponé emparejamientos probables considerando:
- combinaciones 1 a N o N a 1 cuya suma coincida (o difiera por una comisión chica),
- coincidencias de beneficiario/CUIT/número de comprobante en los textos,
- proximidad de fechas.
Solo proponé pares donde tengas un argumento concreto; no fuerces coincidencias.
Indicá confianza "alta" solo si los importes cierran exactamente o hay un número
de referencia compartido."""


def available() -> bool:
    if _load_key():
        return True
    # `ant auth login` profile
    cfg = os.path.join(os.path.expanduser("~"), ".config", "anthropic")
    appdata = os.path.join(os.environ.get("APPDATA", ""), "Anthropic")
    return os.path.isdir(cfg) or os.path.isdir(appdata)


def _bank_row(m):
    return {
        "id": m.id, "fecha": m.date.isoformat() if m.date else None,
        "lado": m.side, "importe": m.amount,
        "texto": f"{m.voucher or ''} {m.description} {m.detail}".strip(),
    }


def _ledger_row(a):
    return {
        "id": a.id, "hoja": a.sheet,
        "fecha": a.date.isoformat() if a.date else None,
        "lado": a.side, "importe": a.amount,
        "texto": f"{a.reference} {a.comment}".strip(),
    }


def batch_count(bank_movs) -> int:
    return max(1, -(-len(bank_movs) // MAX_BANK))  # ceil


def _glossary_block(glossary) -> str:
    """Text of the equivalences dictionary to include in the prompt."""
    if not glossary:
        return ""
    lines = "\n".join(f'- "{p["extracto"]}" (extracto) ≈ "{p["sistema"]}" (sistema)'
                      for p in glossary)
    return ("\n\nGLOSARIO DE EQUIVALENCIAS definido por el cliente — el mismo "
            "concepto tiene distinto nombre en el extracto y en el sistema "
            "contable; usalo para vincular movimientos:\n" + lines)


def simulate_suggestions(bank_movs, ledger_entries, progress=None, glossary=None):
    """Simulation mode: produces test suggestions WITHOUT calling the API.
    Emulates the per-batch latency so the progress UI can be tested."""
    import time

    if not bank_movs or not ledger_entries:
        return []
    batches = min(3, batch_count(bank_movs))
    for t in range(batches):
        if progress:
            progress(t + 1, batches)
        time.sleep(4)

    suggestions = []
    confidences = ["alta", "media", "baja"]
    used = set()
    for i, m in enumerate(bank_movs[:12]):
        candidate = None
        for a in ledger_entries:
            if a.id in used:
                continue
            side_ok = (m.side == "credito") == (a.side == "debe")
            if side_ok:
                if candidate is None or abs(a.amount - m.amount) < abs(candidate.amount - m.amount):
                    candidate = a
        if candidate is None:
            continue
        used.add(candidate.id)
        suggestions.append({
            "ids_banco": [m.id],
            "ids_mayor": [candidate.id],
            "confianza": confidences[i % 3],
            "motivo": f"SIMULACIÓN (sin IA): par de prueba por cercanía de importe "
                      f"(${m.amount:,.2f} vs ${candidate.amount:,.2f}).",
        })
    return suggestions


def suggest_matches(bank_movs, ledger_entries, progress=None, glossary=None):
    """Asks Claude for reconciliation suggestions. Returns a list of dicts.
    `progress(batch, total)` is called when each batch starts."""
    import anthropic

    if not bank_movs or not ledger_entries:
        return []

    _load_key()
    client = anthropic.Anthropic(timeout=120, max_retries=2)   # cap per batch
    suggestions = []
    total_batches = batch_count(bank_movs)

    for i in range(0, len(bank_movs), MAX_BANK):
        if progress:
            progress(i // MAX_BANK + 1, total_batches)
        presupuesto_ia.verificar()      # daily cap: PresupuestoExcedido
        bank_batch = bank_movs[i:i + MAX_BANK]
        ledger_batch = ledger_entries[:MAX_LEDGER]
        prompt = (
            "MOVIMIENTOS DEL BANCO SIN CONCILIAR:\n"
            + json.dumps([_bank_row(m) for m in bank_batch], ensure_ascii=False)
            + "\n\nASIENTOS DEL MAYOR SIN CONTRAPARTIDA:\n"
            + json.dumps([_ledger_row(a) for a in ledger_batch], ensure_ascii=False)
            + _glossary_block(glossary)
        )
        try:
            # With the server-side fallback: if a safety classifier rejects the
            # request, the API retries it with another model.
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=8000,
                betas=["server-side-fallback-2026-07-01"],
                system=SYSTEM,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": prompt}],
                extra_body={"fallbacks": "default"},
            )
        except anthropic.BadRequestError:
            # SDK/organization without the fallbacks beta: a normal call
            response = client.messages.create(
                model=MODEL,
                max_tokens=8000,
                system=SYSTEM,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": prompt}],
            )
        from engine import ai_usage
        ai_usage.record("matching", MODEL, getattr(response, "usage", None))
        if response.stop_reason == "refusal":
            continue
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
            suggestions.extend(data.get("sugerencias", []))
        except (json.JSONDecodeError, AttributeError):
            continue

    return suggestions
