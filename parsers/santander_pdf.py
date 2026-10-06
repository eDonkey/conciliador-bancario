# -*- coding: utf-8 -*-
"""Parser of Banco Santander Argentina current account statements (PDF).

Extracts the movements of the "Movimientos" table from the positions of the
words on the page. Debit/credit comes from the balance difference (delta),
which can't be misread; the printed amount is only used to validate it.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime

import pdfplumber

AMOUNT_RE = re.compile(r'^-?[\d.]+,\d{2}$')
DATE_RE = re.compile(r'^\d{2}/\d{2}/\d{2}$')
STOP_RE = re.compile(r'^Saldo total \$')


@dataclass
class BankMovement:
    id: str
    date: date | None
    voucher: str | None
    description: str
    detail: str
    debit: float
    credit: float
    balance: float
    file: str
    page: int
    warning: bool = False  # the printed amount doesn't match the balance delta

    @property
    def amount(self) -> float:
        return self.credit if self.credit else self.debit

    @property
    def side(self) -> str:
        return "credito" if self.credit else "debito"

    def to_dict(self):
        return {
            "id": self.id,
            "fecha": self.date.isoformat() if self.date else None,
            "comprobante": self.voucher,
            "descripcion": self.description,
            "detalle": self.detail,
            "debito": self.debit,
            "credito": self.credit,
            "saldo": self.balance,
            "archivo": self.file,
            "advertencia": self.warning,
        }


def _parse_amount(s: str) -> float:
    s = s.replace('$', '').replace(' ', '').strip()
    neg = s.startswith('-')
    v = float(s.lstrip('-').replace('.', '').replace(',', '.'))
    return -v if neg else v


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, '%d/%m/%y').date()
    except ValueError:
        return None


def _cluster_lines(words, tol=2.0):
    """Groups words into visual lines by vertical coordinate."""
    if not words:
        return []
    ws = sorted(words, key=lambda w: (w['top'], w['x0']))
    lines, current, last_top = [], [ws[0]], ws[0]['top']
    for w in ws[1:]:
        if w['top'] - last_top <= tol:
            current.append(w)
        else:
            lines.append(sorted(current, key=lambda x: x['x0']))
            current = [w]
        last_top = w['top']
    lines.append(sorted(current, key=lambda x: x['x0']))
    return lines


def parse_statement(path: str, file_name: str | None = None) -> dict:
    """Parses a PDF statement. Returns a dict with the metadata and the movements."""
    name = file_name or str(path)
    movements: list[BankMovement] = []
    meta = {"archivo": name, "titular": None, "cuenta": None,
            "desde": None, "hasta": None, "saldo_inicial": None, "saldo_final": None}
    raw_rows = []  # (date, voucher, description, amounts<=500, balance, detail, page)
    stop = False

    with pdfplumber.open(path) as pdf:
        for pageno, page in enumerate(pdf.pages, 1):
            if stop:
                break
            lines = _cluster_lines(page.extract_words())
            texts = [' '.join(w['text'] for w in lw) for lw in lines]

            if pageno == 1:
                for t in texts:
                    m = re.search(r'Desde:\s*(\d{2}/\d{2}/\d{2})', t)
                    if m:
                        meta["desde"] = _parse_date(m.group(1))
                    m = re.search(r'Hasta:\s*(\d{2}/\d{2}/\d{2})', t)
                    if m:
                        meta["hasta"] = _parse_date(m.group(1))
                    m = re.search(r'Cuenta Corriente N.?\s*([\d/-]+)', t)
                    if m and not meta["cuenta"]:
                        meta["cuenta"] = m.group(1)
                    if 'CUIT:' in t and not meta["titular"]:
                        meta["titular"] = texts[max(0, texts.index(t) - 1)]

            for i, lw in enumerate(lines):
                t = texts[i]
                if STOP_RE.match(t) or 'Detalle impositivo' in t:
                    stop = True
                    break
                balance_words = [w for w in lw if w['x0'] > 500 and AMOUNT_RE.match(w['text'])]
                if not balance_words:
                    continue
                balance = _parse_amount(balance_words[-1]['text'])
                amounts = [_parse_amount(w['text']) for w in lw
                           if AMOUNT_RE.match(w['text']) and w['x0'] <= 500]
                day = None
                for cand in (lw,
                             lines[i + 1] if i + 1 < len(lines) else [],
                             lines[i - 1] if i > 0 else []):
                    for w in cand:
                        if w['x0'] < 60 and DATE_RE.match(w['text']):
                            day = _parse_date(w['text'])
                            break
                    if day:
                        break
                voucher = next((w['text'] for w in lw if 60 <= w['x0'] <= 110), None)
                desc = ' '.join(w['text'] for w in lw
                                if 110 <= w['x0'] < 345 and not AMOUNT_RE.match(w['text'])
                                and w['text'] != '$')
                # detail: following lines without amounts that start in the description column
                detail_parts = []
                j = i + 1
                while j < len(lines):
                    nl = lines[j]
                    if not nl or nl[0]['x0'] < 100 or nl[0]['x0'] > 200:
                        break
                    if any(AMOUNT_RE.match(w['text']) for w in nl if w['x0'] > 340):
                        break
                    detail_parts.append(' '.join(w['text'] for w in nl))
                    j += 1
                raw_rows.append((day, voucher, desc, amounts, balance,
                                 ' '.join(detail_parts), pageno))

    # debit/credit from the balance delta
    prev = None
    seq = 0
    for day, voucher, desc, amounts, balance, detail, pageno in raw_rows:
        if 'Saldo Inicial' in desc or 'Saldo inicial' in desc:
            prev = balance
            meta["saldo_inicial"] = balance
            continue
        if prev is None:
            prev = balance
            continue
        delta = round(balance - prev, 2)
        debit = round(-delta, 2) if delta < 0 else 0.0
        credit = round(delta, 2) if delta > 0 else 0.0
        warning = not any(abs(a - abs(delta)) < 0.01 for a in amounts) if amounts else True
        seq += 1
        movements.append(BankMovement(
            id=f"{name}#{seq}",
            date=day, voucher=voucher, description=desc, detail=detail,
            debit=debit, credit=credit, balance=balance,
            file=name, page=pageno, warning=warning,
        ))
        prev = balance

    meta["saldo_final"] = movements[-1].balance if movements else meta["saldo_inicial"]
    meta["movimientos"] = movements
    return meta
