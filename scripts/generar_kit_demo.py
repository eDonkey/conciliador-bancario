# -*- coding: utf-8 -*-
"""Genera el kit de extractos de demo (ROD-7 "06") y los fixtures del mayor
sembrado que usa el modo demo (ROD-6 "05", ver DEMO_MODE en app.py).

Un solo lugar de verdad por cuenta: cada "movimiento" de acá se traduce a la
vez en (a) una fila del extracto CSV descargable en static/kit-demo/ y (b) el
asiento correspondiente en datos/demo/<cuenta_id>.json — así los dos lados
nunca quedan desincronizados. Las fechas son relativas a HOY (ROD-3 pide que
la demo nunca se vea vieja), así que este script se puede re-correr con
`python scripts/generar_kit_demo.py` para refrescarlas.

IMPORTANTE: el banco/número/moneda de cada cuenta acá tienen que coincidir
exactamente con lo que siembra hub-grupo/scripts/seed-demo.js (mismo banco,
número y moneda ⇒ mismo cuenta_id derivado, ver engine/cuentas.py). Si se
edita uno hay que editar el otro.
"""
import io
import json
import os
import re
import unicodedata
from datetime import date, datetime, timedelta

import openpyxl

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_DIR = os.path.join(BASE_DIR, "datos", "demo")
KIT_DIR = os.path.join(BASE_DIR, "static", "kit-demo")
HOY = date.today()


def hace(dias):
    return HOY - timedelta(days=dias)


def _banco_key(s):
    plano = unicodedata.normalize("NFD", s or "")
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return plano.strip().lower()


def _digits(s):
    return re.sub(r"\D", "", s or "")


def cuenta_id(banco, numero, moneda):
    return f"{_banco_key(banco)}-{_digits(numero)}-{(moneda or '').lower()}"


def _escribir_mayor_xlsx(asientos, ruta):
    """Arma el .xlsx "Consulta del Mayor" que espera parsers/mayor_xlsx.parse_mayor:
    una hoja por cuenta (E/O) con fila de encabezado 'Asiento | Fecha | ...' y
    los asientos abajo. La hoja O queda vacía si no hay asientos de ese lado
    (parse_mayor solo exige que la hoja exista, no que tenga filas)."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for letra, nombre_hoja in (("E", "Cuenta E"), ("O", "Cuenta O")):
        ws = wb.create_sheet(nombre_hoja)
        de_esta_hoja = [a for a in asientos if a["hoja"] == letra]
        if de_esta_hoja:
            ws.append(["Saldo Inicial", None, None, None, 0.0, None])
        ws.append(["Asiento", "Fecha", "Referencia", "Comentario", "Debe", "Haber"])
        for a in de_esta_hoja:
            fecha = datetime.fromisoformat(a["fecha"]) if a["fecha"] else None
            ws.append([a["asiento"], fecha, a["referencia"], a["comentario"],
                       a["debe"], a["haber"]])
    wb.save(ruta)


def _ar(n):
    """float -> '1.850.205,68' (formato argentino, sin signo)."""
    s = f"{abs(n):,.2f}"                      # '1,850,205.68'
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    return s


# ---------------------------------------------------------------------------
# Cuentas del kit (deben existir, con estos mismos datos, en el seed de
# hub-grupo). Cada "mov" es un caso de la demo: normal | aproximado | orfano
# | rechazado | duplicado (dos filas bancarias, un solo asiento).
# ---------------------------------------------------------------------------
CUENTAS = [
    {
        "empresa": "Demo · Rumbo Motors", "banco": "ciudad",
        "numero": "350123456789", "moneda": "ARS",
        "archivo": "rumbo-motors-cuenta-corriente.csv",
        "movs": [
            {"dias": 14, "lado": "in", "banco": 500000.00, "mayor": 500000.00,
             "desc": "Transferencia recibida Cliente A", "comp": "TR001", "ref": "TR001"},
            {"dias": 12, "lado": "out", "banco": 120000.00, "mayor": 120000.00,
             "desc": "Pago proveedor Repuestos SA", "comp": "PG204", "ref": "PG204"},
            {"dias": 10, "lado": "in", "banco": 85000.32, "mayor": 85000.00,
             "desc": "Cobranza Cliente B", "comp": "CB077", "ref": "CB077"},
            {"dias": 8, "lado": "duplicado", "banco": 45000.00, "mayor": 45000.00,
             "desc": "Pago proveedor Insumos XYZ", "comp": "PG205", "ref": "PG205"},
            {"dias": 5, "lado": "in", "banco": 12500.00, "mayor": None,
             "desc": "Interés a favor", "comp": None, "ref": None},
            {"dias": 20, "lado": "rechazado", "banco": None, "mayor": 67000.00,
             "desc": "Cheque Nro 00456123", "comp": None, "ref": "CHQ456123"},
        ],
    },
    {
        "empresa": "Demo · Andina Repuestos", "banco": "ciudad",
        "numero": "350234567891", "moneda": "ARS",
        "archivo": "andina-repuestos-cuenta-corriente.csv",
        "movs": [
            {"dias": 10, "lado": "in", "banco": 300000.00, "mayor": 300000.00,
             "desc": "Transferencia recibida Cliente C", "comp": "TR301", "ref": "TR301"},
            {"dias": 9, "lado": "out", "banco": 60000.00, "mayor": 60000.00,
             "desc": "Pago proveedor Lubricantes SA", "comp": "PG410", "ref": "PG410"},
            {"dias": 6, "lado": "in", "banco": 45000.00, "mayor": 45000.00,
             "desc": "Cobranza Cliente D", "comp": "CB512", "ref": "CB512"},
            {"dias": 3, "lado": "out", "banco": 18500.00, "mayor": 18500.00,
             "desc": "Pago servicios", "comp": "PG413", "ref": "PG413"},
        ],
    },
]


# Resultado real, verificado a mano corriendo /api/diario/identificar +
# /api/diario/conciliar sobre estos mismos archivos (el matcher tiene varias
# pasadas — importe único, importe+fecha, tolerancia — así que el desenlace
# exacto de un caso ambiguo como el duplicado no siempre es el "obvio").
# Si cambiás los importes/fechas de una cuenta, volvé a probarla a mano y
# actualizá el texto de acá.
RESULTADO_VERIFICADO = {
    "rumbo-motors-cuenta-corriente.csv": (
        "- **4 de 6 conciliados automáticamente**: los 2 pagos limpios (transferencia "
        "recibida y pago a Repuestos SA), la cobranza con 32 centavos de diferencia "
        "(método \"importe aproximado\", queda marcada igual) y uno de los dos pagos "
        "duplicados a Insumos XYZ (mismo importe y fecha exacta: el sistema resuelve "
        "uno solo).\n"
        "- **Quedan 2 sin explicar del lado banco** ($57.500 total): el segundo pago "
        "duplicado a Insumos XYZ ($45.000, para mostrar cómo se marca/investiga un "
        "duplicado a mano) y el interés a favor ($12.500, sin asiento — no tiene "
        "contrapartida en el mayor).\n"
        "- **Queda 1 sin explicar del lado mayor** ($67.000): el cheque Nro 00456123, "
        "cargado en el mayor pero nunca acreditado en el banco (rechazado).\n"
    ),
    "andina-repuestos-cuenta-corriente.csv": (
        "- **4 de 4 conciliados automáticamente (100%)** — este archivo es el caso "
        "prolijo: todo cruza solo, sin nada para resolver a mano.\n"
    ),
}


def generar():
    os.makedirs(DEMO_DIR, exist_ok=True)
    os.makedirs(KIT_DIR, exist_ok=True)
    resultados_md = ["# Resultados esperados del kit de demo\n",
                      f"Generado el {HOY.isoformat()} (fechas relativas a hoy; "
                      "volvé a correr `python scripts/generar_kit_demo.py` si se ve vieja).\n"]

    for cta in CUENTAS:
        cid = cuenta_id(cta["banco"], cta["numero"], cta["moneda"])
        filas_csv = []   # (fecha, importe_signed, comprobante, desc, saldo)
        asientos = []    # dicts para el fixture del mayor
        saldo = 1_000_000.00

        for i, m in enumerate(cta["movs"], 1):
            fecha = hace(m["dias"])
            if m["lado"] == "duplicado":
                # dos movimientos bancarios idénticos, un solo asiento: queda
                # ambiguo a propósito (se resuelve a mano en la demo).
                for suf in ("A", "B"):
                    saldo -= m["banco"]
                    filas_csv.append((fecha, -m["banco"], f'{m["comp"]}{suf}', m["desc"], saldo))
                asientos.append({"hoja": "E", "asiento": 4000 + i, "fecha": fecha.isoformat(),
                                  "referencia": m["ref"], "comentario": m["desc"],
                                  "debe": 0.0, "haber": m["mayor"]})
                continue
            if m["lado"] == "rechazado":
                # solo mayor: el cheque nunca se acreditó en el banco.
                asientos.append({"hoja": "E", "asiento": 4000 + i, "fecha": fecha.isoformat(),
                                  "referencia": m["ref"], "comentario": m["desc"],
                                  "debe": 0.0, "haber": m["mayor"]})
                continue
            signo = 1 if m["lado"] == "in" else -1
            saldo += signo * m["banco"]
            filas_csv.append((fecha, signo * m["banco"], m["comp"], m["desc"], saldo))
            if m["mayor"] is None:
                continue   # huérfano: sin asiento del lado del mayor
            # in (crédito banco) <-> debe mayor; out (débito banco) <-> haber mayor
            debe = m["mayor"] if m["lado"] == "in" else 0.0
            haber = m["mayor"] if m["lado"] == "out" else 0.0
            asientos.append({"hoja": "E", "asiento": 4000 + i, "fecha": fecha.isoformat(),
                              "referencia": m["ref"], "comentario": m["desc"],
                              "debe": debe, "haber": haber})

        # --- extracto CSV formato "Ciudad" (ver parsers/diarios._parse_ciudad) ---
        marca_moneda = "U$S" if cta["moneda"] == "USD" else "$"
        buf = io.StringIO()
        buf.write("Cuenta;Sucursal;Fecha;Importe;Comprobante;Descripcion;Saldo\r\n")
        for fecha, importe, comp, desc, saldo_fila in sorted(filas_csv, key=lambda r: r[0]):
            buf.write(f'{cta["numero"]} {marca_moneda};0001;{fecha.strftime("%d/%m/%Y")};'
                      f'{("-" if importe < 0 else "") + _ar(importe)};{comp or ""};{desc};'
                      f'{_ar(saldo_fila)}\r\n')
        with open(os.path.join(KIT_DIR, cta["archivo"]), "w", encoding="latin-1", newline="") as f:
            f.write(buf.getvalue())

        with open(os.path.join(DEMO_DIR, f"{cid}.json"), "w", encoding="utf-8") as f:
            json.dump(asientos, f, ensure_ascii=False, indent=1)

        # --- mismo mayor, pero como .xlsx "Consulta del Mayor" real, para el
        # conciliador mensual (/) — ver parsers/mayor_xlsx.parse_mayor ---
        archivo_mayor = cta["archivo"].rsplit(".", 1)[0] + "-mayor.xlsx"
        _escribir_mayor_xlsx(asientos, os.path.join(KIT_DIR, archivo_mayor))

        total_movs = len(filas_csv)
        resultados_md.append(f"\n## {cta['archivo']} — {cta['empresa']}\n")
        resultados_md.append(f"Cuenta: {cta['banco']} {cta['numero']} ({cta['moneda']}) · id `{cid}` · "
                              f"{total_movs} movimientos en el extracto\n")
        resultados_md.append(
            f"Mismo resultado sirve para el **modo diario** (`/diario`, solo hace falta este CSV — "
            f"el mayor se agrega solo) y para el **conciliador mensual** (`/`, subiendo este CSV "
            f"como extracto **y** `{archivo_mayor}` como libro mayor).\n")
        resultados_md.append(RESULTADO_VERIFICADO.get(cta["archivo"],
            "- (correr `python scripts/generar_kit_demo.py` no revalida el resultado automáticamente: "
            "si cambiás los importes de este archivo, volvé a probarlo a mano por /diario y actualizá "
            "esta sección con lo que realmente pasó.)\n"))

    with open(os.path.join(KIT_DIR, "RESULTADOS_ESPERADOS.md"), "w", encoding="utf-8") as f:
        f.write("".join(resultados_md))

    print(f"Kit de demo generado: {len(CUENTAS)} cuenta(s) en {KIT_DIR} y {DEMO_DIR}")


if __name__ == "__main__":
    generar()
