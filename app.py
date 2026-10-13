# -*- coding: utf-8 -*-
"""Bank reconciler — web server.

Usage:
    python -m uvicorn app:app --port 8765
or simply:
    python app.py
"""
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime

from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parsers.santander_pdf import parse_statement
from parsers.ledger_xlsx import parse_ledger
from engine.matcher import reconcile
from engine import ai_assist
from engine import fbs_sql
from engine import ai_usage
from engine import rules as rules_mod
from engine import equivalences
from engine import fee_terms
from engine import accounts as accounts_mod
from engine import analysis
from engine import telemetria
from engine import acceso
from engine import presupuesto_ia
from engine import seguridad
from parsers import daily_files

# no /docs or /openapi.json: the API map is not published
app = FastAPI(title="Conciliador bancario",
              docs_url=None, redoc_url=None, openapi_url=None)
# monitoring (New Relic): the progress polling is not traced, it's noise
telemetria.iniciar(app, "conciliador", puerto=8765, excluir=[r"/api/progreso/"])
# mandatory authentication (APP_PASSWORD + signed session); without APP_PASSWORD
# only the machine itself is served. See engine/seguridad.py and AUDIT.md.
acceso.instalar(app)


@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "datos")
os.makedirs(DATA_DIR, exist_ok=True)
RESULTS = {}     # job_id -> serialized result
PROGRESS = {}    # job_id -> {estado, fase, porcentaje, eta_seg, ...}
STAGING = {}     # staging_id -> parsed files of the daily mode

# Demo environment: the FBS is simulated (engine/simulacion.py) and the demo
# generates the statements of each hub account in its bank's format, from the
# same simulation. Without this variable the behavior is exactly production's.
DEMO_MODE = fbs_sql.simulated()


def _blocked_in_demo():
    """The demo doesn't export or import the service's data. Returns the 404
    response when that applies."""
    if DEMO_MODE:
        return JSONResponse(status_code=404, content={"error": "No disponible en la demo"})
    return None


BANK_LISTS = ["banco_sin_contabilizar", "gastos_bancarios"]
LEDGER_LISTS = ["e_sin_banco", "o_pendientes_sin_banco"]


# Job ids (12) and daily run ids (10) are uuid4 hex. They are validated
# before a path is built: an id with "..\" (Windows) or a job_id in the body
# with "../" read and overwrote .json files outside datos/.
_VALID_JOB_RE = re.compile(r"^[0-9a-f]{12}$")
_VALID_RUN_RE = re.compile(r"^[0-9a-f]{10}$")


def _run_path(run_id) -> str | None:
    """Path of a daily run's board; None if the id is not valid."""
    if not isinstance(run_id, str) or not _VALID_RUN_RE.match(run_id):
        return None
    return os.path.join(DATA_DIR, f"grupo_{run_id}.json")


def _save_job(job_id):
    if not _VALID_JOB_RE.match(job_id):
        raise ValueError(f"id de job inválido: {job_id!r}")
    with open(os.path.join(DATA_DIR, f"{job_id}.json"), "w", encoding="utf-8") as f:
        json.dump(RESULTS[job_id], f, ensure_ascii=False)


def _get_job(job_id):
    if not isinstance(job_id, str) or not _VALID_JOB_RE.match(job_id):
        return None
    if job_id not in RESULTS:
        path = os.path.join(DATA_DIR, f"{job_id}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                RESULTS[job_id] = json.load(f)
    return RESULTS.get(job_id)


PAYROLL_TERMS = ("haber", "sueldo", "nomina")   # "pago haberes",
# "acred. sueldos", "acreditación (de) nómina", etc. — without accents or capitals.


def _is_payroll(m: dict) -> bool:
    import unicodedata
    text = f'{m.get("descripcion") or ""} {m.get("detalle") or ""}'
    plain = unicodedata.normalize("NFD", text.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return any(t in plain for t in PAYROLL_TERMS)


def _recompute_summary(data):
    # payroll (salary payments of the statement) is tagged to show it in its
    # own tab/sheet: the person builds the payroll entry in FBS from it and on
    # the next run they match by themselves. A saved manual reclassification
    # overrides the detector.
    overrides = _reclassification_overrides()
    for m in data.get("banco_sin_contabilizar", []):
        ov = overrides.get(_movement_key(m)) if overrides else None
        if ov == "haberes_sin_contabilizar":
            m["es_haberes"] = True
        elif ov == "banco_sin_contabilizar":
            m["es_haberes"] = False
        else:
            m["es_haberes"] = _is_payroll(m)
    # "I already loaded it in FBS": the operator's control mark. It lives by
    # movement key, so it follows the movement when it is carried over to the
    # next day or the period is reprocessed.
    loaded = _loaded_in_fbs()
    for list_name in BANK_LISTS:
        for m in data.get(list_name, []):
            info = loaded.get(_movement_key(m))
            m["cargado_fbs"] = info["fecha"] if info else None
    r = data["resumen"]
    def bank_amount(x):
        return x["credito"] or x["debito"]

    def ledger_amount(x):
        return x["debe"] or x["haber"]
    for list_name in BANK_LISTS:
        r[list_name] = {"cantidad": len(data[list_name]),
                        "importe": round(sum(bank_amount(x) for x in data[list_name]), 2)}
    payroll = [x for x in data["banco_sin_contabilizar"] if x.get("es_haberes")]
    rest = [x for x in data["banco_sin_contabilizar"] if not x.get("es_haberes")]
    r["haberes_sin_contabilizar"] = {"cantidad": len(payroll),
                                     "importe": round(sum(bank_amount(x) for x in payroll), 2)}
    r["banco_sin_contabilizar"] = {"cantidad": len(rest),
                                   "importe": round(sum(bank_amount(x) for x in rest), 2)}
    # loading control: how many of the pending ones were already loaded in FBS
    r["cargados_fbs"] = sum(1 for list_name in BANK_LISTS
                            for x in data.get(list_name, []) if x.get("cargado_fbs"))
    r["cargados_fbs_pendientes"] = len(payroll) + len(rest) - sum(
        1 for x in data["banco_sin_contabilizar"] if x.get("cargado_fbs"))
    for list_name in LEDGER_LISTS:
        r[list_name] = {"cantidad": len(data[list_name]),
                        "importe": round(sum(ledger_amount(x) for x in data[list_name]), 2)}
    r["conciliados_manual"] = len(data.get("conciliados_manual", []))
    manual_movs = sum(len(m["banco"]) for m in data.get("conciliados_manual", []))
    matched = r["conciliados_e"] + r["en_o_pendientes_confirmar"] + manual_movs
    r["porcentaje_conciliado"] = round(
        100.0 * matched / max(1, r["movimientos_banco"]), 1)
    fees_explained = sum(p.get("movimientos", 0) for p in data.get("nota_debito", [])
                         if p.get("total_mayor") is not None)
    r["gastos_explicados"] = fees_explained
    r["porcentaje_explicado"] = round(
        min(100.0, 100.0 * (matched + fees_explained) / max(1, r["movimientos_banco"])), 1)


def _serialize(result, statements, ai_suggestions, ai_state):
    def match_dict(m):
        return {"banco": m["banco"].to_dict(), "asiento": m["asiento"].to_dict(),
                "metodo": m["metodo"]}

    return {
        "extractos": [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v)
             for k, v in e.items() if k != "movimientos"}
            for e in statements
        ],
        "resumen": result["resumen"],
        "o_cancelados": result["o_cancelados"],
        "o_pendientes_total": result["o_pendientes_total"],
        "conciliados_e": [match_dict(m) for m in result["matches_e"]],
        "conciliados_o": [dict(match_dict(m), confirmado=False)
                          for m in result["matches_o"]],
        "banco_sin_contabilizar": [m.to_dict() for m in result["banco_sin_contabilizar"]],
        "gastos_bancarios": [m.to_dict() for m in result["gastos_bancarios"]],
        "e_sin_banco": [a.to_dict() for a in result["e_sin_banco"]],
        "o_pendientes_sin_banco": [a.to_dict() for a in result["o_pendientes_sin_banco"]],
        "nota_debito": [
            {**p, "asientos_mayor": [a.to_dict() for a in p["asientos_mayor"]]}
            for p in result["nota_debito"]
        ],
        "contraasientos": [
            {"debe": p["debe"].to_dict(), "haber": p["haber"].to_dict()}
            for p in result["contraasientos"]
        ],
        "excluidos_mayor": [],
        "ia": {"estado": ai_state, "sugerencias": ai_suggestions},
    }


@app.post("/api/conciliar")
async def api_reconcile(
    extractos: list[UploadFile] = File(...),
    mayor: UploadFile | None = File(None),
    usar_ia: str = Form("no"),
    marca: str = Form(""),
    fbs_cuenta: str = Form(""),
    fbs_desde: str = Form(""),
    fbs_hasta: str = Form(""),
):
    """Receives the files, starts the processing in the background and
    returns the job_id right away. The ledger can come as an uploaded Excel
    or straight from FBS (fbs_cuenta + fbs_desde + fbs_hasta, hub config).
    Progress is read from /api/progreso."""
    if usar_ia == "si":
        try:
            presupuesto_ia.verificar()
        except presupuesto_ia.PresupuestoExcedido as exc:
            return JSONResponse(status_code=429, content={"error": str(exc)})
    files = [(up.filename, await up.read()) for up in extractos]
    ledger_data = await mayor.read() if mayor else b""
    if not ledger_data and not fbs_cuenta:
        return JSONResponse(status_code=422, content={
            "error": "Subí el Excel del mayor o elegí una cuenta del FBS "
                     "con su rango de fechas."})
    fbs_ledger = ({"cuenta": fbs_cuenta, "desde": fbs_desde, "hasta": fbs_hasta}
                  if not ledger_data else None)

    job_id = uuid.uuid4().hex[:12]
    total_mb = (sum(len(b) for _, b in files) + len(ledger_data)) / 1_000_000
    base_estimate = 10 + total_mb * 14   # parsing + matching, calibrated with real data
    PROGRESS[job_id] = {"estado": "procesando", "fase": "Preparando el análisis",
                        "porcentaje": 2, "eta_seg": round(base_estimate),
                        "inicio": time.time()}
    threading.Thread(target=_process_job_measured,
                     args=(job_id, files, ledger_data, usar_ia, base_estimate,
                           marca, fbs_ledger),
                     daemon=True).start()
    return {"job_id": job_id, "estado": "procesando"}


def _process_job_measured(job_id, files, ledger_data, use_ai, base_estimate,
                          brand="", fbs_ledger=None):
    """_process_job as a monitoring span: duration and errors of each monthly
    reconciliation (it runs in a thread, outside any request)."""
    with telemetria.tramo("conciliacion mensual", job=job_id, marca=brand or None,
                          extractos=len(files),
                          mayor="fbs" if fbs_ledger else "excel"):
        _process_job(job_id, files, ledger_data, use_ai, base_estimate, brand,
                     fbs_ledger)


def _process_job(job_id, files, ledger_data, use_ai, base_estimate, brand="",
                 fbs_ledger=None):
    started = PROGRESS[job_id]["inicio"]

    def prog(phase, pct, eta=None):
        if eta is None:
            eta = max(3, base_estimate - (time.time() - started))
        PROGRESS[job_id] = {"estado": "procesando", "fase": phase,
                            "porcentaje": round(pct), "eta_seg": round(eta),
                            "inicio": started}

    try:
        # --- parse statements: PDF (Santander) or XLS/XLSX/CSV/TXT of any of
        # the group's banks (the daily mode's parsers; only the parsing CODE
        # is reused — the daily data, its memory and its carry-over play no
        # part in the monthly reconciliation) -----------------------------------
        parsed = []
        n = len(files)
        for i, (name, data) in enumerate(files):
            prog(f"Leyendo extracto {i + 1} de {n}: {name}", 5 + 30 * i / n)
            if data[:5] == b'%PDF-' or name.lower().endswith(".pdf"):
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(data)
                    path = tmp.name
                try:
                    parsed.append(parse_statement(path, name))
                finally:
                    os.unlink(path)
                continue
            info = daily_files.identify(name, data)
            if info.get("tipo") == "fbs":
                PROGRESS[job_id] = {"estado": "error", "mensaje":
                    f'"{name}" es un reporte del FBS (Consulta del Mayor): '
                    "va en el campo del libro mayor, no en el de extractos."}
                return
            if info.get("tipo") != "extracto":
                PROGRESS[job_id] = {"estado": "error", "mensaje":
                    f'No pude leer "{name}" como extracto bancario '
                    f'({info.get("error", "formato no reconocido")}). '
                    "Se aceptan PDF, XLS/XLSX y CSV de los bancos del grupo."}
                return
            # ids unique across files, same convention as the PDF parser
            for j, m in enumerate(info["movimientos"], 1):
                m.id = f"{name}#{j}"
            parsed.append({"archivo": name, "titular": None,
                           "cuenta": info.get("cuenta"),
                           "banco": info.get("banco"),
                           "desde": info.get("desde"), "hasta": info.get("hasta"),
                           "saldo_inicial": None, "saldo_final": None,
                           "movimientos": info["movimientos"]})
        parsed.sort(key=lambda e: (e["desde"] or e["hasta"] or date.min))
        movs = [m for e in parsed for m in e["movimientos"]]

        # --- parse the ledger (uploaded Excel or straight from FBS) -------------
        ledger_source = {"tipo": "archivo"}
        if fbs_ledger:
            prog("Trayendo el mayor E y O directo del FBS", 38)
            try:
                d1 = date.fromisoformat(fbs_ledger["desde"] or "")
                d2 = date.fromisoformat(fbs_ledger["hasta"] or "")
                if d1 > d2:
                    d1, d2 = d2, d1
                infos = fbs_sql.fetch(d1.isoformat(), d2.isoformat(),
                                      only_accounts=[fbs_ledger["cuenta"]])
            except ValueError as exc:
                PROGRESS[job_id] = {"estado": "error",
                                    "mensaje": f"Mayor desde el FBS: {exc}"}
                return
            ledger = {"hojas": [i["archivo"] for i in infos],
                      "E": {"asientos": []}, "O": {"asientos": []}}
            for i in infos:
                ledger[i["hoja"]]["asientos"].extend(i["asientos"])
            ledger_source = {
                "tipo": "fbs", "cuenta_id": fbs_ledger["cuenta"],
                "desde": d1.isoformat(), "hasta": d2.isoformat(),
                "asientos_e": len(ledger["E"]["asientos"]),
                "asientos_o": len(ledger["O"]["asientos"])}
        else:
            prog("Leyendo el libro mayor", 38)
            ledger = parse_ledger(ledger_data)
            if "E" not in ledger or "O" not in ledger:
                PROGRESS[job_id] = {"estado": "error", "mensaje":
                    "El Excel debe tener una hoja de la cuenta E y una de la cuenta O "
                    f"(hojas encontradas: {ledger['hojas']})"}
                return

        # --- reconcile ------------------------------------------------------------
        prog("Cruzando el extracto contra el mayor", 46)
        rules = rules_mod.load()
        equivalence_pairs = equivalences.load()
        user_fee_terms = fee_terms.load()
        _force_fee_flags(movs)
        result = reconcile(movs, ledger["E"]["asientos"],
                           ledger["O"]["asientos"], learned_rules=rules,
                           equivalence_pairs=equivalence_pairs,
                           user_fee_terms=user_fee_terms,
                           fee_exceptions=fee_terms.load_exceptions())

        # --- AI (real or simulated) on the residuals ---------------------------
        ai_suggestions, ai_state = [], "desactivada"
        if use_ai in ("si", "simular"):
            ledger_residual = (result["e_sin_banco"]
                               + result["o_pendientes_sin_banco"])
            # bank fees don't go to the AI: the monthly debit note explains them
            bank_residual = result["banco_sin_contabilizar"]
            secs_per_batch = 4 if use_ai == "simular" else 45

            def ai_progress(batch, total):
                prog(f"Análisis con IA — tanda {batch} de {total}",
                     58 + 38 * (batch - 1) / total,
                     eta=(total - batch + 1) * secs_per_batch)

            if use_ai == "simular":
                ai_suggestions = ai_assist.simulate_suggestions(
                    bank_residual, ledger_residual, progress=ai_progress,
                    glossary=equivalence_pairs)
                ai_state = "simulada"
            elif not ai_assist.available():
                ai_state = "sin_credenciales"
            else:
                try:
                    ai_suggestions = ai_assist.suggest_matches(
                        bank_residual, ledger_residual, progress=ai_progress,
                        glossary=equivalence_pairs)
                    ai_state = "ok"
                except presupuesto_ia.PresupuestoExcedido as exc:
                    ai_state = f"error: {exc}"     # carries on without AI
                except Exception as exc:  # noqa: BLE001 — show the error
                    ai_state = f"error: {exc}"
                    telemetria.registrar_error(exc, origen="ia", job=job_id)

        prog("Preparando los resultados", 96, eta=4)
        out = _serialize(result, parsed, ai_suggestions, ai_state)
        out["mayor_origen"] = ledger_source
        out["marca"] = brand or None
        if brand:
            # warning when a statement looks like an account of ANOTHER brand
            everything = accounts_mod.load()
            brand_warnings = []
            for e in parsed:
                c = accounts_mod.find_by_number(
                    everything, e.get("banco"), e.get("cuenta"))
                if c and accounts_mod.slug(brand) not in accounts_mod.slug(c["empresa"]):
                    brand_warnings.append(
                        f'"{e["archivo"]}" parece la cuenta {c["numero"]} de '
                        f'{c["empresa"]}, no de la marca de esta página.')
            out["avisos_marca"] = brand_warnings
        # audit: which AI mode came in the request (to diagnose unintended
        # simulated runs)
        out["ia"]["modo_pedido"] = use_ai
        out["conciliados_manual"] = []
        out["resumen"]["conciliados_manual"] = 0
        out["resumen"]["reglas_disponibles"] = len(rules)
        out["job_id"] = job_id
        out["cruces_vetados"] = _apply_vetoes(out)
        _recompute_summary(out)   # tags payroll and splits its counters
        analysis.annotate_residuals(out)
        RESULTS[job_id] = out
        _save_job(job_id)
        PROGRESS[job_id] = {"estado": "listo", "porcentaje": 100, "eta_seg": 0,
                            "fase": "Conciliación terminada", "inicio": started}
    except Exception as exc:  # noqa: BLE001 — the error must reach the UI
        PROGRESS[job_id] = {"estado": "error", "mensaje": str(exc)}
        telemetria.registrar_error(exc, origen="job", job=job_id, marca=brand or None)


@app.get("/api/diagnostico")
def api_diagnostics():
    """What the server sees of the AI environment, without exposing secrets.
    Used to check that the key reached the process (e.g. after a deploy)."""
    key = os.environ.get("ANTHROPIC_API_KEY") or ""
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
    return {
        "ia_disponible": ai_assist.available(),
        "ia_presupuesto": presupuesto_ia.estado(),
        "ANTHROPIC_API_KEY": {
            "presente": bool(key.strip()),
            "formato_esperado": key.strip().startswith("sk-ant-"),
            "espacios_al_borde": key != key.strip(),
            "entre_comillas": key.strip()[:1] in ('"', "'") if key.strip() else False,
        },
        "ANTHROPIC_AUTH_TOKEN_presente": bool(token.strip()),
        "archivo_clave_local": os.path.exists(ai_assist.KEY_FILE),
        # True if datos/ is mounted as a persistent volume (Railway):
        # without it, all the learning is lost on every deploy
        "datos_es_volumen": os.path.ismount(DATA_DIR),
        # state of the direct FBS connection through the hub config
        "fbs_hub": fbs_sql.diagnostics(),
        # the app's real API spend (tokens and estimated USD per call):
        # if the bill doesn't match this, the usage came from somewhere else
        "ia_uso": ai_usage.summary(),
        # monitoring (New Relic): whether it's on, with which group and version
        "monitor": telemetria.estado(),
    }


@app.post("/api/analizar/{job_id}")
def api_analyze(job_id: str, body: dict = Body(...)):
    """AI analysis of a pending entry: why it was left without a bank movement.
    The result is cached in the job so the call isn't repeated."""
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    ledger_id = body.get("id_mayor")
    entry = next((x for list_name in LEDGER_LISTS for x in data[list_name]
                  if x["id"] == ledger_id), None)
    if not entry:
        return JSONResponse(status_code=404, content={"error": "Asiento no encontrado"})

    cache = data.setdefault("analisis", {})
    if ledger_id in cache and not body.get("rehacer"):
        return {"analisis": cache[ledger_id]}

    learned = analysis.load()

    # if the signature already has a verdict from the user, no AI is spent:
    # what was learned (rule or correction) is returned directly
    signature = rules_mod.ledger_signature(entry.get("referencia"), entry.get("comentario"))
    previous = analysis.find(signature, learned)
    if previous and not body.get("rehacer"):
        cache[ledger_id] = {
            "texto": previous.get("correccion") or previous.get("explicacion", ""),
            "fecha": date.today().isoformat(),
            "veredicto": previous["veredicto"],
            "correccion": previous.get("correccion", ""),
            "origen": "aprendizaje",
        }
        _save_job(job_id)
        return {"analisis": cache[ledger_id]}

    corrections = [a for a in learned if a["veredicto"] == "corregido"][:8]
    try:
        result = analysis.analyze_entry(
            entry, data.get("banco_sin_contabilizar", []), corrections,
            account=(data.get("cuenta") or {}).get("etiqueta", ""),
            simulate=bool(body.get("simular")))
    except presupuesto_ia.PresupuestoExcedido as exc:
        return JSONResponse(status_code=429, content={"error": str(exc)})

    cache[ledger_id] = {"texto": result["texto"], "fecha": date.today().isoformat(),
                        "veredicto": None, "correccion": "",
                        "origen": result["origen"]}
    _save_job(job_id)
    return {"analisis": cache[ledger_id]}


@app.post("/api/analizar/{job_id}/veredicto")
def api_analysis_verdict(job_id: str, body: dict = Body(...)):
    """The user's confirmation of an analysis: Yes -> it becomes a rule;
    No + explanation -> the system learns the correction."""
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    ledger_id = body.get("id_mayor")
    analysis_entry = (data.get("analisis") or {}).get(ledger_id)
    if not analysis_entry:
        return JSONResponse(status_code=409, content={
            "error": "Primero hay que correr el análisis de este asiento"})
    ok = bool(body.get("ok"))
    correction = (body.get("correccion") or "").strip()
    if not ok and not correction:
        return JSONResponse(status_code=422, content={
            "error": "Contanos por qué el análisis no está bien, así el sistema aprende"})

    entry = next((x for list_name in LEDGER_LISTS for x in data[list_name]
                  if x["id"] == ledger_id), None)
    if entry:
        signature = rules_mod.ledger_signature(entry.get("referencia"), entry.get("comentario"))
        analysis.learn(signature, analysis_entry["texto"], ok, correction)
        analysis.annotate_residuals(data)
    analysis_entry["veredicto"] = "ok" if ok else "corregido"
    analysis_entry["correccion"] = correction
    _save_job(job_id)
    return {"analisis": analysis_entry}


MIGRATABLE_FILE_RE = re.compile(r'^[\w.-]+\.json$')
# never travel through the migration API: the FBS connection (with its password
# and the destination server) and the internal AI counters
NOT_MIGRATABLE = {"fbs_sql.json", "ia_presupuesto.json", "ia_uso_log.json"}


def _migratable(name: str) -> bool:
    return bool(MIGRATABLE_FILE_RE.match(name)) and name not in NOT_MIGRATABLE


@app.get("/api/migracion/exportar")
def api_migration_export():
    """Exports all of the service's data (learning, results, boards,
    accounts) as a single JSON. Never includes credentials."""
    if blocked := _blocked_in_demo():
        return blocked
    bundle = {}
    for name in sorted(os.listdir(DATA_DIR)):
        if not _migratable(name):
            continue
        try:
            with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
                bundle[name] = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
    return {"version": 1, "exportado": date.today().isoformat(),
            "cantidad": len(bundle), "archivos": bundle}


@app.post("/api/migracion/importar")
def api_migration_import(body: dict = Body(...)):
    """Imports a bundle exported from another service. As a safeguard, it only
    works on a service WITHOUT previous daily runs (unless {"forzar": true}),
    so production data isn't overwritten by mistake."""
    if blocked := _blocked_in_demo():
        return blocked
    has_runs = any(n.startswith("grupo_") for n in os.listdir(DATA_DIR))
    if has_runs and not body.get("forzar"):
        return JSONResponse(status_code=409, content={
            "error": "Este servicio ya tiene conciliaciones cargadas. "
                     "Para pisarlas igual hay que mandar forzar=true."})
    files = body.get("archivos") or {}
    written, ignored = 0, []
    for name, content in files.items():
        if not _migratable(name):
            ignored.append(name)
            continue
        with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False)
        written += 1
    RESULTS.clear()   # invalidate the in-memory cache: read again from disk
    return {"ok": True, "archivos_importados": written, "ignorados": ignored,
            "datos_es_volumen": os.path.ismount(DATA_DIR)}


@app.get("/api/equivalencias")
def api_equivalences():
    """Dictionary of statement ↔ system term equivalences."""
    return {"equivalencias": equivalences.load()}


@app.post("/api/equivalencias")
def api_equivalences_add(body: dict = Body(...)):
    pairs, error = equivalences.add(body.get("extracto"), body.get("sistema"))
    if error:
        return JSONResponse(status_code=422, content={"error": error})
    return {"equivalencias": pairs}


@app.delete("/api/equivalencias/{eq_id}")
def api_equivalences_remove(eq_id: str):
    return {"equivalencias": equivalences.remove(eq_id)}


@app.get("/api/cuentas")
def api_accounts(marca: str = ""):
    """The group's bank accounts with their label and FBS mapping. With
    ?marca= it is limited to that brand (full or abbreviated name)."""
    everything = accounts_mod.load()
    accounts = accounts_mod.filter_brand(everything, marca)
    return {"cuentas": [{**c, "etiqueta": accounts_mod.label(c)} for c in accounts],
            "bancos": accounts_mod.BANKS, "marca": marca or None,
            "marca_nombre": (sorted({c["empresa"] for c in accounts})[0]
                             if marca and accounts else None),
            # to explain an empty filter without having to guess the name
            "marcas_disponibles": accounts_mod.brands_with_accounts(everything)}


def _summary_row(name: str, info: dict, accounts: list[dict]) -> dict:
    """Row of the identification step for an already parsed (real or virtual)
    file: finds which bank account it belongs to."""
    account = None
    if info.get("cuenta_id_config"):   # link already configured in the hub
        account = next((c for c in accounts
                        if c["id"] == info["cuenta_id_config"]), None)
    elif info["tipo"] == "extracto":
        account = accounts_mod.find_by_number(
            accounts, info.get("banco"), info.get("cuenta"), info.get("moneda"))
    elif info["tipo"] == "fbs":
        idents = list((info.get("codigos") or {}).values()) + [info.get("nombre_fbs")]
        account = next((c for c in (accounts_mod.find_by_fbs(accounts, i)
                                    for i in idents if i) if c), None)
        if account is None:  # the internal name sometimes carries the real number
            account = accounts_mod.find_by_fbs_name(accounts, info.get("nombre_fbs"))
    return {
        "archivo": name, "tipo": info["tipo"],
        "banco": info.get("banco"), "moneda": info.get("moneda"),
        "cuenta_detectada": info.get("cuenta"),
        "hoja": info.get("hoja"), "codigo_fbs": info.get("codigo_fbs"),
        "nombre_fbs": info.get("nombre_fbs"),
        "cantidad": info.get("cantidad", 0),
        "desde": info["desde"].isoformat() if info.get("desde") else None,
        "hasta": info["hasta"].isoformat() if info.get("hasta") else None,
        "cuenta_id": account["id"] if account else None,
        # assigned from the hub config: not chosen again in the UI
        "cuenta_fija": bool(info.get("cuenta_id_config") and account),
        "error": info.get("error"),
    }


def _staging_target(staging_id: str, brand: str) -> tuple[str, dict]:
    """An existing staging (to add files to it) or a new one."""
    stag = STAGING.get(staging_id or "")
    if stag is None:
        staging_id = uuid.uuid4().hex[:10]
        stag = {"archivos": {}, "resumen": [], "marca": brand or None}
        STAGING[staging_id] = stag
    return staging_id, stag


def _add_to_staging(stag: dict, name: str, info: dict, accounts: list[dict]):
    """Adds (or replaces, if it was there) a parsed file to the staging."""
    stag["archivos"][name] = info
    stag["resumen"] = [f for f in stag["resumen"] if f["archivo"] != name]
    stag["resumen"].append(_summary_row(name, info, accounts))


@app.post("/api/diario/identificar")
async def api_daily_identify(archivos: list[UploadFile] = File(...),
                             marca: str = "", staging: str = ""):
    """Daily mode, step 1: detects what each file is (bank/account/FBS) and
    keeps the parsed data in memory for the reconciliation step. With ?marca=
    the identification only assigns accounts of that brand; with ?staging=
    the files are added to a previous identification (e.g. to the ledger
    already pulled from FBS through SQL) instead of starting from scratch."""
    staging_id, stag = _staging_target(staging, marca)
    accounts = accounts_mod.filter_brand(accounts_mod.load(),
                                         stag.get("marca") or marca)
    for up in archivos:
        data = await up.read()
        info = daily_files.identify(up.filename, data)
        _add_to_staging(stag, up.filename, info, accounts)
    return {"staging_id": staging_id, "archivos": stag["resumen"],
            "marca": stag.get("marca")}


@app.get("/api/fbs-sql")
def api_fbs_sql_get(marca: str = ""):
    """Config of the direct FBS connection (without the password). With
    ?marca= the accounts configured in the hub are limited to that brand."""
    return fbs_sql.public_config(marca)


@app.post("/api/fbs-sql")
def api_fbs_sql_post(request: Request, body: dict = Body(...)):
    # changing WHICH server the app connects to (and with which password) is
    # only allowed from the machine itself or when the deployment enables it
    # explicitly (FBS_SQL_EDITABLE=1); the query can always be edited
    editable = (seguridad.es_local(request)
                or os.environ.get("FBS_SQL_EDITABLE", "").strip().lower() in ("1", "true", "on", "si"))
    try:
        fbs_sql.save_config(body, allow_destination=editable)
    except PermissionError as exc:
        return JSONResponse(status_code=403, content={"error": str(exc)})
    except ValueError as exc:   # read-only / invalid destination
        return JSONResponse(status_code=422, content={"error": str(exc)})
    return fbs_sql.public_config()


@app.post("/api/fbs-sql/probar")
def api_fbs_sql_test():
    return fbs_sql.test_connection()


@app.post("/api/diario/fbs-sql")
def api_daily_fbs_sql(body: dict = Body(...)):
    """Pulls ledger E and O straight from the FBS SQL Server for a date range
    and adds it to the staging as if they were uploaded reports: the rest of
    the flow (mappings, reconciliation, memory, carry-over) is the same."""
    date_from, date_to = body.get("desde"), body.get("hasta")
    try:
        d1, d2 = date.fromisoformat(date_from or ""), date.fromisoformat(date_to or "")
    except ValueError:
        return JSONResponse(status_code=422, content={
            "error": "Indicá el rango de fechas (desde y hasta)."})
    if d1 > d2:
        d1, d2 = d2, d1
    try:
        infos = fbs_sql.fetch(d1.isoformat(), d2.isoformat(),
                              brand=body.get("marca") or "",
                              only_accounts=body.get("cuentas") or None)
    except ValueError as exc:
        return JSONResponse(status_code=502, content={"error": str(exc)})
    if not infos:
        return JSONResponse(status_code=422, content={
            "error": "La query del FBS no devolvió asientos E/O en ese rango."})
    brand = body.get("marca") or ""
    staging_id, stag = _staging_target(body.get("staging_id") or "", brand)
    accounts = accounts_mod.filter_brand(accounts_mod.load(),
                                         stag.get("marca") or brand)
    for info in infos:
        _add_to_staging(stag, info["archivo"], info, accounts)
    # the REQUESTED range (not the one of the entries that came back): it's
    # what later allows warning "the ledger you pulled doesn't reach the
    # statement's dates"
    prev = stag.get("rango_fbs")
    stag["rango_fbs"] = [min(d1.isoformat(), prev[0]) if prev else d1.isoformat(),
                         max(d2.isoformat(), prev[1]) if prev else d2.isoformat()]
    return {"staging_id": staging_id, "archivos": stag["resumen"],
            "marca": stag.get("marca"),
            "fbs_sql": {"cuentas": len(infos),
                        "asientos": sum(i["cantidad"] for i in infos)}}


@app.get("/api/diario/fbs-sql/excel/{staging_id}")
def api_daily_fbs_excel(staging_id: str):
    """Downloads what was pulled from FBS as an Excel with the SAME format as
    the 'Consulta del Mayor' reports that used to be uploaded by hand (one
    sheet '(E) (codigo)' / '(O) (codigo)' per account): it serves as a
    backup/audit trail and can even be uploaded to the reconciler again."""
    stag = STAGING.get(staging_id)
    if not stag:
        return JSONResponse(status_code=404, content={
            "error": "La identificación expiró (el servidor se reinició)."})
    infos = [i for i in stag["archivos"].values()
             if i.get("tipo") == "fbs"
             and str(i.get("archivo", "")).startswith("FBS directo")]
    if not infos:
        return JSONResponse(status_code=404, content={
            "error": "No hay asientos traídos del FBS en esta identificación."})
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for i in sorted(infos, key=lambda x: x["archivo"]):
        ws = wb.create_sheet(f"({i['hoja']}) ({i['codigo_fbs']})"[:31])
        ws.append(["Consulta del Mayor"])
        ws.append([f"Cuenta: {i.get('nombre_fbs') or ''} "
                   f"({i['hoja']}) ({i['codigo_fbs']})"])
        if i.get("desde"):
            ws.append([f"Movimientos del {i['desde']} AL {i['hasta']}"])
        ws.append([])
        ws.append(["Asiento", "Fecha", "Referencia", "Comentario", "Debe", "Haber"])
        for a in i["asientos"]:
            ws.append([a.number,
                       datetime(a.date.year, a.date.month, a.date.day)
                       if a.date else None,
                       a.reference, a.comment, a.debit, a.credit])
    buf = io.BytesIO()
    wb.save(buf)
    name = f"FBS_directo_{date.today().isoformat()}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


CARRY_OVER_PATH = os.path.join(DATA_DIR, "arrastre_diario.json")


def _load_carry_over() -> dict:
    if os.path.exists(CARRY_OVER_PATH):
        try:
            with open(CARRY_OVER_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_carry_over(d: dict):
    with open(CARRY_OVER_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _iso_date(s):
    from datetime import date as _d
    return _d.fromisoformat(s) if s else None


def _rebuild_movement(d, seq):
    from parsers.santander_pdf import BankMovement
    file = d.get("archivo") or ""
    if not file.startswith("(pendiente"):
        file = f"(pendiente de días anteriores) {file}"
    return BankMovement(
        id=f"P#{seq}", date=_iso_date(d.get("fecha")),
        voucher=d.get("comprobante"), description=d.get("descripcion") or "",
        detail=d.get("detalle") or "", debit=d.get("debito") or 0.0,
        credit=d.get("credito") or 0.0, balance=d.get("saldo") or 0.0,
        file=file, page=0)


def _rebuild_entry(d, seq):
    from parsers.ledger_xlsx import LedgerEntry
    return LedgerEntry(
        id=f"P{d.get('hoja', 'E')}#{seq}", sheet=d.get("hoja", "E"),
        number=d.get("asiento"), date=_iso_date(d.get("fecha")),
        reference=d.get("referencia") or "", comment=d.get("comentario") or "",
        debit=d.get("debe") or 0.0, credit=d.get("haber") or 0.0)


def _apply_carry_over(cid, movs, g, carry_over):
    """Adds to the matching the residuals of the account's previous
    reconciliation: unbooked bank movements and FBS entries without a bank
    movement, skipping those already in today's files. Returns (n_movs, n_entries)."""
    prev = _get_job(carry_over.get(cid) or "")
    if not prev:
        return 0, 0
    seq = 0
    mov_keys = {(m.date, m.description, round(m.credit - m.debit, 2), m.voucher)
                for m in movs}
    n_movs = 0
    for d in prev.get("banco_sin_contabilizar", []):
        key = (_iso_date(d.get("fecha")), d.get("descripcion") or "",
               round((d.get("credito") or 0) - (d.get("debito") or 0), 2),
               d.get("comprobante"))
        if key in mov_keys:
            continue
        seq += 1
        movs.append(_rebuild_movement(d, seq))
        n_movs += 1
    entry_keys = {(a.sheet, str(a.number), a.date, round(a.debit, 2), round(a.credit, 2))
                  for a in g["e"] + g["o"]}
    n_entries = 0
    for list_name, dest in (("e_sin_banco", g["e"]), ("o_pendientes_sin_banco", g["o"])):
        for d in prev.get(list_name, []):
            key = (d.get("hoja", "E"), str(d.get("asiento")), _iso_date(d.get("fecha")),
                   round(d.get("debe") or 0, 2), round(d.get("haber") or 0, 2))
            if key in entry_keys:
                continue
            seq += 1
            dest.append(_rebuild_entry(d, seq))
            n_entries += 1
    return n_movs, n_entries


# --- matched memory: cumulative files don't duplicate ---------------------------
# The client uploads the day's statement but the cumulative FBS ledger (or, at
# month end, everything together). Without this, what was already matched in
# previous runs showed up again as "sin banco"/"sin asiento". The memory keeps,
# per account, how many times each movement/entry was consumed, and new runs
# skip those repetitions.
MATCHED_MEMORY_PATH = os.path.join(DATA_DIR, "memoria_conciliados.json")


def _load_memory() -> dict:
    if os.path.exists(MATCHED_MEMORY_PATH):
        try:
            with open(MATCHED_MEMORY_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_memory(m: dict):
    with open(MATCHED_MEMORY_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)


def _movement_key(d) -> str:
    net = (d.get("credito") or 0) - (d.get("debito") or 0)
    return f'{d.get("fecha") or ""}|{d.get("descripcion") or ""}|{net:.2f}|{d.get("comprobante") or ""}'


def _movement_key_obj(m) -> str:
    return (f'{m.date.isoformat() if m.date else ""}|{m.description}|'
            f'{m.credit - m.debit:.2f}|{m.voucher or ""}')


def _entry_key(d) -> str:
    return (f'{d.get("hoja") or ""}|{d.get("asiento") or ""}|{d.get("fecha") or ""}|'
            f'{d.get("debe") or 0:.2f}|{d.get("haber") or 0:.2f}')


def _entry_key_obj(a) -> str:
    return (f'{a.sheet}|{a.number}|{a.date.isoformat() if a.date else ""}|'
            f'{a.debit:.2f}|{a.credit:.2f}')


# --- vetoed matches: bank<->entry pairs the user marked as "doesn't belong".
# The match is undone and the pair is never proposed again in any future run
# (important with the daily carry-over, which would rebuild it the next day).
VETOES_PATH = os.path.join(DATA_DIR, "cruces_vetados.json")


def _load_vetoes() -> list:
    if os.path.exists(VETOES_PATH):
        try:
            with open(VETOES_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_vetoes(vetoes: list):
    unique, seen = [], set()
    for v in vetoes:
        pair = (v.get("mov"), v.get("asiento"))
        if pair not in seen:
            seen.add(pair)
            unique.append(v)
    with open(VETOES_PATH, "w", encoding="utf-8") as f:
        json.dump(unique, f, ensure_ascii=False, indent=1)


def _undo_match(data, match, list_name):
    """Undoes a match in a serialized job: the movement goes back to 'Banco sin
    contabilizar' and the entry to its pending list according to its sheet."""
    data[list_name] = [m for m in data[list_name] if m is not match]
    b, a = match["banco"], match["asiento"]
    data["banco_sin_contabilizar"].append(b)
    data["banco_sin_contabilizar"].sort(key=lambda x: x.get("fecha") or "")
    target = "o_pendientes_sin_banco" if a.get("hoja") == "O" else "e_sin_banco"
    data[target].append(a)
    data[target].sort(key=lambda x: x.get("fecha") or "")
    r = data["resumen"]
    r["conciliados_e"] = len(data["conciliados_e"])
    r["en_o_pendientes_confirmar"] = len(data["conciliados_o"])
    r["o_confirmados"] = sum(1 for m in data["conciliados_o"] if m.get("confirmado"))
    _recompute_summary(data)
    return target


def _apply_vetoes(data) -> int:
    """In a freshly generated job, undoes the matches the user vetoed in
    previous runs. Returns how many it undid."""
    vetoes = {(v.get("mov"), v.get("asiento")) for v in _load_vetoes()}
    if not vetoes:
        return 0
    n = 0
    for list_name in ("conciliados_e", "conciliados_o"):
        for match in list(data.get(list_name, [])):
            pair = (_movement_key(match["banco"]),
                    _entry_key(match["asiento"]))
            if pair in vetoes:
                _undo_match(data, match, list_name)
                n += 1
    return n


def _veto_id(v) -> str:
    """Old vetoes have no id: it's derived from the pair so they can be deleted."""
    return v.get("id") or hashlib.sha1(
        f'{v.get("mov")}|{v.get("asiento")}'.encode("utf-8")).hexdigest()[:12]


def _readable_veto(v) -> dict:
    """Opens the keys 'fecha|descripcion|neto|comprobante' and
    'hoja|asiento|fecha|debe|haber' to show them in the UI."""
    def num(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None
    m = (v.get("mov") or "").split("|")
    a = (v.get("asiento") or "").split("|")
    mov = {"fecha": m[0] if m else "", "descripcion": "|".join(m[1:-2]) if len(m) >= 4 else "",
           "neto": num(m[-2]) if len(m) >= 4 else None, "comprobante": m[-1] if len(m) >= 4 else ""}
    entry = {"hoja": a[0] if a else "", "asiento": "|".join(a[1:-3]) if len(a) >= 5 else "",
             "fecha": a[-3] if len(a) >= 5 else "", "debe": num(a[-2]) if len(a) >= 5 else None,
             "haber": num(a[-1]) if len(a) >= 5 else None}
    return {"id": _veto_id(v), "metodo": v.get("metodo"), "job": v.get("job"),
            "fecha": v.get("fecha"), "mov": mov, "asiento": entry}


@app.get("/api/vetos")
def api_vetoes_list():
    """Matches cancelled by the user (vetoes): pairs no run proposes again,
    so they can be seen and reverted."""
    return {"vetos": [_readable_veto(v) for v in _load_vetoes()]}


@app.delete("/api/vetos/{veto_id}")
def api_vetoes_delete(veto_id: str):
    """Removes a veto: the pair can be matched again in the next run.
    It doesn't touch the results already saved."""
    vetoes = _load_vetoes()
    remaining = [v for v in vetoes if _veto_id(v) != veto_id]
    if len(remaining) == len(vetoes):
        return JSONResponse(status_code=404, content={"error": "Cruce anulado no encontrado"})
    _save_vetoes(remaining)
    return {"vetos": [_readable_veto(v) for v in remaining]}


# --- manual reclassifications: the user moves a bank movement between "Banco
# sin contabilizar", "Gastos bancarios" and "Haberes", explaining why. The
# decision persists by key and is respected in every future run (it overrides
# the automatic detector).
RECLASSIFICATIONS_PATH = os.path.join(DATA_DIR, "reclasificaciones.json")
RECLASSIFICATION_TARGETS = ("banco_sin_contabilizar", "gastos_bancarios",
                            "haberes_sin_contabilizar")


def _load_reclassifications() -> list:
    if os.path.exists(RECLASSIFICATIONS_PATH):
        try:
            with open(RECLASSIFICATIONS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_reclassifications(items: list):
    with open(RECLASSIFICATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def _reclassification_overrides() -> dict:
    """movement key -> chosen target (the last decision wins)."""
    return {r["mov"]: r["destino"] for r in _load_reclassifications()
            if r.get("destino") in RECLASSIFICATION_TARGETS}


# --- "I already loaded it in FBS": the operator's control mark ------------------
# The cashier loads the movements in FBS out of order (some must be sorted
# out with someone else) and needs to tick off what's done, as they used to in
# Excel. The mark does NOT move the movement to another tab: it only paints the
# row, so when they look again they know what's left. It lives by movement
# key, so it goes along when it's carried over to the next day or the month
# is reprocessed.
LOADED_IN_FBS_PATH = os.path.join(DATA_DIR, "cargados_fbs.json")


def _loaded_in_fbs() -> dict:
    """movement key -> {fecha, nota}."""
    if os.path.exists(LOADED_IN_FBS_PATH):
        try:
            with open(LOADED_IN_FBS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_loaded_in_fbs(d: dict):
    with open(LOADED_IN_FBS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _force_fee_flags(movs):
    """Before matching: flags the saved reclassifications on the movements, so
    the matcher (and the monthly debit note) classifies as decided."""
    overrides = _reclassification_overrides()
    if not overrides:
        return
    for m in movs:
        ov = overrides.get(_movement_key_obj(m))
        if ov == "gastos_bancarios":
            m._force_fee = True
        elif ov in ("banco_sin_contabilizar", "haberes_sin_contabilizar"):
            m._force_fee = False


def _harvest_consumed(prev: dict):
    """Keys of everything already matched/explained in a job, in its CURRENT
    state (including what was matched by hand after the run)."""
    movs, entries = {}, {}

    def add(d, k):
        d[k] = d.get(k, 0) + 1

    for m in prev.get("conciliados_e", []) + prev.get("conciliados_o", []):
        add(movs, _movement_key(m["banco"]))
        add(entries, _entry_key(m["asiento"]))
    for m in prev.get("conciliados_manual", []):
        for b in m.get("banco", []):
            add(movs, _movement_key(b))
        for a in m.get("mayor", []):
            add(entries, _entry_key(a))
    for b in prev.get("gastos_bancarios", []):
        add(movs, _movement_key(b))
    for p in prev.get("nota_debito", []):
        for a in p.get("asientos_mayor", []):
            add(entries, _entry_key(a))
    for p in prev.get("contraasientos", []):
        add(entries, _entry_key(p["debe"]))
        add(entries, _entry_key(p["haber"]))
    for e in prev.get("excluidos_mayor", []):
        for a in e.get("mayor", []):
            add(entries, _entry_key(a))
    return movs, entries


def _process_confirmations(prev, g):
    """Closes the O -> E cycle between daily runs.

    The movements that in the previous run were left "matched against O,
    pending confirmation" are carried over waiting for their confirmation: if
    today's files have their entry in account E (they were loaded in FBS),
    they go STRAIGHT to Conciliados (E) and the counter-entry the confirmation
    created in O is cancelled. If they're not confirmed yet, they still show
    as pending confirmation.

    Returns (confirmed_e, carried_o_pending, cancelled_counter_entries)."""
    from engine import matcher as matcher_mod

    waiting = list((prev or {}).get("conciliados_o") or [])
    if not waiting or not g["e"]:
        return [], waiting, 0

    # The confirmation is STRICT: in FES, confirming an order replicates in
    # account E the reference, the amount and the side of the original O
    # entry. Only that counts as a confirmation — no "unique amount" or close
    # dates, which confirmed against someone else's entries of the same amount
    # (e.g. one leg of an internal transfer) and consumed them forever through
    # the matched memory.
    def _keys(entry_ref, entry_comment):
        rm = matcher_mod._rm_key(entry_comment or "")
        ref = (entry_ref or "").strip().upper()
        return {c for c in (rm, ref) if c}

    confirmed, confirmed_idx, cancelled = [], set(), 0
    used_e = set()
    for i, item in enumerate(waiting):
        entry = item.get("asiento") or {}
        o_keys = _keys(entry.get("referencia"), entry.get("comentario"))
        if not o_keys:
            continue      # without a reference there is no automatic confirmation
        amount = round((entry.get("debe") or 0) or (entry.get("haber") or 0), 2)
        original_side = "debe" if (entry.get("debe") or 0) else "haber"
        e_conf = next(
            (a for a in g["e"]
             if a.id not in used_e and a.side == original_side
             and round(a.amount, 2) == amount
             and _keys(a.reference, a.comment) & o_keys), None)
        if e_conf is None:
            continue
        used_e.add(e_conf.id)
        confirmed_idx.add(i)
        confirmed.append({
            "banco": item["banco"], "asiento": e_conf.to_dict(),
            "metodo": "confirmado en E (venía conciliado contra O)",
        })
        # the confirmation creates a counter-entry in O: cancel it from the pool
        for j, a in enumerate(g["o"]):
            if (a.side != original_side and round(a.amount, 2) == amount
                    and _keys(a.reference, a.comment) & o_keys):
                del g["o"][j]
                cancelled += 1
                break
    g["e"][:] = [a for a in g["e"] if a.id not in used_e]

    pending = []
    for i, item in enumerate(waiting):
        if i in confirmed_idx:
            continue
        method = item.get("metodo") or ""
        if "arrastrado" not in method:
            item = {**item, "metodo": (method + " · arrastrado").strip(" ·")}
        pending.append(item)
    return confirmed, pending, cancelled


def _ledger_coverage(movs, ledger_range):
    """The ledger is pulled by date range: what the bank has OUTSIDE that range
    has nothing to match against. It's not that the entry is missing in FBS —
    it wasn't pulled. Returns None if the ledger covers the whole statement.

    The ledger range comes from TODAY's files/pulls (not from the entries that
    survived the matched memory: if today's ledger was already all matched,
    the range was still pulled).

    On the forward side 2 days are tolerated: it's normal for the bank's last
    movements not to have an entry loaded yet."""
    d1, d2 = ledger_range
    fb = [m.date for m in movs if m.date]
    if not d1 or not d2 or not fb:
        return None
    before = sum(1 for m in movs if m.date and m.date < d1)
    after = sum(1 for m in movs if m.date and (m.date - d2).days > 2)
    if not before and not after:
        return None
    return {"mayor_desde": d1.isoformat(), "mayor_hasta": d2.isoformat(),
            "banco_desde": min(fb).isoformat(), "banco_hasta": max(fb).isoformat(),
            "antes": before, "despues": after, "fuera": before + after}


def _mini_summary(r):
    return {
        "movimientos_banco": r["movimientos_banco"],
        "conciliados": r["conciliados_e"] + r["en_o_pendientes_confirmar"]
                       + r.get("conciliados_manual", 0),
        "porcentaje_explicado": r.get("porcentaje_explicado", r["porcentaje_conciliado"]),
        "banco_sin_contabilizar": r["banco_sin_contabilizar"]["cantidad"],
        "gastos_bancarios": r["gastos_bancarios"]["cantidad"],
        "mayor_sin_banco": r["e_sin_banco"]["cantidad"] + r["o_pendientes_sin_banco"]["cantidad"],
        "arrastrados": r.get("arrastrados", 0),
        "arrastrados_movs": r.get("arrastrados_movs", 0),
        "arrastrados_asientos": r.get("arrastrados_asientos", 0),
        "omitidos": r.get("omitidos", 0),
    }


@app.post("/api/diario/conciliar/{staging_id}")
def api_daily_reconcile(staging_id: str, body: dict = Body(...)):
    """Daily mode, step 2: with the file→account assignments confirmed, runs
    one reconciliation per account (same engine and same learning as the
    monthly mode) and returns the day's board."""
    stag = STAGING.get(staging_id)
    if not stag:
        return JSONResponse(status_code=404, content={
            "error": "La identificación expiró (el servidor se reinició). Volvé a subir los archivos."})
    assignments = body.get("asignaciones") or {}
    accounts = accounts_mod.load()
    by_id = {c["id"]: c for c in accounts}
    # the board id is known from the start: each job stores which daily run it
    # belongs to, so its detail knows how to go back to the day's board
    run_id = uuid.uuid4().hex[:10]

    # group files by account and learn new FBS mappings
    groups = {}
    unassigned = []
    for row in stag["resumen"]:
        name = row["archivo"]
        cid = assignments.get(name, row.get("cuenta_id"))
        if not cid or cid == "ignorar" or row["tipo"] == "desconocido":
            unassigned.append({"archivo": name, "tipo": row["tipo"],
                               "motivo": row.get("error") or "sin cuenta asignada"})
            continue
        info = stag["archivos"][name]
        g = groups.setdefault(cid, {"movs": [], "e": [], "o": [], "archivos": [],
                                    "mayor_fechas": []})
        g["archivos"].append(name)
        if row["tipo"] == "extracto":
            g["movs"].extend(info["movimientos"])
        else:
            # which date range today's ledger brought (to warn if it falls
            # short of the statement)
            g["mayor_fechas"] += [date.fromisoformat(row[k]) for k in ("desde", "hasta")
                                  if row.get(k)]
            # an FBS file can carry sheet E, sheet O, or both together
            g["e"].extend(a for a in info["asientos"] if a.sheet == "E")
            g["o"].extend(a for a in info["asientos"] if a.sheet == "O")
            codes = info.get("codigos") or {}
            if by_id.get(cid):
                for letter in set(info.get("hoja") or "E") & {"E", "O"}:
                    ident = codes.get(letter) or row.get("nombre_fbs")
                    if ident and ident not in (by_id[cid].get("fbs_e"),
                                               by_id[cid].get("fbs_o")):
                        accounts = accounts_mod.map_fbs(cid, letter, ident,
                                                        row.get("nombre_fbs"))
                by_id = {c["id"]: c for c in accounts}

    rules = rules_mod.load()
    equivalence_pairs = equivalences.load()
    user_fee_terms = fee_terms.load()

    carry_over = _load_carry_over()
    memory = _load_memory()
    board = []
    for cid, g in groups.items():
        label = accounts_mod.label(by_id[cid]) if cid in by_id else cid

        # matched memory: add what the previous run consumed (in its current
        # state, manual matches included) and skip the repetitions today's
        # cumulative files bring
        mem = memory.setdefault(cid, {"movs": {}, "asientos": {}, "jobs": []})
        mem.setdefault("jobs", [])
        prev_id = carry_over.get(cid)
        prev = _get_job(prev_id or "")
        if prev and prev_id not in mem["jobs"]:   # each run is harvested only once
            c_movs, c_entries = _harvest_consumed(prev)
            for k, n in c_movs.items():
                mem["movs"][k] = mem["movs"].get(k, 0) + n
            for k, n in c_entries.items():
                mem["asientos"][k] = mem["asientos"].get(k, 0) + n
            mem["jobs"].append(prev_id)

        skipped_movs = skipped_entries = 0
        used_mem = {}
        # Repeated rows are NEVER suppressed: two identical taxes or
        # commissions are real movements. The only thing skipped is what the
        # matched memory recorded as already consumed in previous runs (and
        # only as many times as it was consumed).
        movs = []
        for m in sorted(g["movs"], key=lambda x: (x.date or date.min, x.id)):
            k = _movement_key_obj(m)
            if used_mem.get(k, 0) < mem["movs"].get(k, 0):
                used_mem[k] = used_mem.get(k, 0) + 1
                skipped_movs += 1
                continue
            m.id = f"B#{len(movs) + 1}"
            movs.append(m)
        used_mem = {}
        for side in ("e", "o"):
            kept = []
            for a in g[side]:
                k = _entry_key_obj(a)
                if used_mem.get(k, 0) < mem["asientos"].get(k, 0):
                    used_mem[k] = used_mem.get(k, 0) + 1
                    skipped_entries += 1
                    continue
                kept.append(a)
            g[side] = kept
        # O -> E cycle: what was matched against O in the previous run waits
        # for its confirmation; if today's E has it, it goes straight to
        # Conciliados (E)
        confirmed_e, carried_o, cancelled_counter = _process_confirmations(prev, g)

        # does the pulled ledger cover the statement's dates? Checked BEFORE
        # the carry-over: carried entries are old and would hide the gap.
        ledger_dates = g.get("mayor_fechas") or []
        requested = stag.get("rango_fbs")
        if ledger_dates and requested:      # the ledger came straight from FBS: the request counts
            ledger_dates = [date.fromisoformat(requested[0]), date.fromisoformat(requested[1])]
        coverage = _ledger_coverage(movs, (min(ledger_dates), max(ledger_dates)) if ledger_dates else (None, None))

        # carry-over: the pending items of this account's previous
        # reconciliation join today's matching (yesterday's shows up in
        # today's FBS and the other way around)
        carried_movs, carried_entries = _apply_carry_over(cid, movs, g, carry_over)
        if not movs:
            board.append({"cuenta_id": cid, "etiqueta": label, "estado": "sin_extracto",
                          "archivos": g["archivos"]})
            continue
        if not g["e"] and not g["o"]:
            board.append({"cuenta_id": cid, "etiqueta": label, "estado": "sin_fbs",
                          "archivos": g["archivos"],
                          "resumen": {"movimientos_banco": len(movs)}})
            continue
        _force_fee_flags(movs)
        result = reconcile(movs, g["e"], g["o"], learned_rules=rules,
                           equivalence_pairs=equivalence_pairs, user_fee_terms=user_fee_terms,
                           fee_exceptions=fee_terms.load_exceptions())
        # period covered by TODAY's statements (not counting the carried ones,
        # which bring dates of previous days)
        dates = [m.date for m in movs if m.date and m.id.startswith("B#")]
        statements_meta = [{"archivo": ", ".join(g["archivos"]),
                            "desde": min(dates) if dates else None,
                            "hasta": max(dates) if dates else None}]
        job_id = uuid.uuid4().hex[:12]
        out = _serialize(result, statements_meta, [], "desactivada")
        out["conciliados_manual"] = []
        out["resumen"]["conciliados_manual"] = 0
        out["resumen"]["reglas_disponibles"] = len(rules)
        out["job_id"] = job_id
        out["cuenta"] = {"id": cid, "etiqueta": label}
        out["grupo_diario"] = run_id
        if coverage:
            out["cobertura_mayor"] = coverage
        # O -> E cycle: add the confirmed ones and carry those still pending
        if confirmed_e or carried_o:
            out["conciliados_e"].extend(confirmed_e)
            out["conciliados_o"].extend(carried_o)
            r = out["resumen"]
            r["conciliados_e"] += len(confirmed_e)
            r["en_o_pendientes_confirmar"] += len(carried_o)
            r["confirmados_desde_o"] = len(confirmed_e)
            r["contrapartidas_o_canceladas"] = cancelled_counter
            r["movimientos_banco"] += len(confirmed_e) + len(carried_o)
            matched = r["conciliados_e"] + r["en_o_pendientes_confirmar"]
            r["porcentaje_conciliado"] = round(
                100.0 * matched / max(1, r["movimientos_banco"]), 1)
            r["porcentaje_explicado"] = round(min(100.0,
                100.0 * (matched + r.get("gastos_explicados", 0))
                / max(1, r["movimientos_banco"])), 1)

        # which job this account's chain came from (to be able to undo runs)
        out["arrastre_desde"] = carry_over.get(cid)
        if skipped_movs or skipped_entries:
            out["memoria_omitidos"] = {"movimientos": skipped_movs, "asientos": skipped_entries}
            out["resumen"]["omitidos"] = skipped_movs + skipped_entries
        out["cruces_vetados"] = _apply_vetoes(out)
        _recompute_summary(out)   # tags payroll and splits its counters
        analysis.annotate_residuals(out)
        if carried_movs or carried_entries:
            out["arrastre"] = {"movimientos": carried_movs, "asientos": carried_entries,
                               "desde_job": carry_over.get(cid)}
            out["resumen"]["arrastrados"] = carried_movs + carried_entries
            out["resumen"]["arrastrados_movs"] = carried_movs
            out["resumen"]["arrastrados_asientos"] = carried_entries
        RESULTS[job_id] = out
        _save_job(job_id)
        carry_over[cid] = job_id   # the next reconciliation carries over from here
        board.append({"cuenta_id": cid, "etiqueta": label, "estado": "ok",
                      "job_id": job_id, "archivos": g["archivos"],
                      "desde": dates and min(dates).isoformat() or None,
                      "hasta": dates and max(dates).isoformat() or None,
                      "cobertura_mayor": coverage,
                      "resumen": _mini_summary(out["resumen"])})

    _save_carry_over(carry_over)
    _save_memory(memory)
    run = {"grupo_id": run_id, "procesado": date.today().isoformat(),
           "hora": time.strftime("%H:%M"),
           "marca": stag.get("marca"),
           "cuentas": sorted(board, key=lambda x: x["etiqueta"]),
           "sin_asignar": unassigned}
    with open(os.path.join(DATA_DIR, f"grupo_{run_id}.json"), "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False)
    STAGING.pop(staging_id, None)
    return run


@app.get("/api/diario")
def api_daily_history(marca: str = ""):
    """The daily memory: every saved daily reconciliation, newest first, with
    its summary. Boards are never deleted and the pending items chain each
    day to the next.

    With ?marca= only that brand's runs are listed. The filter doesn't depend
    on how brands and accounts are in the hub today: the brand is compared
    tolerating renames (same_brand) and the accounts by number, not by the
    full id (which changes if the bank is renamed). A run that belongs to no
    known brand is shown anyway, so it isn't lost from sight."""
    everything = accounts_mod.load() if marca else []
    brand_numbers = {accounts_mod.account_number(c["id"])
                     for c in accounts_mod.filter_brand(everything, marca)} - {""}
    other_numbers = ({accounts_mod.account_number(c["id"]) for c in everything} - {""}) - brand_numbers
    brands_today = accounts_mod.brands_with_accounts(everything)

    def _of_this_brand(g: dict, accounts: list) -> bool:
        nums = {accounts_mod.account_number(c.get("cuenta_id")) for c in accounts} - {""}
        if accounts_mod.same_brand(g.get("marca"), marca) or (nums & brand_numbers):
            return True
        if nums & other_numbers:                       # its accounts belong to another brand
            return False
        # no recognizable accounts: hidden only if it claims to be of another
        # brand that exists today; otherwise it's an orphan and is shown
        return not any(accounts_mod.same_brand(g.get("marca"), m) for m in brands_today)
    runs = []
    for name in os.listdir(DATA_DIR):
        if not (name.startswith("grupo_") and name.endswith(".json")):
            continue
        path = os.path.join(DATA_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                g = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        accounts = g.get("cuentas", [])
        if marca and not _of_this_brand(g, accounts):
            continue
        ok = [c for c in accounts if c.get("estado") == "ok"]
        def r(c, k):
            return (c.get("resumen") or {}).get(k) or 0
        explained = [c["resumen"]["porcentaje_explicado"] for c in ok
                     if (c.get("resumen") or {}).get("porcentaje_explicado") is not None]
        mov_dates = [c.get(k) for c in accounts for k in ("desde", "hasta") if c.get(k)]
        runs.append({
            "desde": min(mov_dates) if mov_dates else None,
            "hasta": max(mov_dates) if mov_dates else None,
            "grupo_id": g.get("grupo_id"), "marca": g.get("marca"),
            "bancos": sorted({accounts_mod.bank_name(c.get("cuenta_id"))
                              for c in accounts if c.get("cuenta_id")} - {""}),
            "procesado": g.get("procesado"), "hora": g.get("hora"),
            "cuentas": len(accounts), "conciliadas": len(ok),
            "incompletas": len(accounts) - len(ok),
            "movimientos": sum(r(c, "movimientos_banco") for c in ok),
            "arrastrados": sum(r(c, "arrastrados") for c in ok),
            "pendientes": sum(r(c, "banco_sin_contabilizar") + r(c, "mayor_sin_banco")
                              for c in ok),
            "explicado_promedio": round(sum(explained) / len(explained), 1) if explained else None,
            "_orden": os.path.getmtime(path),
        })
    runs.sort(key=lambda x: x["_orden"], reverse=True)
    for g in runs:
        g.pop("_orden")
    return {"grupos": runs}


def _matched_rows(data: dict) -> list[dict]:
    """Everything a reconciliation considered solved, as flat bank <-> entry
    rows. It's what the matched memory takes as consumed: that's why an
    already matched movement doesn't show up again in the following runs."""
    rows = []

    def row(kind, method, bank=None, entry=None):
        rows.append({"tipo": kind, "metodo": method,
                     "banco": bank, "asiento": entry})

    for m in data.get("conciliados_e", []):
        row("Cruzado con la cuenta E", m.get("metodo"), m.get("banco"), m.get("asiento"))
    for m in data.get("conciliados_o", []):
        row("Conciliado contra la O" + (" (confirmado)" if m.get("confirmado") else ""),
            m.get("metodo"), m.get("banco"), m.get("asiento"))
    for m in data.get("conciliados_manual", []):
        banks, ledger = m.get("banco") or [], m.get("mayor") or []
        for i in range(max(len(banks), len(ledger))):
            row("Conciliado a mano", m.get("nota") or "conciliación manual",
                banks[i] if i < len(banks) else None,
                ledger[i] if i < len(ledger) else None)
    for b in data.get("gastos_bancarios", []):
        row("Gasto bancario", "no se cruza asiento por asiento", b, None)
    return rows


def _row_text(f: dict) -> str:
    b, a = f.get("banco") or {}, f.get("asiento") or {}
    return " ".join(str(x) for x in (
        f.get("tipo"), f.get("metodo"), b.get("fecha"), b.get("descripcion"),
        b.get("detalle"), b.get("comprobante"), b.get("debito"), b.get("credito"),
        a.get("hoja"), a.get("asiento"), a.get("fecha"), a.get("referencia"),
        a.get("comentario"), a.get("debe"), a.get("haber")) if x)


@app.get("/api/diario/conciliados")
def api_daily_matched(cuenta: str = "", q: str = "", corridas: int = 40,
                      limite: int = 1500):
    """History of an account's matched items in daily mode: what each run
    matched and against which entry. It enables the check that couldn't be
    done: if a bank movement shows without an entry, here you see whether that
    entry was already consumed before — and with which movement it was matched.

    Built by reading the saved boards (not a separate copy), so it also
    reflects what was matched by hand or cancelled after the run."""
    num = accounts_mod.account_number(cuenta) if cuenta else ""
    runs = []
    for name in os.listdir(DATA_DIR):
        if name.startswith("grupo_") and name.endswith(".json"):
            path = os.path.join(DATA_DIR, name)
            runs.append((os.path.getmtime(path), path))
    runs.sort(reverse=True)

    rows, runs_read, truncated = [], 0, False
    for _, path in runs:
        if runs_read >= max(1, corridas) or len(rows) >= limite:
            truncated = truncated or len(rows) >= limite
            break
        try:
            with open(path, encoding="utf-8") as f:
                g = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        used = [c for c in g.get("cuentas", [])
                if c.get("estado") == "ok" and c.get("job_id")
                and (not cuenta or accounts_mod.account_number(c.get("cuenta_id")) == num)]
        if not used:
            continue
        runs_read += 1
        for c in used:
            data = _get_job(c["job_id"])
            if not data:
                continue
            header = {"grupo_id": g.get("grupo_id"), "job_id": c["job_id"],
                      "cuenta_id": c.get("cuenta_id"), "etiqueta": c.get("etiqueta"),
                      "procesado": g.get("procesado"), "hora": g.get("hora")}
            for f in _matched_rows(data):
                rows.append({**header, **f})

    if q:
        term = q.strip().lower()
        rows = [f for f in rows if term in _row_text(f).lower()]
    rows.sort(key=lambda f: ((f.get("banco") or {}).get("fecha")
                             or (f.get("asiento") or {}).get("fecha") or ""),
              reverse=True)
    return {"cuenta": cuenta, "corridas": runs_read, "total": len(rows),
            "truncado": truncated, "filas": rows[:limite]}


@app.delete("/api/diario/memoria")
def api_daily_memory_delete():
    """Deletes ALL the matched memory (test phase): the entries and movements
    consumed in previous runs show up again as pending in the next pulls. It
    doesn't touch the boards or the carry-over.
    (Declared before /api/diario/{grupo_id} so 'memoria' isn't taken as a
    board id.)"""
    m = _load_memory()
    movs = sum(sum(c.get("movs", {}).values()) for c in m.values())
    entries = sum(sum(c.get("asientos", {}).values()) for c in m.values())
    if os.path.exists(MATCHED_MEMORY_PATH):
        os.remove(MATCHED_MEMORY_PATH)
    return {"ok": True, "cuentas_olvidadas": len(m),
            "movs_olvidados": movs, "asientos_olvidados": entries}


@app.get("/api/diario/{grupo_id}/excel")
def api_daily_excel(grupo_id: str):
    """Exports a daily run's full board to Excel: summary per account +
    consolidated lists of every account (bank pending items, payroll, fees and
    ledger pending items), with the Cuenta column on each sheet. The full
    detail of ONE account is still exported from inside that reconciliation."""
    path = _run_path(grupo_id)
    if not path or not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "Tablero no encontrado"})
    with open(path, encoding="utf-8") as f:
        run = json.load(f)

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Tablero"
    ws.append(["Cuenta", "Estado", "Movimientos", "Conciliados", "Arrastrados",
               "Pendientes banco", "Pendientes mayor", "% explicado"])
    sheets = {
        "Banco sin contabilizar": ["Cuenta", "Fecha", "Comprobante", "Descripción",
                                   "Detalle", "Débito", "Crédito", "Cargado en FBS"],
        "Haberes sin contabilizar": ["Cuenta", "Fecha", "Comprobante", "Descripción",
                                     "Detalle", "Débito", "Crédito", "Cargado en FBS"],
        "Gastos bancarios": ["Cuenta", "Fecha", "Comprobante", "Descripción",
                             "Detalle", "Débito", "Crédito", "Cargado en FBS"],
        "Mayor sin banco": ["Cuenta", "Hoja", "Asiento", "Fecha", "Referencia",
                            "Comentario", "Debe", "Haber"],
    }
    for name, headers in sheets.items():
        wb.create_sheet(name).append(headers)

    def movement_row(label, m):
        return [label, m.get("fecha"), m.get("comprobante"), m.get("descripcion"),
                m.get("detalle"), m.get("debito") or None, m.get("credito") or None,
                m.get("cargado_fbs") or ""]

    for row in run.get("cuentas", []):
        label = row.get("etiqueta") or row.get("cuenta_id") or ""
        r = row.get("resumen") or {}
        ws.append([label, row.get("estado"),
                   r.get("movimientos_banco"), r.get("conciliados"),
                   r.get("arrastrados"), r.get("banco_sin_contabilizar"),
                   r.get("mayor_sin_banco"), r.get("porcentaje_explicado")])
        data = _get_job(row["job_id"]) if row.get("job_id") else None
        if not data:
            continue
        for m in data.get("banco_sin_contabilizar", []):
            target = ("Haberes sin contabilizar" if m.get("es_haberes")
                      else "Banco sin contabilizar")
            wb[target].append(movement_row(label, m))
        for m in data.get("gastos_bancarios", []):
            wb["Gastos bancarios"].append(movement_row(label, m))
        for list_name in ("e_sin_banco", "o_pendientes_sin_banco"):
            for a in data.get(list_name, []):
                wb["Mayor sin banco"].append(
                    [label, a.get("hoja"), a.get("asiento"), a.get("fecha"),
                     a.get("referencia"), a.get("comentario"),
                     a.get("debe") or None, a.get("haber") or None])

    buf = io.BytesIO()
    wb.save(buf)
    name = f"Conciliacion_diaria_{run.get('procesado') or grupo_id}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/diario/{grupo_id}")
def api_daily_run(grupo_id: str):
    path = _run_path(grupo_id)
    if not path or not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "Tablero no encontrado"})
    with open(path, encoding="utf-8") as f:
        run = json.load(f)
    # refresh the numbers with each job's current state (later manual
    # reconciliations included)
    for row in run["cuentas"]:
        if row.get("job_id"):
            data = _get_job(row["job_id"])
            if data:
                row["resumen"] = _mini_summary(data["resumen"])
    return run


@app.delete("/api/diario/{grupo_id}")
def api_daily_delete(grupo_id: str):
    """Deletes a whole daily run (e.g. a wrong statement was uploaded): removes
    the board and its accounts' jobs, and rewinds each account's carry-over
    chain to its previous run."""
    path = _run_path(grupo_id)
    if not path or not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "Tablero no encontrado"})
    with open(path, encoding="utf-8") as f:
        run = json.load(f)

    carry_over = _load_carry_over()
    rewound = []
    for row in run.get("cuentas", []):
        job_id = row.get("job_id")
        if not job_id:
            continue
        data = _get_job(job_id) or {}
        cid = row.get("cuenta_id")
        # if this run was the last link of the carry-over, go back to the previous one
        if cid and carry_over.get(cid) == job_id:
            previous = data.get("arrastre_desde") or \
                (data.get("arrastre") or {}).get("desde_job")
            if previous and _get_job(previous):
                carry_over[cid] = previous
                rewound.append({"cuenta_id": cid, "vuelve_a": previous})
            else:
                carry_over.pop(cid, None)
                rewound.append({"cuenta_id": cid, "vuelve_a": None})
        RESULTS.pop(job_id, None)
        try:
            os.remove(os.path.join(DATA_DIR, f"{job_id}.json"))
        except OSError:
            pass
    _save_carry_over(carry_over)
    os.remove(path)
    return {"ok": True, "grupo_id": grupo_id, "arrastre_rebobinado": rewound}


@app.get("/api/gastos")
def api_fees():
    """Concepts defined by the user that are classified as bank fees, and
    exceptions (concepts that NEVER are)."""
    return {"gastos": fee_terms.load(),
            "excepciones": fee_terms.load_exceptions()}


@app.delete("/api/gastos/excepciones/{exc_id}")
def api_fee_exception_delete(exc_id: str):
    """(Declared before /api/gastos/{term_id} so 'excepciones' isn't taken as
    a term id.)"""
    return {"excepciones": fee_terms.remove_exception(exc_id)}


@app.post("/api/gastos")
def api_fees_add(body: dict = Body(...)):
    """Adds a fee concept. If a job_id comes too, it also reclassifies in that
    result the 'banco sin contabilizar' movements that contain it."""
    terms, error = fee_terms.add(body.get("termino"))
    # a term already registered is not a fatal error: it's applied to the job
    # anyway (the user's typical retry when the first click moved nothing)
    if error and "ya está registrado" not in error:
        return JSONResponse(status_code=422, content={"error": error})

    data, moved = None, 0
    job_id = body.get("job_id")
    if job_id:
        data = _get_job(job_id)
    if data:
        term = fee_terms.clean_term(body.get("termino"))
        to_move = [x for x in data["banco_sin_contabilizar"]
                   if fee_terms.is_fee(x["descripcion"], [{"termino": term}])]
        if to_move:
            ids = {x["id"] for x in to_move}
            data["banco_sin_contabilizar"] = [
                x for x in data["banco_sin_contabilizar"] if x["id"] not in ids]
            data["gastos_bancarios"].extend(to_move)
            data["gastos_bancarios"].sort(key=lambda x: x["fecha"] or "")
            moved = len(to_move)
            _recompute_summary(data)
            _save_job(job_id)
    return {"gastos": terms, "movidos": moved, "datos": data}


@app.delete("/api/gastos/{term_id}")
def api_fees_delete(term_id: str):
    return {"gastos": fee_terms.remove(term_id)}


@app.post("/api/excluir/{job_id}")
def api_exclude(job_id: str, body: dict = Body(...)):
    """Takes ledger entries out of the reconciliation (cancelled ones,
    reversals the automatic detection didn't find, etc.). They stay referenced
    and can be restored."""
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    ids = set(body.get("ids_mayor") or [])
    if not ids:
        return JSONResponse(status_code=422, content={"error": "No hay asientos seleccionados"})

    items = []
    for list_name in LEDGER_LISTS:
        for x in data[list_name]:
            if x["id"] in ids:
                items.append({**x, "_origen": list_name})
    if len(items) != len(ids):
        return JSONResponse(status_code=409, content={
            "error": "Algunos asientos ya no están disponibles"})
    for list_name in LEDGER_LISTS:
        data[list_name] = [x for x in data[list_name] if x["id"] not in ids]

    data.setdefault("excluidos_mayor", []).append({
        "excl_id": uuid.uuid4().hex[:10],
        "mayor": items,
        "nota": (body.get("nota") or "").strip(),
    })
    _recompute_summary(data)
    _save_job(job_id)
    return data


@app.delete("/api/excluir/{job_id}/{excl_id}")
def api_exclude_undo(job_id: str, excl_id: str):
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    exclusion = next((e for e in data.get("excluidos_mayor", [])
                      if e["excl_id"] == excl_id), None)
    if not exclusion:
        return JSONResponse(status_code=404, content={"error": "Exclusión no encontrada"})
    data["excluidos_mayor"] = [e for e in data["excluidos_mayor"]
                               if e["excl_id"] != excl_id]
    for x in exclusion["mayor"]:
        origin = x.pop("_origen", "e_sin_banco")
        data[origin].append(x)
        data[origin].sort(key=lambda i: i["fecha"] or "")
    _recompute_summary(data)
    _save_job(job_id)
    return data


@app.get("/api/progreso/{job_id}")
def api_progress(job_id: str):
    p = PROGRESS.get(job_id)
    if p:
        return p
    if _get_job(job_id):
        return {"estado": "listo", "porcentaje": 100, "eta_seg": 0,
                "fase": "Conciliación terminada"}
    return JSONResponse(status_code=404, content={"error": "Job no encontrado"})


@app.get("/api/resultado/{job_id}")
def api_result(job_id: str):
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    return data


@app.post("/api/manual/{job_id}")
def api_manual_match(job_id: str, body: dict = Body(...)):
    """Records a manual reconciliation (N bank movements ↔ M entries)."""
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    data.setdefault("conciliados_manual", [])

    bank_ids = set(body.get("ids_banco") or [])
    ledger_ids = set(body.get("ids_mayor") or [])
    if not bank_ids or not ledger_ids:
        return JSONResponse(status_code=422, content={
            "error": "Hay que seleccionar al menos un movimiento del banco y un asiento del mayor"})

    # find and take the items out of the residual lists
    bank_items, ledger_items = [], []
    for list_name in BANK_LISTS:
        for x in data[list_name]:
            if x["id"] in bank_ids:
                bank_items.append({**x, "_origen": list_name})
    for list_name in LEDGER_LISTS:
        for x in data[list_name]:
            if x["id"] in ledger_ids:
                ledger_items.append({**x, "_origen": list_name})

    found = {x["id"] for x in bank_items} | {x["id"] for x in ledger_items}
    missing = (bank_ids | ledger_ids) - found
    if missing:
        return JSONResponse(status_code=409, content={
            "error": f"Estos ítems ya no están disponibles (¿ya conciliados?): {sorted(missing)}"})

    for list_name in BANK_LISTS:
        data[list_name] = [x for x in data[list_name] if x["id"] not in bank_ids]
    for list_name in LEDGER_LISTS:
        data[list_name] = [x for x in data[list_name] if x["id"] not in ledger_ids]

    bank_net = round(sum(x["credito"] - x["debito"] for x in bank_items), 2)
    ledger_net = round(sum(x["debe"] - x["haber"] for x in ledger_items), 2)
    match = {
        "match_id": uuid.uuid4().hex[:10],
        "banco": bank_items,
        "mayor": ledger_items,
        "neto_banco": bank_net,
        "neto_mayor": ledger_net,
        "diferencia": round(bank_net - ledger_net, 2),
        "nota": (body.get("nota") or "").strip(),
        "fuente": body.get("fuente") or "manual",
    }
    data["conciliados_manual"].append(match)

    # learn the rule for future reconciliations
    rule = rules_mod.learn(match)
    match["regla_aprendida"] = bool(rule)

    idx = body.get("sugerencia_idx")
    if idx is not None and 0 <= idx < len(data["ia"]["sugerencias"]):
        data["ia"]["sugerencias"][idx]["aceptada"] = True

    _recompute_summary(data)
    _save_job(job_id)
    return data


@app.post("/api/confirmar/{job_id}")
def api_confirm(job_id: str, body: dict = Body(...)):
    """Marks 'pending confirmation (O)' movements as confirmed in the
    accounting system (or unmarks them). It's a follow-up checklist."""
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})

    if "todos" in body:
        val = bool(body["todos"])
        for m in data["conciliados_o"]:
            m["confirmado"] = val
    else:
        bank_id = body.get("banco_id")
        m = next((m for m in data["conciliados_o"]
                  if m["banco"]["id"] == bank_id), None)
        if not m:
            return JSONResponse(status_code=404, content={"error": "Movimiento no encontrado"})
        m["confirmado"] = bool(body.get("confirmado", True))

    data["resumen"]["o_confirmados"] = sum(
        1 for m in data["conciliados_o"] if m.get("confirmado"))
    _save_job(job_id)
    return data


@app.post("/api/cargado/{job_id}")
def api_mark_loaded(job_id: str, body: dict = Body(...)):
    """Marks (or unmarks) a bank movement as ALREADY LOADED in FBS. It's a
    control mark: the movement doesn't change tabs, it's only painted, so the
    operator knows what is left to load when they look again. The mark
    persists per movement and follows it to the next day."""
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    bank_id = body.get("banco_id")
    mov = next((m for list_name in BANK_LISTS for m in data.get(list_name, [])
                if m["id"] == bank_id), None)
    if not mov:
        return JSONResponse(status_code=404, content={"error": "Movimiento no encontrado"})

    loaded = _loaded_in_fbs()
    key = _movement_key(mov)
    if body.get("cargado", True):
        loaded[key] = {"fecha": date.today().isoformat(),
                       "nota": (body.get("nota") or "").strip()[:300],
                       "descripcion": mov.get("descripcion"), "job": job_id}
    else:
        loaded.pop(key, None)
    _save_loaded_in_fbs(loaded)

    _recompute_summary(data)   # re-tags and updates the counters
    _save_job(job_id)
    return data


@app.post("/api/reclasificar/{job_id}")
def api_reclassify(job_id: str, body: dict = Body(...)):
    """Moves a bank movement to another classification (banco sin contabilizar
    / gastos bancarios / haberes), with the user's reason. The decision
    persists and future runs respect it."""
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    target = body.get("destino")
    if target not in RECLASSIFICATION_TARGETS:
        return JSONResponse(status_code=422, content={"error": "Destino inválido"})
    reason = (body.get("motivo") or "").strip()
    if not reason:
        return JSONResponse(status_code=422, content={
            "error": "Contanos por qué va en esa clasificación, así queda registrado"})
    bank_id = body.get("banco_id")
    origin = next((list_name for list_name in ("banco_sin_contabilizar", "gastos_bancarios")
                   if any(m["id"] == bank_id for m in data.get(list_name, []))), None)
    if not origin:
        return JSONResponse(status_code=404, content={"error": "Movimiento no encontrado"})
    mov = next(m for m in data[origin] if m["id"] == bank_id)

    target_list = ("gastos_bancarios" if target == "gastos_bancarios"
                   else "banco_sin_contabilizar")
    if target_list != origin:
        data[origin] = [m for m in data[origin] if m is not mov]
        data[target_list].append(mov)
        data[target_list].sort(key=lambda x: x.get("fecha") or "")
    mov["reclasificado"] = reason

    items = _load_reclassifications()
    items = [r for r in items if r.get("mov") != _movement_key(mov)]
    items.append({"mov": _movement_key(mov), "destino": target,
                  "motivo": reason[:300], "descripcion": mov.get("descripcion"),
                  "fecha": date.today().isoformat(), "job": job_id})
    _save_reclassifications(items)

    # "record that this CONCEPT is never a fee": when it's taken out of fees,
    # the exception can be learned, and it applies to similar movements in ALL
    # future runs (not only this movement)
    learned_exception = None
    if body.get("aprender_concepto") and target != "gastos_bancarios":
        _, err = fee_terms.add_exception(mov.get("descripcion"))
        if err and "ya está registrado" not in err:
            return JSONResponse(status_code=422, content={"error": err})
        learned_exception = fee_terms.clean_term(mov.get("descripcion"))
        # move right away the other fees of this result that contain it
        similar = [m for m in data["gastos_bancarios"]
                   if fee_terms.is_fee(m.get("descripcion"),
                                       [{"termino": learned_exception}])]
        if similar:
            ids = {m["id"] for m in similar}
            data["gastos_bancarios"] = [m for m in data["gastos_bancarios"]
                                        if m["id"] not in ids]
            data["banco_sin_contabilizar"].extend(similar)
            data["banco_sin_contabilizar"].sort(key=lambda x: x.get("fecha") or "")

    _recompute_summary(data)
    _save_job(job_id)
    # the monthly debit note totals of THIS result are not rebuilt here; they
    # come out right in the next run (the matcher already respects the decision)
    return {**data, "excepcion_aprendida": learned_exception}


@app.post("/api/anular/{job_id}")
def api_cancel_match(job_id: str, body: dict = Body(...)):
    """Cancels an automatic match that doesn't belong (the client's request):
    the movement goes back to 'Banco sin contabilizar' and the entry to its
    pending list (E or O according to its sheet). The pair is vetoed: no
    future run proposes it again."""
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    list_name = body.get("lista")
    if list_name not in ("conciliados_e", "conciliados_o"):
        return JSONResponse(status_code=422, content={"error": "Lista inválida"})
    bank_id = body.get("banco_id")
    match = next((m for m in data.get(list_name, [])
                  if m["banco"]["id"] == bank_id), None)
    if not match:
        return JSONResponse(status_code=404, content={"error": "Cruce no encontrado"})

    vetoes = _load_vetoes()
    vetoes.append({"id": uuid.uuid4().hex[:12],
                   "mov": _movement_key(match["banco"]),
                   "asiento": _entry_key(match["asiento"]),
                   "metodo": match.get("metodo"), "job": job_id,
                   "fecha": date.today().isoformat()})
    _save_vetoes(vetoes)
    _undo_match(data, match, list_name)
    _save_job(job_id)
    return data


@app.delete("/api/manual/{job_id}/{match_id}")
def api_manual_undo(job_id: str, match_id: str):
    """Undoes a manual reconciliation: the items go back to their lists."""
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    match = next((m for m in data.get("conciliados_manual", [])
                  if m["match_id"] == match_id), None)
    if not match:
        return JSONResponse(status_code=404, content={"error": "Match no encontrado"})

    data["conciliados_manual"] = [m for m in data["conciliados_manual"]
                                  if m["match_id"] != match_id]
    rules_mod.forget(match)
    for x in match["banco"]:
        origin = x.pop("_origen", "banco_sin_contabilizar")
        data[origin].append(x)
        data[origin].sort(key=lambda i: i["fecha"] or "")
    for x in match["mayor"]:
        origin = x.pop("_origen", "e_sin_banco")
        data[origin].append(x)
        data[origin].sort(key=lambda i: i["fecha"] or "")

    _recompute_summary(data)
    _save_job(job_id)
    return data


@app.get("/api/exportar/{job_id}")
def api_export(job_id: str):
    data = _get_job(job_id)
    if not data:
        return JSONResponse(status_code=404, content={"error": "Resultado no encontrado"})
    buffer = _build_excel(data)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="conciliacion.xlsx"'},
    )


def _build_excel(data):
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="C8102E")

    def sheet(name, columns, rows):
        ws = wb.create_sheet(name)
        ws.append(columns)
        for c in ws[1]:
            c.font = header_font
            c.fill = header_fill
        for f in rows:
            ws.append(f)
        for col in ws.columns:
            width = max((len(str(c.value or '')) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(width + 2, 60)
        return ws

    # Summary
    ws = wb.active
    ws.title = "Resumen"
    r = data["resumen"]
    rows = [
        ("Movimientos del banco", r["movimientos_banco"]),
        ("Conciliados con cuenta E", r["conciliados_e"]),
        ("En cuenta O pendientes de confirmar", r["en_o_pendientes_confirmar"]),
        ("Conciliados manualmente (grupos)", r.get("conciliados_manual", 0)),
        ("Gastos/impuestos bancarios sin contabilizar",
         f'{r["gastos_bancarios"]["cantidad"]}  ($ {r["gastos_bancarios"]["importe"]:,.2f})'),
        ("Haberes del extracto sin contabilizar (para asiento de sueldos en FBS)",
         f'{r.get("haberes_sin_contabilizar", {}).get("cantidad", 0)}  '
         f'($ {r.get("haberes_sin_contabilizar", {}).get("importe", 0):,.2f})'),
        ("Otros movimientos del banco sin contabilizar",
         f'{r["banco_sin_contabilizar"]["cantidad"]}  ($ {r["banco_sin_contabilizar"]["importe"]:,.2f})'),
        ("Asientos E sin movimiento en el banco",
         f'{r["e_sin_banco"]["cantidad"]}  ($ {r["e_sin_banco"]["importe"]:,.2f})'),
        ("Pendientes en O sin movimiento en el banco",
         f'{r["o_pendientes_sin_banco"]["cantidad"]}  ($ {r["o_pendientes_sin_banco"]["importe"]:,.2f})'),
        ("% del extracto conciliado asiento por asiento", f'{r["porcentaje_conciliado"]}%'),
        ("% del extracto explicado (incluye gastos justificados por ND mensual)",
         f'{r.get("porcentaje_explicado", r["porcentaje_conciliado"])}%'),
    ]
    ws.append(["Concepto", "Valor"])
    for c in ws[1]:
        c.font = header_font
        c.fill = header_fill
    for f in rows:
        ws.append(f)
    ws.column_dimensions["A"].width = 48
    ws.column_dimensions["B"].width = 30

    def match_row(m):
        b, a = m["banco"], m["asiento"]
        return (b["fecha"], b["comprobante"], b["descripcion"], b["debito"] or "",
                b["credito"] or "", a["hoja"], a["asiento"], a["fecha"],
                a["referencia"], a["comentario"], a["debe"] or "", a["haber"] or "",
                m["metodo"])

    match_cols = ["Fecha banco", "Comprobante", "Descripción banco", "Débito", "Crédito",
                  "Hoja", "Asiento", "Fecha mayor", "Referencia", "Comentario",
                  "Debe", "Haber", "Método"]
    sheet("Conciliados (E)", match_cols, [match_row(m) for m in data["conciliados_e"]])
    sheet("Pendientes confirmar (O)", match_cols + ["Confirmado en FES"],
          [match_row(m) + ("SÍ" if m.get("confirmado") else "",)
           for m in data["conciliados_o"]])

    bank_cols = ["Fecha", "Comprobante", "Descripción", "Detalle", "Débito", "Crédito",
                 "Archivo", "Cargado en FBS"]
    def bank_row(b):
        return (b["fecha"], b["comprobante"], b["descripcion"], b["detalle"],
                b["debito"] or "", b["credito"] or "", b["archivo"],
                b.get("cargado_fbs") or "")
    payroll = [b for b in data["banco_sin_contabilizar"] if b.get("es_haberes")]
    rest = [b for b in data["banco_sin_contabilizar"] if not b.get("es_haberes")]
    # a dedicated sheet: the payroll entry in FBS is built from this list
    sheet("Haberes sin contabilizar", bank_cols, [bank_row(b) for b in payroll])
    sheet("Banco sin contabilizar", bank_cols, [bank_row(b) for b in rest])
    sheet("Gastos bancarios", bank_cols,
          [bank_row(b) for b in data["gastos_bancarios"]])

    ledger_cols = ["Hoja", "Asiento", "Fecha", "Referencia", "Comentario", "Debe", "Haber"]
    def ledger_row(a):
        return (a["hoja"], a["asiento"], a["fecha"], a["referencia"],
                a["comentario"], a["debe"] or "", a["haber"] or "")
    sheet("Mayor E sin banco", ledger_cols, [ledger_row(a) for a in data["e_sin_banco"]])
    sheet("O pendientes sin banco", ledger_cols,
          [ledger_row(a) for a in data["o_pendientes_sin_banco"]])

    excluded_rows = []
    for p in data.get("contraasientos", []):
        excluded_rows.append(ledger_row(p["debe"]) + ("contraasiento (automático)",))
        excluded_rows.append(ledger_row(p["haber"]) + ("contraasiento (automático)",))
    for e in data.get("excluidos_mayor", []):
        reason = "excluido manualmente" + (f' — {e["nota"]}' if e["nota"] else "")
        for a in e["mayor"]:
            excluded_rows.append(ledger_row(a) + (reason,))
    sheet("Contraasientos y excluidos", ledger_cols + ["Motivo"], excluded_rows)

    # Monthly debit note (grouped bank fees)
    debit_note_rows = []
    for p in data["nota_debito"]:
        for c in p["categorias"]:
            obs = ""
            if "iva_esperado" in c:
                diff = round(c["importe"] - c["iva_esperado"], 2)
                obs = f'IVA esperado $ {c["iva_esperado"]:,.2f} (dif {diff:,.2f})'
            debit_note_rows.append((p["periodo"], c["categoria"], c["cantidad"], c["importe"], obs))
        debit_note_rows.append((p["periodo"], "TOTAL SEGÚN EXTRACTO", "", p["total_extracto"], ""))
        for a in p["asientos_mayor"]:
            side = a["haber"] or -a["debe"]
            debit_note_rows.append((p["periodo"], f'Mayor [{a["hoja"]}] {a["referencia"]} — {a["comentario"]}',
                                    "", side, "asiento del mayor"))
        if p["total_mayor"] is not None:
            debit_note_rows.append((p["periodo"], "TOTAL SEGÚN MAYOR", "", p["total_mayor"], ""))
            debit_note_rows.append((p["periodo"], "DIFERENCIA (extracto - mayor)", "",
                                    p["diferencia"], ""))
        debit_note_rows.append(("", "", "", "", ""))
    sheet("Gastos por mes (ND)",
          ["Período", "Concepto", "Cant.", "Importe", "Observación"], debit_note_rows)

    # Detail of concepts per period (to investigate differences)
    concept_rows = []
    for p in data["nota_debito"]:
        for c in p["conceptos"]:
            concept_rows.append((p["periodo"], c["descripcion"], c["cantidad"], c["importe"]))
    sheet("Gastos detalle conceptos",
          ["Período", "Concepto del extracto", "Cant.", "Importe"], concept_rows)

    # Matched by hand (N to M groups)
    manual_rows = []
    for n, m in enumerate(data.get("conciliados_manual", []), 1):
        for b in m["banco"]:
            manual_rows.append((n, "BANCO", b["fecha"], b["comprobante"],
                                f'{b["descripcion"]} {b["detalle"]}'.strip(),
                                b["debito"] or "", b["credito"] or "", "", m["nota"]))
        for a in m["mayor"]:
            manual_rows.append((n, f'MAYOR {a["hoja"]}', a["fecha"], a["asiento"],
                                f'{a["referencia"]} — {a["comentario"]}',
                                a["haber"] or "", a["debe"] or "", "", ""))
        manual_rows.append((n, "→ diferencia", "", "", "", "", "", m["diferencia"],
                            "aceptada de IA" if m["fuente"] == "ia" else ""))
    sheet("Conciliados manualmente",
          ["Grupo", "Lado", "Fecha", "Comp/Asiento", "Descripción",
           "Débito/Haber", "Crédito/Debe", "Diferencia", "Nota"], manual_rows)

    if data["ia"]["sugerencias"]:
        sheet("Sugerencias IA",
              ["IDs banco", "IDs mayor", "Confianza", "Motivo"],
              [(", ".join(s["ids_banco"]), ", ".join(s["ids_mayor"]),
                s["confianza"], s["motivo"]) for s in data["ia"]["sugerencias"]])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


@app.get("/api/demo/estado")
def api_demo_state():
    if not DEMO_MODE:
        return {"demo": False}
    from engine import simulacion
    return {"demo": True, "hoy": simulacion.ultimo_habil().isoformat()}


def _demo_accounts(brand: str = "") -> list[dict]:
    """The hub accounts with FBS (the ones the simulation knows) of the brand."""
    from engine import simulacion
    everything = fbs_sql.hub_fbs_accounts()
    return simulacion.con_roles(fbs_sql._filter_configs(everything, brand), everything)


def _demo_range(date_from: str, date_to: str):
    from engine import simulacion
    try:
        d1 = date.fromisoformat(date_from) if date_from else simulacion.ultimo_habil()
        d2 = date.fromisoformat(date_to) if date_to else d1
    except ValueError:
        return None
    if d1 > d2:
        d1, d2 = d2, d1
    return d1, min(d2, date.today())


@app.get("/api/demo/cuentas")
def api_demo_accounts(marca: str = ""):
    """Accounts to generate statements for in the demo, with their bank's format."""
    if not DEMO_MODE:
        return JSONResponse(status_code=404, content={"error": "No disponible"})
    from engine import simulacion
    return {"cuentas": [{"cuenta_id": c["cuenta_id"], "etiqueta": c["etiqueta"],
                         "banco": c["banco"], "moneda": c["moneda"], "rol": c["rol"],
                         "formato": simulacion.FORMATOS.get(c["banco"], c["banco"])}
                        for c in sorted(_demo_accounts(marca), key=lambda x: x["etiqueta"])],
            "hoy": simulacion.ultimo_habil().isoformat()}


@app.get("/api/demo/extracto")
def api_demo_statement(cuenta: str, desde: str = "", hasta: str = "", marca: str = ""):
    """The statement of one account for the range, in its bank's format and
    consistent with the ledger the simulated FBS returns."""
    if not DEMO_MODE:
        return JSONResponse(status_code=404, content={"error": "No disponible"})
    from engine import simulacion
    date_range = _demo_range(desde, hasta)
    c = next((x for x in _demo_accounts(marca) if x["cuenta_id"] == cuenta), None)
    if not date_range or not c:
        return JSONResponse(status_code=404, content={"error": "Esa cuenta no tiene FBS en el hub de la demo."})
    try:
        name, content, n = simulacion.extracto(c, *date_range)
    except RuntimeError as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
    kind = ("text/csv" if name.endswith(".csv") else "application/vnd.ms-excel"
            if name.endswith(".xls") else
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return Response(content=content, media_type=kind, headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "X-Movimientos": str(n), "Cache-Control": "no-store"})


@app.get("/api/demo/extractos")
def api_demo_statements(marca: str = "", desde: str = "", hasta: str = ""):
    """The statements of every account of the brand, in a .zip."""
    if not DEMO_MODE:
        return JSONResponse(status_code=404, content={"error": "No disponible"})
    from engine import simulacion
    date_range = _demo_range(desde, hasta)
    accounts = _demo_accounts(marca)
    if not date_range or not accounts:
        return JSONResponse(status_code=404, content={"error": "No hay cuentas con FBS en el hub de la demo."})
    content = simulacion.zip_extractos(accounts, *date_range)
    d1, d2 = date_range
    name = f"extractos_{accounts_mod.slug(marca) or 'grupo'}_{d1.isoformat()}" + \
             ("" if d1 == d2 else f"_a_{d2.isoformat()}") + ".zip"
    return Response(content=content, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


_JOB_FILE_RE = re.compile(r"^[0-9a-f]{12}\.json$")
_RUN_FILE_RE = re.compile(r"^grupo_[0-9a-f]{10}\.json$")


@app.post("/api/demo/reset")
def api_demo_reset():
    """Clears the demo's accumulated state (ROD-10 "09"): whatever is in the
    memory (movements/entries already consumed), the carry-over between runs,
    and the reconciliation history (jobs and boards) — without this, repeating
    the same statement showed it as "already reconciled" instead of running it
    again. The statements and the simulated FBS store nothing: they always
    come out the same from engine/simulacion.py."""
    if not DEMO_MODE:
        return JSONResponse(status_code=404, content={"error": "No disponible"})
    STAGING.clear()
    RESULTS.clear()
    for path in (CARRY_OVER_PATH, MATCHED_MEMORY_PATH):
        if os.path.exists(path):
            os.remove(path)
    for name in os.listdir(DATA_DIR):
        if _JOB_FILE_RE.match(name) or _RUN_FILE_RE.match(name):
            os.remove(os.path.join(DATA_DIR, name))
    return {"ok": True}


if DEMO_MODE:
    @app.middleware("http")
    async def _demo_no_index(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp


@app.get("/diario")
def daily_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "diario.html"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    # no cache: after each deploy the browser must fetch the new UI
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"),
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8765"))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    # proxy_headers=False: the client IP and "is local" are decided by
    # engine/seguridad.py, not by uvicorn (which would trust X-Forwarded-For)
    uvicorn.run(app, host=host, port=port, proxy_headers=False)
