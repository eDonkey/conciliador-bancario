# -*- coding: utf-8 -*-
"""Los cruces anulados que se listan son los de ESTA conciliación.

Reportado desde Nave: en la solapa "Excluidos y cruces anulados" de una cuenta
aparecían cruces anulados de otras cuentas y de otras empresas. El veto se
guarda como un par de claves (movimiento, asiento) y vale para todas las
corridas; lo que estaba mal era listarlos todos.
"""
import app as app_mod

MOV_LEAP = {"id": "B#1", "fecha": "2026-09-08", "descripcion": "CUPONES PRIS",
            "debito": 119432.99, "credito": 0, "comprobante": ""}
ASIENTO_LEAP = {"id": "E#1", "hoja": "E", "asiento": "991592", "fecha": "2026-09-08",
                "debe": 1623414.07, "haber": 0}
MOV_AJENO = {"id": "B#9", "fecha": "2026-09-04", "descripcion": "Deposito echeq otro banco 48hs",
             "debito": 0, "credito": 8471412.68, "comprobante": "000103630"}
ASIENTO_AJENO = {"id": "O#3", "hoja": "O", "asiento": "983354", "fecha": "2026-09-03",
                 "debe": 8471412.68, "haber": 0}

JOB = {
    "banco_sin_contabilizar": [MOV_LEAP],
    "gastos_bancarios": [], "retenciones": [],
    "e_sin_banco": [ASIENTO_LEAP], "o_pendientes_sin_banco": [],
    "conciliados_e": [], "conciliados_o": [], "conciliados_manual": [],
    "contraasientos": [], "excluidos_mayor": [], "nota_debito": [],
    "retencion_periodos": [],
}


def veto(mov, asiento):
    return {"mov": app_mod._clave_mov_dict(mov),
            "asiento": app_mod._clave_asiento_dict(asiento)}


def test_solo_los_de_esta_conciliacion(monkeypatch):
    monkeypatch.setattr(app_mod, "_obtener", lambda job_id: JOB)
    todos = [veto(MOV_LEAP, ASIENTO_LEAP), veto(MOV_AJENO, ASIENTO_AJENO)]
    quedan = app_mod._vetos_del_job(todos, "elquesea")
    assert quedan == [todos[0]]


def test_el_veto_tiene_que_coincidir_de_los_dos_lados(monkeypatch):
    monkeypatch.setattr(app_mod, "_obtener", lambda job_id: JOB)
    # el movimiento es de este job pero el asiento es de otra cuenta
    cruzado = veto(MOV_LEAP, ASIENTO_AJENO)
    assert app_mod._vetos_del_job([cruzado], "elquesea") == []


def test_sin_job_no_se_filtra_nada(monkeypatch):
    """Si el job no está (se borró el archivo), mejor mostrarlos que esconderlos."""
    monkeypatch.setattr(app_mod, "_obtener", lambda job_id: None)
    todos = [veto(MOV_LEAP, ASIENTO_LEAP), veto(MOV_AJENO, ASIENTO_AJENO)]
    assert app_mod._vetos_del_job(todos, "borrado") == todos
