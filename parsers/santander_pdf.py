# -*- coding: utf-8 -*-
"""Parser de extractos de cuenta corriente de Banco Santander Argentina (PDF).

Extrae los movimientos de la tabla "Movimientos" usando las posiciones de las
palabras en la página. El débito/crédito se determina por la diferencia de
saldos (delta), que es infalible; el monto impreso se usa solo para validar.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import pdfplumber

AMOUNT_RE = re.compile(r'^-?[\d.]+,\d{2}$')
DATE_RE = re.compile(r'^\d{2}/\d{2}/\d{2}$')
STOP_RE = re.compile(r'^Saldo total \$')


@dataclass
class MovimientoBanco:
    id: str
    fecha: date | None
    comprobante: str | None
    descripcion: str
    detalle: str
    debito: float
    credito: float
    saldo: float
    archivo: str
    pagina: int
    advertencia: bool = False  # el monto impreso no coincide con el delta de saldo

    @property
    def importe(self) -> float:
        return self.credito if self.credito else self.debito

    @property
    def lado(self) -> str:
        return "credito" if self.credito else "debito"

    def to_dict(self):
        return {
            "id": self.id,
            "fecha": self.fecha.isoformat() if self.fecha else None,
            "comprobante": self.comprobante,
            "descripcion": self.descripcion,
            "detalle": self.detalle,
            "debito": self.debito,
            "credito": self.credito,
            "saldo": self.saldo,
            "archivo": self.archivo,
            "advertencia": self.advertencia,
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
    """Agrupa palabras en líneas visuales por coordenada vertical."""
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


def parse_extracto(path: str, nombre_archivo: str | None = None) -> dict:
    """Parsea un extracto PDF. Devuelve dict con metadatos y movimientos."""
    nombre = nombre_archivo or str(path)
    movimientos: list[MovimientoBanco] = []
    meta = {"archivo": nombre, "titular": None, "cuenta": None,
            "desde": None, "hasta": None, "saldo_inicial": None, "saldo_final": None}
    raw_rows = []  # (fecha, comp, desc_words, amounts<=500, saldo, pagina, line_idx)
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
                saldo_words = [w for w in lw if w['x0'] > 500 and AMOUNT_RE.match(w['text'])]
                if not saldo_words:
                    continue
                saldo = _parse_amount(saldo_words[-1]['text'])
                amounts = [_parse_amount(w['text']) for w in lw
                           if AMOUNT_RE.match(w['text']) and w['x0'] <= 500]
                fecha = None
                for cand in (lw,
                             lines[i + 1] if i + 1 < len(lines) else [],
                             lines[i - 1] if i > 0 else []):
                    for w in cand:
                        if w['x0'] < 60 and DATE_RE.match(w['text']):
                            fecha = _parse_date(w['text'])
                            break
                    if fecha:
                        break
                comp = next((w['text'] for w in lw if 60 <= w['x0'] <= 110), None)
                desc = ' '.join(w['text'] for w in lw
                                if 110 <= w['x0'] < 345 and not AMOUNT_RE.match(w['text'])
                                and w['text'] != '$')
                # detalle: líneas siguientes sin montos que empiezan en la columna descripción
                detalle_parts = []
                j = i + 1
                while j < len(lines):
                    nl = lines[j]
                    if not nl or nl[0]['x0'] < 100 or nl[0]['x0'] > 200:
                        break
                    if any(AMOUNT_RE.match(w['text']) for w in nl if w['x0'] > 340):
                        break
                    detalle_parts.append(' '.join(w['text'] for w in nl))
                    j += 1
                raw_rows.append((fecha, comp, desc, amounts, saldo,
                                 ' '.join(detalle_parts), pageno))

    # débito/crédito por delta de saldo
    prev = None
    seq = 0
    for fecha, comp, desc, amounts, saldo, detalle, pageno in raw_rows:
        if 'Saldo Inicial' in desc or 'Saldo inicial' in desc:
            prev = saldo
            meta["saldo_inicial"] = saldo
            continue
        if prev is None:
            prev = saldo
            continue
        delta = round(saldo - prev, 2)
        debito = round(-delta, 2) if delta < 0 else 0.0
        credito = round(delta, 2) if delta > 0 else 0.0
        advertencia = not any(abs(a - abs(delta)) < 0.01 for a in amounts) if amounts else True
        seq += 1
        movimientos.append(MovimientoBanco(
            id=f"{nombre}#{seq}",
            fecha=fecha, comprobante=comp, descripcion=desc, detalle=detalle,
            debito=debito, credito=credito, saldo=saldo,
            archivo=nombre, pagina=pageno, advertencia=advertencia,
        ))
        prev = saldo

    meta["saldo_final"] = movimientos[-1].saldo if movimientos else meta["saldo_inicial"]
    meta["movimientos"] = movimientos
    return meta
