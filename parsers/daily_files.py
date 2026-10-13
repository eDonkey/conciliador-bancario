# -*- coding: utf-8 -*-
"""Parsers of the Nave group's daily multi-bank statements and of the FBS
accounting system's reports (ledger E and O per account).

`identify(name, data)` tells by itself what each file is:
  - a Santander/Río statement (an "xls" that is really tab-separated text),
    BBVA/Francés (binary .xls), Galicia (.xlsx), Macro (binary .xls)
    or Ciudad (.csv with ';'),
  - or an FBS ledger (binary .xls, sheet "(E) (codigo)" / "(O) (codigo)").

Returns the metadata + the already parsed movements/entries. The account is
reported as the digits found inside the file (Galicia has none: it is left
for manual assignment).
"""
import html
import io
import re
from datetime import date, datetime, timedelta

import xlrd

from parsers.santander_pdf import BankMovement
from parsers.ledger_xlsx import LedgerEntry

EXCEL_EPOCH = date(1899, 12, 30)
FBS_SHEET_RE = re.compile(r'\((E|O)\)\s*\((\d+)\)')


def _digits(s: str) -> str:
    return re.sub(r'\D', '', s or '')


def _ar_amount(s: str) -> float:
    """'1.850.205,68' / '(467.935,00)' / '-26107,00' -> signed float."""
    s = (s or '').strip()
    neg = s.startswith('(') or s.startswith('-')
    v = s.strip('()').lstrip('-').replace('.', '').replace(',', '.')
    return -float(v) if neg and v else (float(v) if v else 0.0)


def _parse_date(s):
    """Date from 'dd/mm/yyyy', 'dd-mm-yyyy', a datetime or an Excel serial."""
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    if isinstance(s, float) and 20000 < s < 80000:   # plausible Excel serial
        return EXCEL_EPOCH + timedelta(days=int(s))
    m = re.match(r'(\d{2})[/-](\d{2})[/-](\d{4})', str(s or ''))
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', str(s or ''))
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _movement(seq, file, day, description, amount, balance=0.0,
              voucher=None, detail=""):
    return BankMovement(
        id=f"B#{seq}", date=day, voucher=voucher,
        description=description.strip(), detail=(detail or '').strip(),
        debit=round(-amount, 2) if amount < 0 else 0.0,
        credit=round(amount, 2) if amount > 0 else 0.0,
        balance=balance, file=file, page=0)


# --- Santander / Río: tab-separated text disguised as .xls ------------------

def _is_rio(data: bytes) -> bool:
    """Santander/Río tab-separated text with a 'Concepto' header. Two real
    variants: the classic one (starts with an empty line) and the
    'descargaUltimosMovimientos' export (starts right away with 'Movimientos
    del Día' and has two sections: the day's + the latest movements)."""
    if b'\tConcepto\t' not in data[:2000].replace(b'\r', b''):
        return False
    start = data[:400].decode('latin-1', errors='replace')
    return data[:2] in (b'\r\n', b'\n\r') or start.lstrip().startswith('Movimientos')


def _parse_rio(name, data):
    text = html.unescape(data.decode('latin-1', errors='replace'))
    # case-insensitive: the new export says "Cuenta corriente" in lowercase
    m = re.search(r'Cuenta Corriente en (Pesos|D[oó]lares)\s+Nro\.\s*([\d/-]+)',
                  text, re.IGNORECASE)
    currency = 'USD' if m and 'lares' in m.group(1) else 'ARS'
    account = _digits(m.group(2)) if m else None
    movs, seq = [], 0
    for line in text.splitlines():
        c = line.split('\t')
        if len(c) < 8 or not re.match(r'\d{2}/\d{2}/\d{4}', c[0]):
            continue
        seq += 1
        desc, _, det = c[5].partition('  - ')
        movs.append(_movement(seq, name, _parse_date(c[0]), desc, _ar_amount(c[6]),
                              _ar_amount(c[7]), voucher=c[4].strip() or None,
                              detail=det))
    return {"banco": "santander", "cuenta": account, "moneda": currency,
            "movimientos": movs}


# --- Ciudad: latin-1 CSV with ';' --------------------------------------------

def _parse_ciudad(name, data):
    text = data.decode('latin-1', errors='replace')
    movs, seq = [], 0
    account = currency = None
    for line in text.splitlines()[1:]:
        c = [x.strip() for x in line.split(';')]
        if len(c) < 7 or not re.match(r'\d{2}/\d{2}/\d{4}', c[2]):
            continue
        if account is None:
            account = _digits(c[0]).lstrip('0')
            currency = 'USD' if 'U$S' in c[0].upper() else 'ARS'
        seq += 1
        movs.append(_movement(seq, name, _parse_date(c[2]), c[5], _ar_amount(c[3]),
                              _ar_amount(c[6]), voucher=c[4] or None))
    return {"banco": "ciudad", "cuenta": account, "moneda": currency,
            "movimientos": movs}


# --- Galicia: .xlsx, sheet 'Movimientos' (no account number inside) ---------

def _parse_galicia(name, data):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb['Movimientos'] if 'Movimientos' in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr = {str(v).strip(): i for i, v in enumerate(rows[0]) if v}
    movs = []
    for seq, f in enumerate(rows[1:], start=1):
        day = _parse_date(f[hdr['Fecha']])
        if day is None:
            continue
        deb = float(f[hdr['Débitos']] or 0)
        cre = float(f[hdr['Créditos']] or 0)
        detail = ' '.join(str(f[hdr[k]]).strip() for k in
                          ('Concepto', 'Leyendas Adicionales 1', 'Leyendas Adicionales 2')
                          if k in hdr and f[hdr[k]])
        movs.append(_movement(seq, name, day, str(f[hdr['Descripción']] or ''),
                              cre - deb, float(f[hdr['Saldo']] or 0),
                              voucher=str(f[hdr['Número de Comprobante']] or '') or None,
                              detail=detail))
    return {"banco": "galicia", "cuenta": None, "moneda": None,
            "movimientos": movs}


# --- BBVA / Francés: binary .xls --------------------------------------------

def _parse_bbva(name, wb):
    account = currency = None
    movs = []
    seq = 0
    for sheet in wb.sheets():
        rows = [[sheet.cell_value(r, c) for c in range(sheet.ncols)]
                for r in range(sheet.nrows)]
        hdr_i = None
        for i, f in enumerate(rows):
            if str(f[0]).startswith('Cuenta:') and len(f) > 1:
                m = re.search(r'([\d/-]+)\s*\(CC\s*(U\$S|\$)\)', str(f[1]))
                if m:
                    account = _digits(m.group(1))
                    currency = 'USD' if m.group(2) == 'U$S' else 'ARS'
            if f[0] == 'Fecha' and 'Concepto' in f:
                hdr_i = i
                cols = {str(v).strip(): j for j, v in enumerate(f) if str(v).strip()}
                continue
            if hdr_i is None or not _parse_date(f[0]):
                continue
            day = _parse_date(f[0])
            cre = abs(float(f[cols['Crédito']] or 0)) if f[cols['Crédito']] != '' else 0.0
            deb = abs(float(f[cols['Débito']] or 0)) if f[cols['Débito']] != '' else 0.0
            voucher_col = next((cols[k] for k in ('Nro de cheque', 'Número Documento')
                                if k in cols), None)
            voucher = str(f[voucher_col] or '') if voucher_col is not None else ''
            detail = str(f[cols['Detalle']] or '') if 'Detalle' in cols else ''
            # No deduplication: identical rows (e.g. two equal taxes the same
            # day) are real operations and are all kept.
            seq += 1
            movs.append(_movement(seq, name, day, str(f[cols['Concepto']]),
                                  cre - deb, voucher=voucher.strip() or None,
                                  detail=detail))
    return {"banco": "frances", "cuenta": account, "moneda": currency,
            "movimientos": movs}


# --- Macro: binary .xls -----------------------------------------------------

def _parse_macro(name, wb):
    sheet = wb.sheets()[0]
    rows = [[sheet.cell_value(r, c) for c in range(sheet.ncols)]
            for r in range(sheet.nrows)]
    account = currency = None
    cols = None
    movs, seq = [], 0
    for f in rows:
        cells = [str(v).strip() for v in f]
        if cells[0] == 'Número':
            account = _digits(next((v for v in cells[1:] if _digits(v)), ''))
        elif cells[0] == 'Moneda':
            currency = 'USD' if any('DOLAR' in v.upper() for v in cells[1:]) else 'ARS'
        elif cells[0] == 'Fecha' and 'Causal' in cells:
            cols = {v: i for i, v in enumerate(cells) if v}
        elif cols and isinstance(f[0], float):
            seq += 1
            movs.append(_movement(seq, name, _parse_date(f[0]),
                                  str(f[cols['Concepto']] or ''),
                                  float(f[cols['Importe']] or 0),
                                  float(f[cols['Saldo']] or 0),
                                  voucher=str(f[cols['Nro. de Referencia']] or '') or None,
                                  detail=str(f[cols['Causal']] or '')))
    return {"banco": "macro", "cuenta": account, "moneda": currency,
            "movimientos": movs}


# --- Santander exported to .xlsx (same columns as the tab-separated text) ---

ACCOUNT_TEXT_RE = re.compile(r'\b(\d{3})[-. ](\d{1,6})[-/.](\d)\b')


def _parse_rio_xlsx(name, wb):
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    currency = None
    hdr = None
    movs, seq = [], 0
    for f in rows:
        c0 = str(f[0] or '')
        if 'Cuenta corriente en' in c0 or 'Cuenta Corriente en' in c0:
            currency = 'USD' if 'lares' in c0 or 'U$S' in c0.upper() else 'ARS'
        elif c0 == 'Fecha' and 'Cod. Operativo' in [str(x).strip() for x in f]:
            hdr = {str(x).strip(): i for i, x in enumerate(f) if x}
        elif hdr and _parse_date(f[0]) and not c0.startswith('Saldo'):
            seq += 1
            desc, _, det = str(f[hdr['Concepto']] or '').partition('  - ')
            balance = f[hdr.get('Saldo', -1)] if 'Saldo' in hdr else 0
            movs.append(_movement(seq, name, _parse_date(f[hdr['Fecha']]), desc,
                                  float(str(f[hdr['Importe']] or 0).replace(',', '')),
                                  float(str(balance or 0).replace(',', '') or 0),
                                  voucher=str(f[hdr['Referencia']] or '') or None))
    # the account number isn't inside: it's in the sheet/file name
    m = ACCOUNT_TEXT_RE.search(f"{ws.title} {name}")
    account = '-'.join(m.groups()) if m else None
    if currency is None and re.search(r'CC\s*\$', f"{ws.title} {name}"):
        currency = 'ARS'
    return {"banco": "santander", "cuenta": account, "moneda": currency,
            "movimientos": movs}


# --- FBS: Consulta del Mayor (sheet E or O) ---------------------------------

def _parse_fbs(name, wb):
    sheet = wb.sheets()[0]
    m = FBS_SHEET_RE.search(sheet.name)
    letter = m.group(1) if m else None
    code = m.group(2) if m else None
    internal_name = re.search(r'Cuenta:\s*([^\n(]+)', sheet.name)
    date_range = re.search(r'(\d{2}/\d{2}/\d{4})\s+AL\s+(\d{2}/\d{2}/\d{4})', sheet.name)
    entries, seq = [], 0
    for r in range(sheet.nrows):
        v = [sheet.cell_value(r, c) for c in range(min(sheet.ncols, 8))]
        if not isinstance(v[1], float) or str(v[0]).startswith('Saldo'):
            continue
        seq += 1
        entries.append(LedgerEntry(
            id=f"{letter or 'E'}#{seq}", sheet=letter or 'E',
            number=int(v[0]) if isinstance(v[0], float) else str(v[0]),
            date=_parse_date(v[1]), reference=str(v[2] or '').strip(),
            comment=str(v[3] or '').strip(),
            debit=round(float(v[4] or 0.0), 2),
            credit=round(float(v[5] or 0.0), 2)))
    return {"tipo": "fbs", "hoja": letter, "codigo_fbs": code,
            "codigos": {letter or "E": code} if code else {},
            "nombre_fbs": (internal_name.group(1).strip() if internal_name else sheet.name[:60]),
            "desde": _parse_date(date_range.group(1)) if date_range else None,
            "hasta": _parse_date(date_range.group(2)) if date_range else None,
            "asientos": entries}


def _parse_fbs_xlsx(name, wb):
    """FBS exported to .xlsx: it can carry sheets E and O in the same file
    ("Hoja E"/"Hoja O") and no account code; the identifier left for the
    mapping is the internal name ("Cuenta: CIT - BANCO SANTANDER...")."""
    entries, sheets = [], []
    codes = {}
    fbs_name = None
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        text0 = ' '.join(str(v) for f in rows[:8] for v in f if v)
        if 'Consulta del Mayor' not in text0:
            continue
        m = FBS_SHEET_RE.search(ws.title) or FBS_SHEET_RE.search(text0)
        letter = m.group(1) if m else None
        if letter is None:
            t = re.search(r'\b(E|O)\b', ws.title.upper())
            letter = t.group(1) if t else ('O' if not sheets else 'E')
        if m:
            codes[letter] = m.group(2)
        sheets.append(letter)
        for f in rows[:8]:
            c0 = str(f[0] or '')
            if c0.startswith('Cuenta:'):
                raw = c0.split('Cuenta:', 1)[1].strip()
                fbs_name = re.split(r'\s*\((?:E|O)\)', raw)[0].strip()[:80]
        seq = 0
        for f in rows:
            day = _parse_date(f[1])
            if day is None or str(f[0] or '').startswith('Saldo') or f[0] is None:
                continue
            seq += 1
            entries.append(LedgerEntry(
                id=f"{letter}#{seq}", sheet=letter,
                number=str(f[0]).strip(), date=day,
                reference=str(f[2] or '').strip(),
                comment=str(f[3] or '').strip(),
                debit=round(float(str(f[4] or 0).replace(',', '') or 0), 2),
                credit=round(float(str(f[5] or 0).replace(',', '') or 0), 2)))
    return {"tipo": "fbs", "hoja": ''.join(sorted(set(sheets))),
            "codigo_fbs": next(iter(codes.values()), None), "codigos": codes,
            "nombre_fbs": fbs_name, "asientos": entries}


# --- identification ---------------------------------------------------------

def identify(name: str, data: bytes) -> dict:
    """Detects and parses a file. Always returns a dict with 'tipo'
    ('extracto' | 'fbs' | 'desconocido') and, depending on the case, the
    movements or entries, bank, account (digits), currency, FBS sheet/code, range."""
    try:
        if data[:4] == b'\xd0\xcf\x11\xe0':          # OLE2: a real binary xls
            wb = xlrd.open_workbook(file_contents=data)
            sheet_name = wb.sheets()[0].name
            text0 = ' '.join(str(wb.sheets()[0].cell_value(r, c))
                             for r in range(min(wb.sheets()[0].nrows, 8))
                             for c in range(min(wb.sheets()[0].ncols, 4)))
            if FBS_SHEET_RE.search(sheet_name) or 'Consulta del Mayor' in text0:
                out = _parse_fbs(name, wb)
            elif any('CUENTA CORRIENTE BANCARIA' in str(wb.sheets()[0].cell_value(r, 0))
                     for r in range(min(wb.sheets()[0].nrows, 6))) or 'Causal' in text0:
                out = {"tipo": "extracto", **_parse_macro(name, wb)}
            else:
                out = {"tipo": "extracto", **_parse_bbva(name, wb)}
        elif data[:2] == b'PK':                      # xlsx: FBS, Galicia or Santander
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                        data_only=True)
            cells0 = ' '.join(str(v) for ws in wb.worksheets
                              for f in list(ws.iter_rows(values_only=True))[:8]
                              for v in f if v)
            if 'Consulta del Mayor' in cells0:
                out = _parse_fbs_xlsx(name, wb)
            elif 'Grupo de Conceptos' in cells0 or 'Movimientos' in wb.sheetnames:
                out = {"tipo": "extracto", **_parse_galicia(name, data)}
            elif 'Cod. Operativo' in cells0:
                out = {"tipo": "extracto", **_parse_rio_xlsx(name, wb)}
            else:
                return {"tipo": "desconocido", "error":
                        "Excel no reconocido: no parece Galicia, Santander ni FBS"}
        elif _is_rio(data):
            out = {"tipo": "extracto", **_parse_rio(name, data)}
        elif data[:6].decode('latin-1', errors='replace').startswith('Cuenta'):
            out = {"tipo": "extracto", **_parse_ciudad(name, data)}
        else:
            return {"tipo": "desconocido", "error": "Formato no reconocido"}
    except Exception as exc:  # noqa: BLE001 — the error is shown per file
        return {"tipo": "desconocido", "error": str(exc)}

    items = out.get("movimientos") or out.get("asientos") or []
    dates = [x.date for x in items if x.date]
    out.setdefault("desde", min(dates) if dates else None)
    out.setdefault("hasta", max(dates) if dates else None)
    out["cantidad"] = len(items)
    out["archivo"] = name
    return out
