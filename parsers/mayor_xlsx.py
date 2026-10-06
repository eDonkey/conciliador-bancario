# -*- coding: utf-8 -*-
"""Parser del libro mayor exportado del sistema contable (FES) en Excel.

Espera un libro con hojas cuyo nombre contenga "E" (cuenta extracto/confirmados)
y "O" (cuenta operativa/transitoria). Cada hoja tiene un encabezado
'Asiento | Fecha | Referencia | Comentario | Debe | Haber | Saldo | Centro de Costo'.
"""
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

import openpyxl


@dataclass
class AsientoMayor:
    id: str
    sheet: str           # 'E' u 'O'
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


def _clasificar_hoja(nombre: str) -> str | None:
    """Determina si la hoja es la cuenta E o la O por su nombre."""
    limpio = re.sub(r'[^A-Z]', ' ', nombre.upper()).split()
    if not limpio:
        return None
    # busca una 'E' u 'O' aislada al final del nombre (p.ej. "Santander E")
    for token in reversed(limpio):
        if token == 'E':
            return 'E'
        if token == 'O':
            return 'O'
    return None


def parse_mayor(source) -> dict:
    """Parsea el Excel del mayor. `source` puede ser una ruta o bytes.

    Devuelve {'E': {'saldo_inicial': float, 'asientos': [...]},
              'O': {...}, 'hojas': [nombres]}
    """
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)

    resultado = {"hojas": wb.sheetnames}
    for nombre in wb.sheetnames:
        clave = _clasificar_hoja(nombre)
        if clave is None or clave in resultado:
            continue
        ws = wb[nombre]
        rows = list(ws.iter_rows(values_only=True))
        hdr = None
        saldo_inicial = None
        for i, r in enumerate(rows):
            if r and any(c == 'Saldo Inicial' for c in r if isinstance(c, str)):
                nums = [c for c in r if isinstance(c, (int, float))]
                if nums:
                    saldo_inicial = float(nums[0])
            if r and r[0] == 'Asiento':
                hdr = i
                break
        if hdr is None:
            continue
        asientos = []
        seq = 0
        for r in rows[hdr + 1:]:
            if not r or not isinstance(r[1], datetime):
                continue
            seq += 1
            asientos.append(AsientoMayor(
                id=f"{clave}#{seq}",
                sheet=clave,
                number=r[0],
                date=r[1].date(),
                reference=str(r[2] or ''),
                comment=str(r[3] or ''),
                debit=round(float(r[4] or 0.0), 2),
                credit=round(float(r[5] or 0.0), 2),
            ))
        resultado[clave] = {"nombre_hoja": nombre, "saldo_inicial": saldo_inicial,
                            "asientos": asientos}
    return resultado
