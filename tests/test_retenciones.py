# -*- coding: utf-8 -*-
"""SIRCREB: la retención de ingresos brutos que el banco descuenta de cada
acreditación. No va en la nota de débito mensual (ahí van comisiones, IVA e
impuestos): se registra con un recibo aparte, porque después se recupera.

Los números son los de una conciliación real de septiembre 2026 (BBVA
109-064858/9): 13 retenciones, 16.210,58 en total, y un asiento del mayor por
16.210,58 que es el recibo.
"""
from datetime import date

from engine.matcher import conciliar
from parsers.mayor_xlsx import AsientoMayor
from parsers.santander_pdf import MovimientoBanco

RETENCIONES = [
    (date(2026, 9, 3), "REG REC SIRC 02/09/26 00002C", 1645.84),
    (date(2026, 9, 9), "REG REC SIRC 08/09/26 00003C", 1765.14),
    (date(2026, 9, 14), "REG REC SIRC 11/09/26 00001C", 1152.39),
    (date(2026, 9, 15), "REG REC SIRC 14/09/26 00004C", 770.00),
    (date(2026, 9, 17), "REG REC SIRC 16/09/26 00002C", 850.00),
    (date(2026, 9, 21), "REG REC SIRC 18/09/26 00003C", 850.00),
    (date(2026, 9, 21), "REG REC SIRC 18/09/26 00002C", 1670.01),
    (date(2026, 9, 23), "REG REC SIRC 22/09/26 00002C", 1545.11),
    (date(2026, 9, 24), "REG REC SIRC 23/09/26 00004C", 1364.46),
    (date(2026, 9, 24), "REG REC SIRC 23/09/26 00003C", 100.00),
    (date(2026, 9, 25), "REG REC SIRC 24/09/26 00005C", 775.49),
    (date(2026, 9, 28), "REG REC SIRC 25/09/26 00006C", 1742.89),
    (date(2026, 9, 30), "REG REC SIRC 29/09/26 00002C", 1979.25),
]
TOTAL = 16210.58


def mov(i, fecha, descripcion, debito=0.0, credito=0.0):
    return MovimientoBanco(id=f"B#{i}", fecha=fecha, comprobante=None,
                           descripcion=descripcion, detalle="", debito=debito,
                           credito=credito, saldo=0.0, archivo="BBVA", pagina=0)


def movimientos():
    return [mov(i, f, d, debito=imp)
            for i, (f, d, imp) in enumerate(RETENCIONES, 1)]


def asiento(n, fecha, referencia, comentario, debe=0.0, haber=0.0, hoja="E"):
    return AsientoMayor(id=f"{hoja}#{n}", hoja=hoja, asiento=n, fecha=fecha,
                        referencia=referencia, comentario=comentario,
                        debe=debe, haber=haber)


RECIBO = asiento(999921, date(2026, 9, 30), "PO 03513",
                 "RC-X-0099-00034263 / PO 03513 / LEAP - BANCO FRANCES",
                 haber=TOTAL)


def test_el_mes_cruza_contra_el_recibo_del_mayor():
    r = conciliar(movimientos(), [RECIBO], [])
    assert r["resumen"]["retenciones"] == {"cantidad": 13, "importe": TOTAL}
    p = r["retencion_periodos"][0]
    assert p["periodo"] == "SEP/26"
    assert p["total_extracto"] == TOTAL and p["total_mayor"] == TOTAL
    assert [a.asiento for a in p["asientos_mayor"]] == [999921]
    # el recibo deja de figurar como asiento pendiente de conciliar...
    assert r["e_sin_banco"] == []
    # ...y las retenciones no quedan como pendientes de contabilizar
    assert r["banco_sin_contabilizar"] == []


def test_sin_recibo_quedan_como_pendientes_de_contabilizar():
    r = conciliar(movimientos(), [], [])
    assert r["retencion_periodos"][0]["total_mayor"] is None
    assert len(r["banco_sin_contabilizar"]) == 13
    assert r["resumen"]["retenciones"]["importe"] == TOTAL


def test_no_entran_en_la_nota_de_debito():
    # una comisión con su IVA, la ND del mayor por esos dos, y las retenciones
    gastos = [mov(90, date(2026, 9, 1), "COMISION MAN 08/26", debito=111200.0),
              mov(91, date(2026, 9, 1), "IVA TASA GRA", debito=23352.0)]
    nd = asiento(998466, date(2026, 9, 30), "PO 03513",
                 "30092026-Gastos Bancarios BBVA 64858-9", haber=134552.0)
    r = conciliar(movimientos() + gastos, [nd, RECIBO], [])
    assert r["resumen"]["gastos_bancarios"] == {"cantidad": 2, "importe": 134552.0}
    # la ND cierra en cero: antes le sobraban los 16.210,58 de las retenciones
    assert r["nota_debito"][0]["diferencia"] == 0.0
    assert r["retencion_periodos"][0]["total_mayor"] == TOTAL


def test_dos_asientos_por_el_mismo_total_no_se_eligen_a_dedo():
    otro = asiento(999922, date(2026, 9, 30), "PO 03599", "otro por el mismo importe",
                   haber=TOTAL)
    r = conciliar(movimientos(), [RECIBO, otro], [])
    p = r["retencion_periodos"][0]
    assert p["total_mayor"] is None and p["ambiguo"] is True
    assert len(r["e_sin_banco"]) == 2          # los dos siguen pendientes
    assert len(r["banco_sin_contabilizar"]) == 13


def test_un_asiento_por_otro_importe_no_las_explica():
    otro = asiento(999923, date(2026, 9, 30), "PO 03599", "no es el recibo",
                   haber=20000.0)
    r = conciliar(movimientos(), [otro], [])
    assert r["retencion_periodos"][0]["total_mayor"] is None
    assert [a.asiento for a in r["e_sin_banco"]] == [999923]


def test_el_recibo_del_mes_siguiente_tambien_vale():
    tarde = asiento(999924, date(2026, 10, 8), "PO 03520", "recibo de sircreb",
                    haber=TOTAL)
    r = conciliar(movimientos(), [tarde], [])
    assert r["retencion_periodos"][0]["total_mayor"] == TOTAL


def test_una_retencion_que_cruza_contra_su_propio_asiento_no_llega_aca():
    # si el mayor tiene el asiento 1 a 1, se concilia como cualquier otro
    uno = [mov(1, date(2026, 9, 3), "REG REC SIRC 02/09/26 00002C", debito=1645.84)]
    propio = asiento(5, date(2026, 9, 3), "SIRC", "retencion sircreb", haber=1645.84)
    r = conciliar(uno, [propio], [])
    assert r["resumen"]["conciliados_e"] == 1
    assert r["resumen"]["retenciones"]["cantidad"] == 0
