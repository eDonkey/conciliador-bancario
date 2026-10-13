# -*- coding: utf-8 -*-
"""Bank fees defined by the user.

The matcher ships with a built-in pattern of bank fees/taxes (FEE_RE), but
new concepts show up ("Percep perc rg 5617 30%") that the user must be able
to add from the app without touching code. Each term is stored without
numbers or signs (so "rg 5617 30%" generalizes to any future rate) and
applies to every following reconciliation.
"""
import json
import os
import re
import uuid
from datetime import date

from engine.equivalences import _normalize

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "datos", "gastos_usuario.json")


def clean_term(text: str) -> str:
    """Concept without numbers: tokens that contain digits are dropped
    WHOLE. 'Percep perc rg 5617 30%' -> 'percep perc rg';
    'REG REC SIRC 28/08/26 00014Q' -> 'reg rec sirc'.
    (Only the digits used to be removed, and a voucher like '00014Q' left a
    stray 'q' that made the term match nothing.)"""
    tokens = (text or '').lower().split()
    clean = [re.sub(r'[$.,/%-]+', '', t) for t in tokens
             if not re.search(r'\d', t)]
    return ' '.join(t for t in clean if t)


def load(path: str = DEFAULT_PATH) -> list[dict]:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save(terms: list[dict], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(terms, f, ensure_ascii=False, indent=1)


def add(text: str, path: str = DEFAULT_PATH) -> tuple[list[dict], str | None]:
    term = clean_term(text)
    if len(term) < 4:
        return load(path), "El concepto necesita al menos 4 caracteres (sin contar números)"
    terms = load(path)
    if any(_normalize(t["termino"]) == _normalize(term) for t in terms):
        return terms, "Ese concepto ya está registrado como gasto bancario"
    terms.append({"id": uuid.uuid4().hex[:8], "termino": term,
                  "creada": date.today().isoformat()})
    _save(terms, path)
    return terms, None


def remove(term_id: str, path: str = DEFAULT_PATH) -> list[dict]:
    terms = [t for t in load(path) if t["id"] != term_id]
    _save(terms, path)
    return terms


def _comparable(text: str) -> str:
    """The text in the SAME shape as the stored terms: no tokens with digits
    and no signs. So 'DBCR 25413 S/DB TASA GRAL' compares as
    'dbcr sdb tasa gral' and the term learned from that same movement matches
    it (the literal term used to be searched inside the original description,
    and the 25413 in the middle or the slash of S/DB made it impossible)."""
    return _normalize(clean_term(text))


def is_fee(description: str, terms: list[dict]) -> bool:
    if not terms:
        return False
    base = " " + _comparable(description) + " "
    for t in terms:
        term = _normalize(t.get("termino") or "").strip()
        # anchored at a word start; the end allows a prefix ('credit'
        # matches 'creditos'), like the historical matching
        if term and (" " + term) in base:
            return True
    return False


# --- Exceptions: concepts that are NEVER a bank fee -------------------------
# The built-in detector (or a user term) can over-classify; when a movement
# is moved out of Fees, the concept can be learned as an exception, and no
# similar movement lands there again.
EXCEPTIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "datos", "gastos_excepciones.json")


def load_exceptions(path: str = EXCEPTIONS_PATH) -> list[dict]:
    return load(path)


def add_exception(text: str, path: str = EXCEPTIONS_PATH):
    return add(text, path)


def remove_exception(term_id: str, path: str = EXCEPTIONS_PATH) -> list[dict]:
    return remove(term_id, path)
