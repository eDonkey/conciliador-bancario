# -*- coding: utf-8 -*-
"""Parser of the general ledger exported from the accounting system (FES) as Excel.

Expects a workbook with sheets whose name contains "E" (statement/confirmed
account) and "O" (operative/transitory account). Each sheet has a header
'Asiento | Fecha | Referencia | Comentario | Debe | Haber | Saldo | Centro de Costo'.
"""
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

import openpyxl


@dataclass
class LedgerEntry:
    id: str
    sheet: str           # 'E' or 'O'
    number: int | str
    date: date | None
    reference: str
    comment: str
    debit: float
    credit: float

    @property
    def amount(self) -> float:
        return self.debit if self.debit else self.credit

    @property
    def side(self) -> str:
        return "debe" if self.debit else "haber"

    def to_dict(self):
        return {
            "id": self.id,
            "hoja": self.sheet,
            "asiento": self.number,
            "fecha": self.date.isoformat() if self.date else None,
            "referencia": self.reference,
            "comentario": self.comment,
            "debe": self.debit,
            "haber": self.credit,
        }


def _classify_sheet(name: str) -> str | None:
    """Tells from its name whether the sheet is account E or O."""
    clean = re.sub(r'[^A-Z]', ' ', name.upper()).split()
    if not clean:
        return None
    # looks for a standalone 'E' or 'O' at the end of the name (e.g. "Santander E")
    for token in reversed(clean):
        if token == 'E':
            return 'E'
        if token == 'O':
            return 'O'
    return None


def parse_ledger(source) -> dict:
    """Parses the ledger Excel. `source` can be a path or bytes.

    Returns {'E': {'saldo_inicial': float, 'asientos': [...]},
             'O': {...}, 'hojas': [names]}
    """
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)

    result = {"hojas": wb.sheetnames}
    for name in wb.sheetnames:
        key = _classify_sheet(name)
        if key is None or key in result:
            continue
        ws = wb[name]
        rows = list(ws.iter_rows(values_only=True))
        hdr = None
        opening_balance = None
        for i, r in enumerate(rows):
            if r and any(c == 'Saldo Inicial' for c in r if isinstance(c, str)):
                nums = [c for c in r if isinstance(c, (int, float))]
                if nums:
                    opening_balance = float(nums[0])
            if r and r[0] == 'Asiento':
                hdr = i
                break
        if hdr is None:
            continue
        entries = []
        seq = 0
        for r in rows[hdr + 1:]:
            if not r or not isinstance(r[1], datetime):
                continue
            seq += 1
            entries.append(LedgerEntry(
                id=f"{key}#{seq}",
                sheet=key,
                number=r[0],
                date=r[1].date(),
                reference=str(r[2] or ''),
                comment=str(r[3] or ''),
                debit=round(float(r[4] or 0.0), 2),
                credit=round(float(r[5] or 0.0), 2),
            ))
        result[key] = {"nombre_hoja": name, "saldo_inicial": opening_balance,
                       "asientos": entries}
    return result
