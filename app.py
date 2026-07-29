# -*- coding: utf-8 -*-
"""Conciliador bancario — servidor web.

Uso:
    python -m uvicorn app:app --port 8765
o simplemente:
    python app.py
"""
import io
import json
import os
import sys
import tempfile
import uuid

from fastapi import Body, FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parsers.santander_pdf import parse_extracto
from parsers.mayor_xlsx import parse_mayor
from engine.matcher import conciliar
from engine import ai_assist
from engine import reglas as reglas_mod

app = FastAPI(title="Conciliador bancario")


@app.middleware("http")
async def _control_acceso(request, call_next):
    """Si CLAVE_ACCESO está definida (p.ej. en un deploy público), los
    endpoints /api/* requieren esa clave (header X-Clave o ?clave=)."""
    clave = os.environ.get("CLAVE_ACCESO")
    if clave and request.url.path.startswith("/api"):
        recibida = request.headers.get("x-clave") or request.query_params.get("clave")
        if recibida != clave:
            return JSONResponse(status_code=401,
                                content={"error": "Clave de acceso requerida"})
    return await call_next(request)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATOS_DIR = os.path.join(BASE_DIR, "datos")
os.makedirs(DATOS_DIR, exist_ok=True)
RESULTADOS = {}  # job_id -> resultado serializado

LISTAS_BANCO = ["banco_sin_contabilizar", "gastos_bancarios"]
LISTAS_MAYOR = ["e_sin_banco", "o_pendientes_sin_banco"]


def _guardar(job_id):
    with open(os.path.join(DATOS_DIR, f"{job_id}.json"), "w", encoding="utf-8") as f:
        json.dump(RESULTADOS[job_id], f, ensure_ascii=False)


def _obtener(job_id):
    if job_id not in RESULTADOS:
        ruta = os.path.join(DATOS_DIR, f"{job_id}.json")
        if os.path.exists(ruta):
            with open(ruta, encoding="utf-8") as f:
                RESULTADOS[job_id] = json.load(f)
    return RESULTADOS.get(job_id)


def _recalcular_resumen(datos):
    r = datos["resumen"]
    imp_b = lambda x: x["credito"] or x["debito"]
    imp_m = lambda x: x["debe"] or x["haber"]
    for lista in LISTAS_BANCO:
        r[lista] = {"cantidad": len(datos[lista]),
                    "importe": round(sum(imp_b(x) for x in datos[lista]), 2)}
    for lista in LISTAS_MAYOR:
        r[lista] = {"cantidad": len(datos[lista]),
                    "importe": round(sum(imp_m(x) for x in datos[lista]), 2)}
    r["conciliados_manual"] = len(datos.get("conciliados_manual", []))
    movs_manual = sum(len(m["banco"]) for m in datos.get("conciliados_manual", []))
    r["porcentaje_conciliado"] = round(
        100.0 * (r["conciliados_e"] + r["en_o_pendientes_confirmar"] + movs_manual)
        / max(1, r["movimientos_banco"]), 1)


def _serializar(resultado, extractos, ia_sugerencias, ia_estado):
    def match_dict(m):
        return {"banco": m["banco"].to_dict(), "asiento": m["asiento"].to_dict(),
                "metodo": m["metodo"]}

    return {
        "extractos": [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v)
             for k, v in e.items() if k != "movimientos"}
            for e in extractos
        ],
        "resumen": resultado["resumen"],
        "o_cancelados": resultado["o_cancelados"],
        "o_pendientes_total": resultado["o_pendientes_total"],
        "conciliados_e": [match_dict(m) for m in resultado["matches_e"]],
        "conciliados_o": [dict(match_dict(m), confirmado=False)
                          for m in resultado["matches_o"]],
        "banco_sin_contabilizar": [m.to_dict() for m in resultado["banco_sin_contabilizar"]],
        "gastos_bancarios": [m.to_dict() for m in resultado["gastos_bancarios"]],
        "e_sin_banco": [a.to_dict() for a in resultado["e_sin_banco"]],
        "o_pendientes_sin_banco": [a.to_dict() for a in resultado["o_pendientes_sin_banco"]],
        "nota_debito": [
            {**p, "asientos_mayor": [a.to_dict() for a in p["asientos_mayor"]]}
            for p in resultado["nota_debito"]
        ],
        "ia": {"estado": ia_estado, "sugerencias": ia_sugerencias},
    }


@app.post("/api/conciliar")
async def api_conciliar(
    extractos: list[UploadFile] = File(...),
    mayor: UploadFile = File(...),
    usar_ia: str = Form("no"),
):
    # --- parsear extractos -------------------------------------------------
    parseados = []
    for up in extractos:
        data = await up.read()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data)
            ruta = tmp.name
        try:
            parseados.append(parse_extracto(ruta, up.filename))
        finally:
            os.unlink(ruta)
    parseados.sort(key=lambda e: (e["desde"] or e["hasta"] or ""))
    movs = [m for e in parseados for m in e["movimientos"]]

    # --- parsear mayor ------------------------------------------------------
    data_mayor = await mayor.read()
    mayor_parsed = parse_mayor(data_mayor)
    if "E" not in mayor_parsed or "O" not in mayor_parsed:
        return JSONResponse(status_code=422, content={
            "error": "El Excel debe tener una hoja de la cuenta E y una de la cuenta O "
                     f"(hojas encontradas: {mayor_parsed['hojas']})"})
    asientos_e = mayor_parsed["E"]["asientos"]
    asientos_o = mayor_parsed["O"]["asientos"]

    # --- conciliar (con las reglas aprendidas de conciliaciones anteriores) --
    reglas = reglas_mod.cargar()
    resultado = conciliar(movs, asientos_e, asientos_o, reglas_aprendidas=reglas)

    # --- IA opcional sobre los residuales ------------------------------------
    ia_sugerencias, ia_estado = [], "desactivada"
    if usar_ia == "si":
        if not ai_assist.disponible():
            ia_estado = "sin_credenciales"
        else:
            try:
                residual_mayor = (resultado["e_sin_banco"]
                                  + resultado["o_pendientes_sin_banco"])
                # los gastos bancarios no van a la IA: ya están explicados por
                # la nota de débito mensual
                residual_banco = resultado["banco_sin_contabilizar"]
                ia_sugerencias = ai_assist.sugerir_matches(residual_banco, residual_mayor)
                ia_estado = "ok"
            except Exception as exc:  # noqa: BLE001 — mostrar el error al usuario
                ia_estado = f"error: {exc}"

    salida = _serializar(resultado, parseados, ia_sugerencias, ia_estado)
    salida["conciliados_manual"] = []
    salida["resumen"]["conciliados_manual"] = 0
    salida["resumen"]["reglas_disponibles"] = len(reglas)
    job_id = uuid.uuid4().hex[:12]
    salida["job_id"] = job_id
    RESULTADOS[job_id] = salida
    _guardar(job_id)
    return salida


@app.get("/api/resultado/{job_id}")
def api_resultado(job_id: str):
    datos = _obtener(job_id)
    if not datos:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    return datos


@app.post("/api/manual/{job_id}")
def api_conciliar_manual(job_id: str, cuerpo: dict = Body(...)):
    """Registra una conciliación manual (N movimientos del banco ↔ M asientos)."""
    datos = _obtener(job_id)
    if not datos:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    datos.setdefault("conciliados_manual", [])

    ids_banco = set(cuerpo.get("ids_banco") or [])
    ids_mayor = set(cuerpo.get("ids_mayor") or [])
    if not ids_banco or not ids_mayor:
        return JSONResponse(status_code=422, content={
            "error": "Hay que seleccionar al menos un movimiento del banco y un asiento del mayor"})

    # localizar y extraer los ítems de las listas residuales
    items_banco, items_mayor = [], []
    for lista in LISTAS_BANCO:
        for x in datos[lista]:
            if x["id"] in ids_banco:
                items_banco.append({**x, "_origen": lista})
    for lista in LISTAS_MAYOR:
        for x in datos[lista]:
            if x["id"] in ids_mayor:
                items_mayor.append({**x, "_origen": lista})

    encontrados = {x["id"] for x in items_banco} | {x["id"] for x in items_mayor}
    faltantes = (ids_banco | ids_mayor) - encontrados
    if faltantes:
        return JSONResponse(status_code=409, content={
            "error": f"Estos ítems ya no están disponibles (¿ya conciliados?): {sorted(faltantes)}"})

    for lista in LISTAS_BANCO:
        datos[lista] = [x for x in datos[lista] if x["id"] not in ids_banco]
    for lista in LISTAS_MAYOR:
        datos[lista] = [x for x in datos[lista] if x["id"] not in ids_mayor]

    neto_banco = round(sum(x["credito"] - x["debito"] for x in items_banco), 2)
    neto_mayor = round(sum(x["debe"] - x["haber"] for x in items_mayor), 2)
    match = {
        "match_id": uuid.uuid4().hex[:10],
        "banco": items_banco,
        "mayor": items_mayor,
        "neto_banco": neto_banco,
        "neto_mayor": neto_mayor,
        "diferencia": round(neto_banco - neto_mayor, 2),
        "nota": (cuerpo.get("nota") or "").strip(),
        "fuente": cuerpo.get("fuente") or "manual",
    }
    datos["conciliados_manual"].append(match)

    # aprender la regla para futuras conciliaciones
    regla = reglas_mod.aprender(match)
    match["regla_aprendida"] = bool(regla)

    idx = cuerpo.get("sugerencia_idx")
    if idx is not None and 0 <= idx < len(datos["ia"]["sugerencias"]):
        datos["ia"]["sugerencias"][idx]["aceptada"] = True

    _recalcular_resumen(datos)
    _guardar(job_id)
    return datos


@app.post("/api/confirmar/{job_id}")
def api_confirmar(job_id: str, cuerpo: dict = Body(...)):
    """Marca movimientos 'pendientes de confirmar (O)' como confirmados en el
    sistema contable (o los desmarca). Es un checklist de seguimiento."""
    datos = _obtener(job_id)
    if not datos:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})

    if "todos" in cuerpo:
        val = bool(cuerpo["todos"])
        for m in datos["conciliados_o"]:
            m["confirmado"] = val
    else:
        banco_id = cuerpo.get("banco_id")
        m = next((m for m in datos["conciliados_o"]
                  if m["banco"]["id"] == banco_id), None)
        if not m:
            return JSONResponse(status_code=404, content={"error": "Movimiento no encontrado"})
        m["confirmado"] = bool(cuerpo.get("confirmado", True))

    datos["resumen"]["o_confirmados"] = sum(
        1 for m in datos["conciliados_o"] if m.get("confirmado"))
    _guardar(job_id)
    return datos


@app.delete("/api/manual/{job_id}/{match_id}")
def api_deshacer_manual(job_id: str, match_id: str):
    """Deshace una conciliación manual: los ítems vuelven a sus listas."""
    datos = _obtener(job_id)
    if not datos:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    match = next((m for m in datos.get("conciliados_manual", [])
                  if m["match_id"] == match_id), None)
    if not match:
        return JSONResponse(status_code=404, content={"error": "Match no encontrado"})

    datos["conciliados_manual"] = [m for m in datos["conciliados_manual"]
                                   if m["match_id"] != match_id]
    reglas_mod.olvidar(match)
    for x in match["banco"]:
        origen = x.pop("_origen", "banco_sin_contabilizar")
        datos[origen].append(x)
        datos[origen].sort(key=lambda i: i["fecha"] or "")
    for x in match["mayor"]:
        origen = x.pop("_origen", "e_sin_banco")
        datos[origen].append(x)
        datos[origen].sort(key=lambda i: i["fecha"] or "")

    _recalcular_resumen(datos)
    _guardar(job_id)
    return datos


@app.get("/api/exportar/{job_id}")
def api_exportar(job_id: str):
    datos = _obtener(job_id)
    if not datos:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    buffer = _generar_excel(datos)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="conciliacion.xlsx"'},
    )


def _generar_excel(datos):
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    encabezado = Font(bold=True, color="FFFFFF")
    relleno = PatternFill("solid", fgColor="C8102E")

    def hoja(nombre, columnas, filas):
        ws = wb.create_sheet(nombre)
        ws.append(columnas)
        for c in ws[1]:
            c.font = encabezado
            c.fill = relleno
        for f in filas:
            ws.append(f)
        for col in ws.columns:
            ancho = max((len(str(c.value or '')) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(ancho + 2, 60)
        return ws

    # Resumen
    ws = wb.active
    ws.title = "Resumen"
    r = datos["resumen"]
    filas = [
        ("Movimientos del banco", r["movimientos_banco"]),
        ("Conciliados con cuenta E", r["conciliados_e"]),
        ("En cuenta O pendientes de confirmar", r["en_o_pendientes_confirmar"]),
        ("Conciliados manualmente (grupos)", r.get("conciliados_manual", 0)),
        ("Gastos/impuestos bancarios sin contabilizar",
         f'{r["gastos_bancarios"]["cantidad"]}  ($ {r["gastos_bancarios"]["importe"]:,.2f})'),
        ("Otros movimientos del banco sin contabilizar",
         f'{r["banco_sin_contabilizar"]["cantidad"]}  ($ {r["banco_sin_contabilizar"]["importe"]:,.2f})'),
        ("Asientos E sin movimiento en el banco",
         f'{r["e_sin_banco"]["cantidad"]}  ($ {r["e_sin_banco"]["importe"]:,.2f})'),
        ("Pendientes en O sin movimiento en el banco",
         f'{r["o_pendientes_sin_banco"]["cantidad"]}  ($ {r["o_pendientes_sin_banco"]["importe"]:,.2f})'),
        ("% del extracto conciliado", f'{r["porcentaje_conciliado"]}%'),
    ]
    ws.append(["Concepto", "Valor"])
    for c in ws[1]:
        c.font = encabezado
        c.fill = relleno
    for f in filas:
        ws.append(f)
    ws.column_dimensions["A"].width = 48
    ws.column_dimensions["B"].width = 30

    def fila_match(m):
        b, a = m["banco"], m["asiento"]
        return (b["fecha"], b["comprobante"], b["descripcion"], b["debito"] or "",
                b["credito"] or "", a["hoja"], a["asiento"], a["fecha"],
                a["referencia"], a["comentario"], a["debe"] or "", a["haber"] or "",
                m["metodo"])

    cols_match = ["Fecha banco", "Comprobante", "Descripción banco", "Débito", "Crédito",
                  "Hoja", "Asiento", "Fecha mayor", "Referencia", "Comentario",
                  "Debe", "Haber", "Método"]
    hoja("Conciliados (E)", cols_match, [fila_match(m) for m in datos["conciliados_e"]])
    hoja("Pendientes confirmar (O)", cols_match + ["Confirmado en FES"],
         [fila_match(m) + ("SÍ" if m.get("confirmado") else "",)
          for m in datos["conciliados_o"]])

    cols_banco = ["Fecha", "Comprobante", "Descripción", "Detalle", "Débito", "Crédito", "Archivo"]
    fila_banco = lambda b: (b["fecha"], b["comprobante"], b["descripcion"], b["detalle"],
                            b["debito"] or "", b["credito"] or "", b["archivo"])
    hoja("Banco sin contabilizar", cols_banco,
         [fila_banco(b) for b in datos["banco_sin_contabilizar"]])
    hoja("Gastos bancarios", cols_banco,
         [fila_banco(b) for b in datos["gastos_bancarios"]])

    cols_mayor = ["Hoja", "Asiento", "Fecha", "Referencia", "Comentario", "Debe", "Haber"]
    fila_mayor = lambda a: (a["hoja"], a["asiento"], a["fecha"], a["referencia"],
                            a["comentario"], a["debe"] or "", a["haber"] or "")
    hoja("Mayor E sin banco", cols_mayor, [fila_mayor(a) for a in datos["e_sin_banco"]])
    hoja("O pendientes sin banco", cols_mayor,
         [fila_mayor(a) for a in datos["o_pendientes_sin_banco"]])

    # Nota de débito mensual (gastos bancarios agrupados)
    filas_nd = []
    for p in datos["nota_debito"]:
        for c in p["categorias"]:
            obs = ""
            if "iva_esperado" in c:
                dif = round(c["importe"] - c["iva_esperado"], 2)
                obs = f'IVA esperado $ {c["iva_esperado"]:,.2f} (dif {dif:,.2f})'
            filas_nd.append((p["periodo"], c["categoria"], c["cantidad"], c["importe"], obs))
        filas_nd.append((p["periodo"], "TOTAL SEGÚN EXTRACTO", "", p["total_extracto"], ""))
        for a in p["asientos_mayor"]:
            lado = a["haber"] or -a["debe"]
            filas_nd.append((p["periodo"], f'Mayor [{a["hoja"]}] {a["referencia"]} — {a["comentario"]}',
                             "", lado, "asiento del mayor"))
        if p["total_mayor"] is not None:
            filas_nd.append((p["periodo"], "TOTAL SEGÚN MAYOR", "", p["total_mayor"], ""))
            filas_nd.append((p["periodo"], "DIFERENCIA (extracto - mayor)", "",
                             p["diferencia"], ""))
        filas_nd.append(("", "", "", "", ""))
    hoja("Gastos por mes (ND)",
         ["Período", "Concepto", "Cant.", "Importe", "Observación"], filas_nd)

    # Detalle de conceptos por período (para investigar diferencias)
    filas_con = []
    for p in datos["nota_debito"]:
        for c in p["conceptos"]:
            filas_con.append((p["periodo"], c["descripcion"], c["cantidad"], c["importe"]))
    hoja("Gastos detalle conceptos",
         ["Período", "Concepto del extracto", "Cant.", "Importe"], filas_con)

    # Conciliados manualmente (grupos N a M)
    filas_man = []
    for n, m in enumerate(datos.get("conciliados_manual", []), 1):
        for b in m["banco"]:
            filas_man.append((n, "BANCO", b["fecha"], b["comprobante"],
                              f'{b["descripcion"]} {b["detalle"]}'.strip(),
                              b["debito"] or "", b["credito"] or "", "", m["nota"]))
        for a in m["mayor"]:
            filas_man.append((n, f'MAYOR {a["hoja"]}', a["fecha"], a["asiento"],
                              f'{a["referencia"]} — {a["comentario"]}',
                              a["haber"] or "", a["debe"] or "", "", ""))
        filas_man.append((n, "→ diferencia", "", "", "", "", "", m["diferencia"],
                          "aceptada de IA" if m["fuente"] == "ia" else ""))
    hoja("Conciliados manualmente",
         ["Grupo", "Lado", "Fecha", "Comp/Asiento", "Descripción",
          "Débito/Haber", "Crédito/Debe", "Diferencia", "Nota"], filas_man)

    if datos["ia"]["sugerencias"]:
        hoja("Sugerencias IA",
             ["IDs banco", "IDs mayor", "Confianza", "Motivo"],
             [(", ".join(s["ids_banco"]), ", ".join(s["ids_mayor"]),
               s["confianza"], s["motivo"]) for s in datos["ia"]["sugerencias"]])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    puerto = int(os.environ.get("PORT", "8765"))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    uvicorn.run(app, host=host, port=puerto)
