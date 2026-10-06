# -*- coding: utf-8 -*-
"""Dictionary of vocabulary equivalences between the statement and the system.

The bank and the accounting system often name the same concept differently:
"PAGO HABERES" in the statement is "SUELDOS" in the ledger. The user loads
these pairs from the UI and the engine uses them to match movements whose
text doesn't coincide but whose concept does (with amounts that add up).
They are also passed to the AI as a glossary.

Each pair is directional: `extracto` must appear in the bank movement's text
and `sistema` in the ledger entry's text. `extracto` accepts SEVERAL terms
separated by commas ("compras en el exterior, compras tarjeta debito,
reintegro tarjeta" ≈ "gasto de representacion"): any movement containing one
of them takes part, and groups can mix terms.
"""
import json
import os
import re
import unicodedata
import uuid
from collections import defaultdict
from datetime import date

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "datos", "equivalencias.json")

# Pairs the dictionary starts with the first time
SEED = [{"extracto": "haberes", "sistema": "sueldos"}]


def _normalize(text: str) -> str:
    """Lowercase and without accents, to compare terms loosely."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def contains(text: str, term: str) -> bool:
    """The term appears as whole word(s) inside the text (accents ignored)."""
    needle = _normalize(term).strip()
    if not needle:
        return False
    return re.search(r'(?<!\w)' + re.escape(needle) + r'(?!\w)', _normalize(text)) is not None


def load(path: str = DEFAULT_PATH) -> list[dict]:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    # first time: seed the default pairs so the dictionary is never empty
    # in the demo
    pairs = [dict(p, id=uuid.uuid4().hex[:8], origen="predefinida",
                  creada=date.today().isoformat()) for p in SEED]
    _save(pairs, path)
    return pairs


def _save(pairs: list[dict], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pairs, f, ensure_ascii=False, indent=1)


def statement_terms(pair: dict) -> list[str]:
    """Terms of the statement side (several, separated by commas)."""
    return [t.strip() for t in (pair.get("extracto") or "").split(",") if t.strip()]


def add(statement: str, system: str, path: str = DEFAULT_PATH) -> tuple[list[dict], str | None]:
    """Adds a pair to the dictionary. Returns (list, error)."""
    parts = [t.strip() for t in (statement or "").split(",") if t.strip()]
    statement = ", ".join(parts)
    system = (system or "").strip()
    if not parts or any(len(t) < 3 for t in parts) or len(system) < 3:
        return load(path), "Cada término necesita al menos 3 caracteres"
    pairs = load(path)
    if any(_normalize(p["extracto"]) == _normalize(statement) and _normalize(p["sistema"]) == _normalize(system)
           for p in pairs):
        return pairs, "Esa equivalencia ya existe"
    pairs.append({"id": uuid.uuid4().hex[:8], "extracto": statement, "sistema": system,
                  "origen": "manual", "creada": date.today().isoformat()})
    _save(pairs, path)
    return pairs, None


def remove(eq_id: str, path: str = DEFAULT_PATH) -> list[dict]:
    pairs = [p for p in load(path) if p["id"] != eq_id]
    _save(pairs, path)
    return pairs


def _bank_text(m) -> str:
    return f"{m.voucher or ''} {m.description} {m.detail}"


def _ledger_text(a) -> str:
    return f"{a.reference} {a.comment}"


def apply(free_bank, free_entries, pairs):
    """Matches residuals using the term equivalences.

    Same spirit as rules.apply: 1 to 1 by exact amount (the equivalence is
    the tie-breaker, no date limit) and groups per day whose sum adds up.
    Returns (matches, used_bank_ids, used_ledger_ids)."""
    if not pairs:
        return [], set(), set()

    matches = []
    used_b, used_a = set(), set()

    # which pairs each movement/entry answers to (the statement side accepts several terms)
    pairs_b = {m.id: [p for p in pairs
                      if any(contains(_bank_text(m), t) for t in statement_terms(p))]
               for m in free_bank}
    pairs_a = {a.id: {p["id"] for p in pairs if contains(_ledger_text(a), p["sistema"])}
               for a in free_entries}

    def label(p, group=None):
        terms = statement_terms(p)
        bank_side = terms[0] + ", …" if len(terms) > 1 else terms[0]
        base = f'equivalencia: {bank_side} ≈ {p["sistema"]}'
        return base + (f" (grupo {group})" if group else "")

    # --- 1 to 1: same amount/side + equivalence ------------------------------
    idx = defaultdict(list)
    for a in free_entries:
        bank_side = 'credito' if a.side == 'debe' else 'debito'
        idx[(bank_side, round(a.amount, 2))].append(a)
    for m in free_bank:
        for p in pairs_b[m.id]:
            candidate = next((a for a in idx.get((m.side, round(m.amount, 2)), [])
                              if a.id not in used_a and p["id"] in pairs_a[a.id]), None)
            if candidate is not None:
                matches.append({"banco": m, "asiento": candidate, "metodo": label(p)})
                used_b.add(m.id)
                used_a.add(candidate.id)
                break

    # --- groups: N bank -> 1 entry, first per day and then per month ---------
    # (summaries like "gasto de representación" group a whole month of card
    # purchases into a single entry)
    for window_name, date_key in (("día", lambda f: f),
                                  ("mes", lambda f: (f.year, f.month))):
        groups_b = defaultdict(list)
        for m in free_bank:
            if m.id in used_b or m.date is None:
                continue
            for p in pairs_b[m.id]:
                groups_b[(p["id"], date_key(m.date))].append((p, m))
        for (pid, _), items in groups_b.items():
            if len(items) < 2:
                continue
            p = items[0][0]
            movs = [m for _, m in items]
            if any(m.id in used_b for m in movs):
                continue
            net = round(sum(x.credit - x.debit for x in movs), 2)
            for a in free_entries:
                if a.id in used_a or pid not in pairs_a[a.id]:
                    continue
                net_a = round(a.debit - a.credit, 2)
                if abs(net - net_a) < 0.01 and net != 0:
                    for x in movs:
                        matches.append({"banco": x, "asiento": a,
                                        "metodo": label(p, group=window_name)})
                        used_b.add(x.id)
                    used_a.add(a.id)
                    break

    # --- groups per day: 1 bank -> N entries ----------------------------------
    groups_a = defaultdict(list)
    for a in free_entries:
        if a.id in used_a:
            continue
        for pid in pairs_a[a.id]:
            groups_a[(pid, a.date)].append(a)
    for (pid, day), items in groups_a.items():
        if len(items) < 2 or day is None:
            continue
        if any(a.id in used_a for a in items):
            continue
        p = next(x for x in pairs if x["id"] == pid)
        net = round(sum(x.debit - x.credit for x in items), 2)
        for m in free_bank:
            if m.id in used_b or not any(x["id"] == pid for x in pairs_b[m.id]):
                continue
            net_b = round(m.credit - m.debit, 2)
            if abs(net - net_b) < 0.01 and net != 0:
                for x in items:
                    matches.append({"banco": m, "asiento": x, "metodo": label(p, group="día")})
                    used_a.add(x.id)
                used_b.add(m.id)
                break

    return matches, used_b, used_a
