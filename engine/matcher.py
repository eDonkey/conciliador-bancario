# -*- coding: utf-8 -*-
"""Bank reconciliation engine.

Logic (following the flow of the FES accounting system):
  - Account O (operative/transitory) receives payment orders and receipts by
    default. When a movement is confirmed, the counter-entry is made in O and
    the entry moves to account E, which should mirror the bank statement.
  - Reconciling = statement vs account E; what isn't in E is looked up in O
    (movements pending confirmation).

Sign convention:
  bank credit (money in)   <->  ledger Debe (debit)
  bank debit  (money out)  <->  ledger Haber (credit)
"""
import re
from collections import defaultdict

# Keywords of bank fees/taxes that are usually not recorded entry by entry in
# the ledger.
# Careful with short terms: without a word boundary, "iva " matches
# "cooperatIVA de seguros" and "com " matches "teleCOM" (it happened with
# payments to real suppliers that ended up as fees).
FEE_RE = re.compile(
    # (?<![a-z])sirc covers "SIRCREB" (Santander) and the abbreviated "REG REC
    # SIRC" of BBVA/Francés; a bare 25413 covers Macro's "DBCR 25413 S/DB";
    # "inter." covers abbreviated interest ("INTER.ADEL.CC S/ACUERD")
    r'comision|(?<![a-z])iva(?![a-z])|impuesto|(?<![a-z])sirc|percepcion|iibb|'
    r'ley ?2[57]\.?[47]?\d*|(?<!\d)25\.?413(?!\d)|mantenimiento|sellados|'
    r'intereses|(?<![a-z])inter\.|(?<![a-z])com\.? ',
    re.IGNORECASE,
)

NUM_RE = re.compile(r'\d{5,}')

# Window for "blind" matches: same amount, no voucher and not a single word
# in common. Beyond it, two different operations of the same amount are more
# likely than the same one.
BLIND_TOLERANCE_DAYS = 10

# --- Monthly debit note of bank fees ------------------------------------------
# Fees are not booked day by day: they are grouped into a single monthly
# debit note, separating commissions taxed at 21%, interest at 10.5% (each
# group with its VAT) and the untaxed concepts (law 25.413, SIRCREB,
# perceptions).
FEE_CATEGORIES = [
    ("IVA 10,5%", re.compile(r'iva\s*10[.,]?5', re.I)),
    ("IVA 21%", re.compile(r'iva', re.I)),
    ("Impuesto ley 25.413", re.compile(r'ley\s*25\.?413|(?<!\d)25\.?413(?!\d)', re.I)),
    ("SIRCREB", re.compile(r'(?<![a-z])sirc', re.I)),
    ("Percepciones IIBB", re.compile(r'iibb|percep', re.I)),   # abbreviated "percep" included
]
# commissions and interest can be taxed at 21% or 10.5% (client feedback): if
# the concept carries the rate in its text it is respected; otherwise the
# historical default (commissions -> 21%, interest -> 10.5%)
COMMISSION_RE = re.compile(r'comision|mantenimiento|sellado|com\.? ', re.I)
INTEREST_RE = re.compile(r'inter[eé]s|(?<![a-z])inter\.', re.I)
RATE_105_RE = re.compile(r'10[.,]5')
RATE_21_RE = re.compile(r'(?<![\d.,])21(?![\d.,])')
CATEGORY_ORDER = [
    "Comisiones (grav. 21%)", "Intereses saldo deudor (grav. 21%)", "IVA 21%",
    "Comisiones (grav. 10,5%)", "Intereses saldo deudor (grav. 10,5%)", "IVA 10,5%",
    "Impuesto ley 25.413", "SIRCREB", "Percepciones IIBB", "Otros",
]
DEBIT_NOTE_RE = re.compile(r'gastos?\s*banc', re.I)
CREDIT_NOTE_RE = re.compile(r'impuestos\s+liq|nota\s+de\s+cr[eé]dito|\bNC-', re.I)
MONTHS = {1: 'ENE', 2: 'FEB', 3: 'MAR', 4: 'ABR', 5: 'MAY', 6: 'JUN',
          7: 'JUL', 8: 'AGO', 9: 'SEP', 10: 'OCT', 11: 'NOV', 12: 'DIC'}
MONTH_TEXT_RE = re.compile(
    r'\b(ENE(?:RO)?|FEB(?:RERO)?|MAR(?:ZO)?|ABR(?:IL)?|MAY(?:O)?|JUN(?:IO)?|'
    r'JUL(?:IO)?|AGO(?:STO)?|SEP(?:TIEMBRE)?|OCT(?:UBRE)?|NOV(?:IEMBRE)?|DIC(?:IEMBRE)?)\b',
    re.I)


def _fee_category(description: str) -> str:
    d = description or ""
    for name, rx in FEE_CATEGORIES:
        if rx.search(d):
            return name
    if INTEREST_RE.search(d):
        if RATE_21_RE.search(d) and not RATE_105_RE.search(d):
            return "Intereses saldo deudor (grav. 21%)"
        return "Intereses saldo deudor (grav. 10,5%)"
    if COMMISSION_RE.search(d):
        if RATE_105_RE.search(d):
            return "Comisiones (grav. 10,5%)"
        return "Comisiones (grav. 21%)"
    return "Otros"


def _entry_month(a) -> str | None:
    """Month (e.g. 'ENE') a ledger debit-note entry belongs to."""
    m = MONTH_TEXT_RE.search(f"{a.reference} {a.comment}")
    if m:
        return m.group(1)[:3].upper()
    return MONTHS.get(a.date.month) if a.date else None


def debit_note_summary(fees, free_entries):
    """Groups the bank fees by statement period and compares them with the
    monthly debit notes recorded in the ledger.

    Returns (periods, used_entry_ids)."""
    by_file = defaultdict(list)
    for g in fees:
        by_file[g.file].append(g)

    # ledger candidates: debit notes (credit side) and tax credit notes (debit side)
    debit_notes = [a for a in free_entries if a.credit and DEBIT_NOTE_RE.search(f"{a.reference} {a.comment}")]
    credit_notes = [a for a in free_entries if a.debit and CREDIT_NOTE_RE.search(f"{a.reference} {a.comment}")]

    periods = []
    used = set()
    for file in sorted(by_file):
        items = by_file[file]
        dates = [g.date for g in items if g.date]
        until = max(dates) if dates else None
        month = MONTHS.get(until.month) if until else None
        label = f"{month}/{until.strftime('%y')}" if until else file

        cats = defaultdict(lambda: {"cantidad": 0, "importe": 0.0})
        concepts = defaultdict(lambda: {"cantidad": 0, "importe": 0.0})
        for g in items:
            net = g.debit - g.credit
            c = _fee_category(g.description)
            cats[c]["cantidad"] += 1
            cats[c]["importe"] += net
            concepts[g.description.strip()]["cantidad"] += 1
            concepts[g.description.strip()]["importe"] += net

        category_rows = []
        for name in CATEGORY_ORDER:
            if name not in cats:
                continue
            row = {"categoria": name,
                   "cantidad": cats[name]["cantidad"],
                   "importe": round(cats[name]["importe"], 2)}
            if name == "IVA 21%" and "Comisiones (grav. 21%)" in cats:
                row["iva_esperado"] = round(0.21 * cats["Comisiones (grav. 21%)"]["importe"], 2)
            if name == "IVA 10,5%" and "Intereses saldo deudor (grav. 10,5%)" in cats:
                row["iva_esperado"] = round(0.105 * cats["Intereses saldo deudor (grav. 10,5%)"]["importe"], 2)
            category_rows.append(row)

        statement_total = round(sum(f["importe"] for f in category_rows), 2)

        # ledger entries of this month
        period_entries = [a for a in debit_notes if _entry_month(a) == month]
        # credit notes of the month after the period also belong (they are settled later)
        credit_note_entries = [a for a in credit_notes
                               if a.date and until and 0 <= (a.date - until).days <= 40]
        ledger_total = round(sum(a.credit for a in period_entries)
                             - sum(a.debit for a in credit_note_entries), 2)
        for a in period_entries + credit_note_entries:
            used.add(a.id)

        periods.append({
            "periodo": label,
            "archivo": file,
            "movimientos": len(items),
            "categorias": category_rows,
            "conceptos": sorted(
                [{"descripcion": k, **v, "importe": round(v["importe"], 2)}
                 for k, v in concepts.items()],
                key=lambda x: -abs(x["importe"])),
            "total_extracto": statement_total,
            "asientos_mayor": period_entries + credit_note_entries,
            "total_mayor": ledger_total if (period_entries or credit_note_entries) else None,
            "diferencia": round(statement_total - ledger_total, 2)
                          if (period_entries or credit_note_entries) else None,
        })
    return periods, used


# --- Amount tolerance ---------------------------------------------------------
# Small differences between bank and ledger (a commission deducted from the
# same transfer, settlement rounding). The pair is only accepted if the
# candidate is UNIQUE in both directions within the tolerance.
TOL_ABS = 100.0     # floor: ±$100
TOL_PCT = 0.001     # 0.1% of the amount
TOL_CAP = 2000.0    # absolute ceiling
TOL_DAYS = 45


def _tolerance(amount: float) -> float:
    return min(max(TOL_ABS, amount * TOL_PCT), TOL_CAP)


def match_with_tolerance(free_bank, free_entries):
    """1 to 1 matches with a small amount difference. Returns
    (pairs, used_bank_ids, used_ledger_ids)."""
    import bisect

    entries_by_side = {"credito": [], "debito": []}
    for a in free_entries:
        bank_side = 'credito' if a.side == 'debe' else 'debito'
        entries_by_side[bank_side].append(a)
    bank_by_side = {"credito": [], "debito": []}
    for m in free_bank:
        bank_by_side[m.side].append(m)
    for d in (entries_by_side, bank_by_side):
        for items in d.values():
            items.sort(key=lambda x: x.amount)
    amounts_a = {k: [a.amount for a in v] for k, v in entries_by_side.items()}
    amounts_b = {k: [m.amount for m in v] for k, v in bank_by_side.items()}

    def near(f1, f2):
        return f1 and f2 and abs((f1 - f2).days) <= TOL_DAYS

    def candidates(amount, day, items, amounts):
        tol = _tolerance(amount)
        out = []
        i = bisect.bisect_left(amounts, amount - tol)
        while i < len(items) and amounts[i] <= amount + tol:
            x = items[i]
            if abs(x.amount - amount) > 0.005 and near(day, x.date):
                out.append(x)
            i += 1
        return out

    pairs, used_b, used_a = [], set(), set()
    for m in free_bank:
        cands = candidates(m.amount, m.date, entries_by_side[m.side], amounts_a[m.side])
        if len(cands) != 1 or cands[0].id in used_a:
            continue
        a = cands[0]
        # the entry must also have this movement as its only candidate
        reverse = candidates(a.amount, a.date, bank_by_side[m.side], amounts_b[m.side])
        if len(reverse) != 1 or reverse[0].id != m.id:
            continue
        diff = round(m.amount - a.amount, 2)
        pairs.append({"banco": m, "asiento": a,
                      "metodo": f"importe aproximado (dif $ {diff:+,.2f})"})
        used_b.add(m.id)
        used_a.add(a.id)
    return pairs, used_b, used_a


WORD_RE = re.compile(r'[a-zñ]{4,}')
# words that appear in almost every text and tell nothing apart
FILLER_WORDS = {'banco', 'santander', 'frances', 'galicia', 'macro', 'ciudad', 'cuenta',
                'corriente', 'pesos', 'transferencia', 'realizada', 'recibida', 'pago',
                'pagos', 'terceros', 'varios', 'fecha', 'importe', 'movimiento'}


def _words(text: str) -> set[str]:
    """Meaningful words of a text, without accents or filler."""
    t = (text or '').lower()
    for a, b in (('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u'), ('ü', 'u')):
        t = t.replace(a, b)
    return set(WORD_RE.findall(t)) - FILLER_WORDS


def _affinity(m, a) -> int:
    """How similar the texts of a movement and an entry are (words in
    common). Only used to BREAK TIES between candidates that already agree on
    amount and date: the FBS ledger almost never repeats the bank's text, so
    requiring it would lose good matches, but when two candidates tie the one
    that also shares the name or the concept wins."""
    return len(_words(f"{m.description} {m.detail}")
               & _words(f"{a.reference} {a.comment}"))


def _numbers(text: str) -> set[str]:
    """Extracts reference numbers (5+ digits) from a text."""
    return {n.lstrip('0') for n in NUM_RE.findall(text or '') if n.lstrip('0')}


def _rm_key(text: str) -> str | None:
    m = re.search(r'RM-?\s*(\d+)', text or '', re.IGNORECASE)
    return m.group(1).lstrip('0') if m else None


def net_reversals(entries):
    """Finds entry + reversal pairs inside the same ledger residual: same key
    (RM or reference), same amount, opposite sides. They are cancelled
    movements that will never show up in the statement.

    Returns (remaining, pairs) where pairs is [{debe, haber}]."""
    debit = defaultdict(list)
    credit = defaultdict(list)
    for a in entries:
        key = (_rm_key(a.comment) or a.reference.strip().upper(), round(a.amount, 2))
        (debit if a.side == 'debe' else credit)[key].append(a)

    pairs, cancelled = [], set()
    for key, debit_list in debit.items():
        credit_list = credit.get(key, [])
        for k in range(min(len(debit_list), len(credit_list))):
            pairs.append({"debe": debit_list[k], "haber": credit_list[k]})
            cancelled.add(debit_list[k].id)
            cancelled.add(credit_list[k].id)
    remaining = [a for a in entries if a.id not in cancelled]
    return remaining, pairs


def net_o_account(o_entries):
    """Cancels inside O the confirmed pairs (entry + counter-entry).

    A confirmed movement appears twice in O: once on each side, with the same
    amount and the same voucher (RM / reference). What is left uncancelled are
    the movements pending confirmation.
    """
    debit = defaultdict(list)
    credit = defaultdict(list)
    for a in o_entries:
        key = (_rm_key(a.comment) or a.reference.strip().upper(), round(a.amount, 2))
        (debit if a.side == 'debe' else credit)[key].append(a)

    cancelled = set()
    for key, debit_list in debit.items():
        credit_list = credit.get(key, [])
        n = min(len(debit_list), len(credit_list))
        for k in range(n):
            cancelled.add(debit_list[k].id)
            cancelled.add(credit_list[k].id)
    pending = [a for a in o_entries if a.id not in cancelled]
    return pending, cancelled


def _match_passes(bank_movs, entries, tolerance_days=45):
    """Matches bank movements against ledger entries.

    Returns (matches, unmatched_bank, unmatched_entries) where matches is a
    list of dicts {banco, asiento, metodo}.
    """
    matches = []
    free_bank = list(bank_movs)
    free_entries = list(entries)

    def index(entries_):
        idx = defaultdict(list)
        for a in entries_:
            bank_side = 'credito' if a.side == 'debe' else 'debito'
            idx[(bank_side, round(a.amount, 2))].append(a)
        return idx

    # --- Pass 1: amount + shared reference number ----------------------------
    idx = index(free_entries)
    used_a, used_b = set(), set()
    for m in free_bank:
        candidates = idx.get((m.side, round(m.amount, 2)), [])
        text_b = f"{m.voucher or ''} {m.description} {m.detail}"
        nums_b = _numbers(text_b)
        if not nums_b:
            continue
        for a in candidates:
            if a.id in used_a:
                continue
            nums_a = _numbers(f"{a.reference} {a.comment}")
            if nums_b & nums_a:
                matches.append({"banco": m, "asiento": a, "metodo": "importe+referencia"})
                used_a.add(a.id)
                used_b.add(m.id)
                break
    free_bank = [m for m in free_bank if m.id not in used_b]
    free_entries = [a for a in free_entries if a.id not in used_a]

    # --- Pass 2: unique amount (a single candidate on each side) -------------
    idx = index(free_entries)
    idx_b = defaultdict(list)
    for m in free_bank:
        idx_b[(m.side, round(m.amount, 2))].append(m)
    used_a, used_b = set(), set()
    for key, list_b in idx_b.items():
        list_a = idx.get(key, [])
        if len(list_b) == 1 and len(list_a) == 1:
            m, a = list_b[0], list_a[0]
            if m.date and a.date and abs((m.date - a.date).days) > tolerance_days:
                continue
            matches.append({"banco": m, "asiento": a, "metodo": "importe único"})
            used_b.add(m.id)
            used_a.add(a.id)
    free_bank = [m for m in free_bank if m.id not in used_b]
    free_entries = [a for a in free_entries if a.id not in used_a]

    # --- Pass 3: same amount, assigned by the closest date -------------------
    idx = index(free_entries)
    used_a, used_b = set(), set()
    by_key = defaultdict(list)
    for m in free_bank:
        by_key[(m.side, round(m.amount, 2))].append(m)
    for key, list_b in by_key.items():
        list_a = [a for a in idx.get(key, [])]
        if not list_a:
            continue
        # greedy by the smallest date distance; among ties wins the one that
        # shares words (payee name, concept) with the entry
        pairs = []
        for m in list_b:
            for a in list_a:
                if m.date and a.date:
                    d = abs((m.date - a.date).days)
                else:
                    d = 9999
                af = _affinity(m, a)
                # with no voucher or text in common the match is blind: the
                # window shrinks there. With 45 days it ended up matching a
                # payment with someone else's entry of the same amount from
                # two weeks before (client feedback) and consuming it forever.
                if d <= (tolerance_days if af else BLIND_TOLERANCE_DAYS):
                    pairs.append((d, -af, m, a))
        pairs.sort(key=lambda p: (p[0], p[1], p[2].id, p[3].id))
        for d, minus_af, m, a in pairs:
            if m.id in used_b or a.id in used_a:
                continue
            # honest warning: there was no voucher in common here, only amount
            # and date. It's the match worth reviewing by hand.
            detail = "" if minus_af else " · sin comprobante ni texto en común"
            matches.append({"banco": m, "asiento": a,
                            "metodo": f"importe+fecha (±{d}d){detail}"})
            used_b.add(m.id)
            used_a.add(a.id)
    free_bank = [m for m in free_bank if m.id not in used_b]
    free_entries = [a for a in free_entries if a.id not in used_a]

    return matches, free_bank, free_entries


def reconcile(bank_movs, e_entries, o_entries, learned_rules=None,
              equivalence_pairs=None, user_fee_terms=None, fee_exceptions=None):
    """Runs the full reconciliation. Returns a serializable dict."""
    from engine import rules
    from engine import equivalences
    from engine import fee_terms

    o_pending, o_cancelled = net_o_account(o_entries)

    # 1) statement vs account E
    matches_e, bank_without_e, e_without_bank = _match_passes(bank_movs, e_entries)

    # 2) what isn't in E is looked up in the O pending confirmation
    matches_o, bank_unmatched, o_pending_without_bank = _match_passes(bank_without_e, o_pending)

    # 2b) rules learned from previous manual reconciliations
    rule_pairs, used_b, used_a = rules.apply(
        bank_unmatched, e_without_bank + o_pending_without_bank, learned_rules or [])
    rules_applied = len(rule_pairs)
    if rule_pairs:
        bank_unmatched = [m for m in bank_unmatched if m.id not in used_b]
        e_without_bank = [a for a in e_without_bank if a.id not in used_a]
        o_pending_without_bank = [a for a in o_pending_without_bank if a.id not in used_a]
        for p in rule_pairs:
            (matches_e if p["asiento"].sheet == 'E' else matches_o).append(p)

    # 2c) vocabulary equivalences (e.g. haberes ≈ sueldos)
    eq_pairs, used_b, used_a = equivalences.apply(
        bank_unmatched, e_without_bank + o_pending_without_bank, equivalence_pairs or [])
    equivalences_applied = len({p["banco"].id for p in eq_pairs})
    if eq_pairs:
        bank_unmatched = [m for m in bank_unmatched if m.id not in used_b]
        e_without_bank = [a for a in e_without_bank if a.id not in used_a]
        o_pending_without_bank = [a for a in o_pending_without_bank if a.id not in used_a]
        for p in eq_pairs:
            (matches_e if p["asiento"].sheet == 'E' else matches_o).append(p)

    # 2d) strict amount tolerance (commissions/rounding), without ambiguity
    tol_pairs, used_b, used_a = match_with_tolerance(
        bank_unmatched, e_without_bank + o_pending_without_bank)
    approximate = len(tol_pairs)
    if tol_pairs:
        bank_unmatched = [m for m in bank_unmatched if m.id not in used_b]
        e_without_bank = [a for a in e_without_bank if a.id not in used_a]
        o_pending_without_bank = [a for a in o_pending_without_bank if a.id not in used_a]
        for p in tol_pairs:
            (matches_e if p["asiento"].sheet == 'E' else matches_o).append(p)

    # 3) classify the bank leftovers (built-in pattern + the user's fee terms).
    # A movement reclassified by hand carries _force_fee (True/False) and that
    # decision overrides the detectors — so the monthly debit note is right too.
    bank_fees, unbooked = [], []
    for m in bank_unmatched:
        forced = getattr(m, "_force_fee", None)
        if forced is not None:
            # a manual decision about THIS movement: overrides everything
            is_fee = forced
        elif fee_terms.is_fee(m.description, fee_exceptions):
            # concept learned as "never a fee": overrides the detectors
            is_fee = False
        else:
            is_fee = bool(FEE_RE.search(m.description)
                          or fee_terms.is_fee(m.description, user_fee_terms))
        (bank_fees if is_fee else unbooked).append(m)

    # 4) monthly debit note: group fees by period and match them against the
    #    ledger's "GASTOS BANCARIOS" entries
    debit_note, debit_note_ids = debit_note_summary(
        bank_fees, e_without_bank + o_pending_without_bank)
    e_without_bank = [a for a in e_without_bank if a.id not in debit_note_ids]
    o_pending_without_bank = [a for a in o_pending_without_bank if a.id not in debit_note_ids]

    # 5) reversals: cancelled pairs inside the ledger residual (an entry and
    #    its counter-entry that will never have a bank movement)
    e_without_bank, reversals = net_reversals(e_without_bank)

    def tot(ms):
        return round(sum(x.amount for x in ms), 2)

    def tot_a(as_):
        return round(sum(x.amount for x in as_), 2)

    # % explained: besides the matched ones, the bank fees whose period
    # matched a ledger debit note are justified even without a 1 to 1 entry.
    fees_explained = sum(p["movimientos"] for p in debit_note
                         if p["total_mayor"] is not None)
    explained = len(matches_e) + len(matches_o) + fees_explained

    return {
        "nota_debito": debit_note,
        "contraasientos": reversals,
        "matches_e": matches_e,
        "matches_o": matches_o,
        "banco_sin_contabilizar": unbooked,
        "gastos_bancarios": bank_fees,
        "e_sin_banco": e_without_bank,
        "o_pendientes_sin_banco": o_pending_without_bank,
        "o_cancelados": len(o_cancelled),
        "o_pendientes_total": len(o_pending),
        "resumen": {
            "movimientos_banco": len(bank_movs),
            "conciliados_e": len(matches_e),
            "en_o_pendientes_confirmar": len(matches_o),
            "reglas_aplicadas": rules_applied,
            "equivalencias_aplicadas": equivalences_applied,
            "aproximados": approximate,
            "gastos_explicados": fees_explained,
            "contraasientos": len(reversals),
            "gastos_bancarios": {"cantidad": len(bank_fees), "importe": tot(bank_fees)},
            "banco_sin_contabilizar": {"cantidad": len(unbooked), "importe": tot(unbooked)},
            "e_sin_banco": {"cantidad": len(e_without_bank), "importe": tot_a(e_without_bank)},
            "o_pendientes_sin_banco": {"cantidad": len(o_pending_without_bank), "importe": tot_a(o_pending_without_bank)},
            "porcentaje_conciliado": round(
                100.0 * (len(matches_e) + len(matches_o)) / max(1, len(bank_movs)), 1),
            "porcentaje_explicado": round(
                min(100.0, 100.0 * explained / max(1, len(bank_movs))), 1),
            "nota_debito": {
                "periodos": len(debit_note),
                "diferencia_total": round(
                    sum(p["diferencia"] for p in debit_note
                        if p["diferencia"] is not None), 2),
            },
        },
    }
