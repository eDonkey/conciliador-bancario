# -*- coding: utf-8 -*-
"""Rules learned from manual reconciliations.

Every time the user matches by hand (or accepts an AI suggestion), a
generalizable rule is extracted: the "signature" of the bank concept and the
one of the ledger entry. In future reconciliations, the pairs that meet a
rule (and whose amounts add up) are matched automatically with the method
"regla aprendida".

Rule types:
  - "1a1":   one bank movement ↔ one entry (same amount, no date limit and
             no tie-breaker: the rule is the tie-breaker).
  - "grupo": N movements of the same concept and day ↔ 1 entry (or the
             other way around), when the sum adds up.
"""
import json
import os
import re
from datetime import date

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "datos", "reglas_aprendidas.json")

STOP_WORDS = {
    "santander", "santanter", "cuenta", "varios", "cuit", "para", "por",
    "banco", "pesos", "nota", "segun", "sobre", "desde", "hasta",
}


def bank_signature(description: str) -> str:
    """Normalized bank concept: no numbers or signs, lowercase."""
    t = re.sub(r'[\d$.,/%-]+', ' ', (description or '').lower())
    return ' '.join(t.split())


def ledger_signature(reference: str, comment: str) -> str:
    """Signature of the entry: alphabetic prefix of the reference + the
    distinctive words of the comment."""
    ref = re.sub(r'[^A-Za-z]', '', reference or '').upper()
    tokens = [w for w in re.findall(r'[a-záéíóúñ]{4,}', (comment or '').lower())
              if w not in STOP_WORDS]
    return ref + '|' + ' '.join(sorted(set(tokens))[:4])


def load(path: str = DEFAULT_PATH) -> list[dict]:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save(rules: list[dict], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=1)


def _derive(match: dict) -> dict | None:
    """Derives the rule of a serialized manual match ({'banco': [...dicts],
    'mayor': [...dicts]}). Returns None if it can't be generalized."""
    bank_sigs = sorted({bank_signature(b["descripcion"]) for b in match["banco"]})
    ledger_sigs = sorted({ledger_signature(a["referencia"], a["comentario"]) for a in match["mayor"]})
    if not bank_sigs or not ledger_sigs:
        return None
    # only generalized when each side has a single concept
    if len(bank_sigs) != 1 or len(ledger_sigs) != 1:
        return None
    kind = "1a1" if len(match["banco"]) == 1 and len(match["mayor"]) == 1 else "grupo"
    return {"tipo": kind, "banco": bank_sigs[0], "mayor": ledger_sigs[0]}


def learn(match: dict, path: str = DEFAULT_PATH) -> dict | None:
    """Records (or reinforces) the rule derived from a manual match."""
    new = _derive(match)
    if new is None:
        return None
    rules = load(path)
    for r in rules:
        if (r["tipo"], r["banco"], r["mayor"]) == (new["tipo"], new["banco"], new["mayor"]):
            r["veces"] = r.get("veces", 1) + 1
            r["actualizada"] = date.today().isoformat()
            _save(rules, path)
            return r
    new["veces"] = 1
    new["actualizada"] = date.today().isoformat()
    rules.append(new)
    _save(rules, path)
    return new


def forget(match: dict, path: str = DEFAULT_PATH):
    """When a manual match is undone, weakens/removes its rule."""
    rule = _derive(match)
    if rule is None:
        return
    rules = load(path)
    for r in list(rules):
        if (r["tipo"], r["banco"], r["mayor"]) == (rule["tipo"], rule["banco"], rule["mayor"]):
            r["veces"] = r.get("veces", 1) - 1
            if r["veces"] <= 0:
                rules.remove(r)
            _save(rules, path)
            return


def apply(free_bank, free_entries, rules):
    """Applies the learned rules to the residuals.

    Returns (pairs, used_bank_ids, used_ledger_ids) where pairs is a list of
    dicts {banco, asiento, metodo} (groups produce one pair per movement,
    sharing the entry)."""
    if not rules:
        return [], set(), set()

    one_to_one = {(r["banco"], r["mayor"]) for r in rules if r["tipo"] == "1a1"}
    group_rules = {(r["banco"], r["mayor"]) for r in rules if r["tipo"] == "grupo"}
    # a 1a1 rule also allows the pairing inside a trivial group
    all_rules = one_to_one | group_rules

    pairs = []
    used_b, used_a = set(), set()

    fb = {m.id: bank_signature(m.description) for m in free_bank}
    fa = {a.id: ledger_signature(a.reference, a.comment) for a in free_entries}

    # --- 1 to 1: same amount/side + the rule's signature ---------------------
    from collections import defaultdict
    idx = defaultdict(list)
    for a in free_entries:
        bank_side = 'credito' if a.side == 'debe' else 'debito'
        idx[(bank_side, round(a.amount, 2))].append(a)
    for m in free_bank:
        for a in idx.get((m.side, round(m.amount, 2)), []):
            if a.id in used_a:
                continue
            if (fb[m.id], fa[a.id]) in all_rules:
                pairs.append({"banco": m, "asiento": a, "metodo": "regla aprendida"})
                used_b.add(m.id)
                used_a.add(a.id)
                break

    # --- groups per day: N bank -> 1 entry -----------------------------------
    groups_b = defaultdict(list)
    for m in free_bank:
        if m.id not in used_b:
            groups_b[(fb[m.id], m.date)].append(m)
    for (sig_b, day), items in groups_b.items():
        if len(items) < 2 or day is None:
            continue
        net = round(sum(x.credit - x.debit for x in items), 2)
        for a in free_entries:
            if a.id in used_a or (sig_b, fa[a.id]) not in group_rules:
                continue
            net_a = round(a.debit - a.credit, 2)
            if abs(net - net_a) < 0.01 and net != 0:
                for x in items:
                    pairs.append({"banco": x, "asiento": a,
                                  "metodo": "regla aprendida (grupo)"})
                    used_b.add(x.id)
                used_a.add(a.id)
                break

    # --- groups per day: 1 bank -> N entries ----------------------------------
    groups_a = defaultdict(list)
    for a in free_entries:
        if a.id not in used_a:
            groups_a[(fa[a.id], a.date)].append(a)
    for (sig_a, day), items in groups_a.items():
        if len(items) < 2 or day is None:
            continue
        net = round(sum(x.debit - x.credit for x in items), 2)
        for m in free_bank:
            if m.id in used_b or (fb[m.id], sig_a) not in group_rules:
                continue
            net_b = round(m.credit - m.debit, 2)
            if abs(net - net_b) < 0.01 and net != 0:
                for x in items:
                    pairs.append({"banco": m, "asiento": x,
                                  "metodo": "regla aprendida (grupo)"})
                    used_a.add(x.id)
                used_b.add(m.id)
                break

    return pairs, used_b, used_a
