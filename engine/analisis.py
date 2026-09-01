# -*- coding: utf-8 -*-
"""Análisis con IA de asientos pendientes, con confirmación del usuario.

Para un asiento de "O pendientes sin banco" la IA explica por qué quedó sin
contrapartida en el extracto. Después el usuario SIEMPRE confirma:
  - "Sí, está bien" -> la explicación se convierte en regla: los asientos con
    la misma firma quedan anotados automáticamente en corridas futuras.
  - "No" + corrección del cliente -> también se aprende: la corrección se
    guarda, se muestra en asientos similares y se le pasa a la IA como
    contexto en los próximos análisis.
"""
import json
import os
from datetime import date

from engine import reglas as reglas_mod
from engine import ai_assist

_DATOS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "datos")
RUTA_DEFAULT = os.path.join(_DATOS, "analisis_aprendidos.json")
RUTA_CACHE = os.path.join(_DATOS, "analisis_cache.json")
RUTA_USO = os.path.join(_DATOS, "analisis_uso.json")

# Haiku 4.5: $1/$5 por millón de tokens — un análisis cuesta ~$0,003.
MODELO = "claude-haiku-4-5"
# tope de llamadas a la API por día (más allá, análisis interno sin IA)
MAX_LLAMADAS_DIA = int(os.environ.get("ANALISIS_MAX_DIA", "150"))
MAX_CACHE = 5000


def cargar(ruta: str = RUTA_DEFAULT) -> list[dict]:
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _guardar(items: list[dict], ruta: str):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def aprender(firma: str, explicacion_ia: str, ok: bool, correccion: str = "",
             ruta: str = RUTA_DEFAULT) -> dict:
    """Registra el veredicto del usuario sobre un análisis."""
    items = cargar(ruta)
    veredicto = "ok" if ok else "corregido"
    for it in items:
        if it["firma"] == firma:
            it["veredicto"] = veredicto
            it["explicacion"] = explicacion_ia[:600]
            if correccion:
                it["correccion"] = correccion.strip()[:600]
            it["veces"] = it.get("veces", 1) + 1
            it["actualizada"] = date.today().isoformat()
            _guardar(items, ruta)
            return it
    nuevo = {"firma": firma, "veredicto": veredicto,
             "explicacion": explicacion_ia[:600],
             "correccion": (correccion or "").strip()[:600],
             "veces": 1, "actualizada": date.today().isoformat()}
    items.append(nuevo)
    _guardar(items, ruta)
    return nuevo


def buscar(firma: str, items: list[dict]) -> dict | None:
    return next((it for it in items if it["firma"] == firma), None)


def anotar_residuales(datos: dict, ruta: str = RUTA_DEFAULT):
    """Marca los asientos residuales cuya firma ya tiene un análisis
    confirmado o corregido por el usuario (la 'regla' del análisis)."""
    items = cargar(ruta)
    if not items:
        return
    for lista in ("e_sin_banco", "o_pendientes_sin_banco"):
        for x in datos.get(lista, []):
            firma = reglas_mod.firma_mayor(x.get("referencia"), x.get("comentario"))
            it = buscar(firma, items)
            if it:
                x["analisis_previo"] = {
                    "veredicto": it["veredicto"],
                    "nota": it.get("correccion") or it.get("explicacion", ""),
                }


SYSTEM = """Sos un analista contable experto en conciliaciones bancarias de
Argentina. Se te muestra UN asiento del libro mayor que quedó SIN contrapartida
en el extracto bancario después de la conciliación automática (la cuenta O son
órdenes de pago y recibos pendientes de confirmación; la E son confirmados).

Explicá en 2 a 4 frases, en español claro para un contador, por qué ese asiento
puede haber quedado pendiente: el pago todavía no salió del banco, salió con
otro importe o con comisión descontada, corresponde a otra cuenta bancaria, es
una anulación o duplicado, el banco lo agrupa con otros movimientos, etc.
Usá los movimientos residuales del extracto como evidencia: si ves un candidato
probable para el cruce, decilo explícitamente con su fecha e importe. Si el
cliente ya corrigió análisis de asientos parecidos, respetá ese criterio.
No inventes datos que no estén en el contexto."""


def _leer_json(ruta, defecto):
    if os.path.exists(ruta):
        try:
            with open(ruta, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return defecto


def _escribir_json(ruta, datos):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False)


def clave_asiento(a: dict) -> str:
    return (f'{a.get("hoja")}|{a.get("asiento")}|{a.get("fecha")}|'
            f'{a.get("debe") or 0:.2f}|{a.get("haber") or 0:.2f}|'
            f'{(a.get("referencia") or "")[:30]}')


def _consumir_presupuesto() -> bool:
    """True si todavía queda presupuesto de llamadas de IA para hoy."""
    uso = _leer_json(RUTA_USO, {})
    hoy = date.today().isoformat()
    if uso.get("fecha") != hoy:
        uso = {"fecha": hoy, "llamadas": 0}
    if uso["llamadas"] >= MAX_LLAMADAS_DIA:
        return False
    uso["llamadas"] += 1
    _escribir_json(RUTA_USO, uso)
    return True


def _interno(asiento: dict, banco: list[dict], motivo: str = "") -> str:
    """Análisis determinístico sin IA: mejor esfuerzo con los datos locales."""
    imp = (asiento.get("debe") or 0) or (asiento.get("haber") or 0)
    lado_banco = "credito" if (asiento.get("debe") or 0) else "debito"
    partes = []
    if motivo:
        partes.append(motivo)
    partes.append(
        "Este asiento no encontró en el extracto ningún movimiento del mismo "
        f"importe (${imp:,.2f}) y lado dentro de la tolerancia de fechas.")
    candidatos = sorted(
        (b for b in banco
         if (b.get("credito") if lado_banco == "credito" else b.get("debito"))),
        key=lambda b: abs(((b.get("credito") or 0) or (b.get("debito") or 0)) - imp))[:2]
    for c in candidatos:
        ic = (c.get("credito") or 0) or (c.get("debito") or 0)
        dif = ic - imp
        if abs(dif) < imp * 0.05 or abs(dif) < 5000:
            partes.append(
                f'El residual más parecido es "{c.get("descripcion")}" del '
                f'{c.get("fecha")} por ${ic:,.2f} (diferencia ${dif:+,.2f}): '
                "podría ser el mismo pago con comisión o redondeo — conviene "
                "revisarlo en la solapa de conciliación manual.")
            break
    else:
        partes.append(
            "Lo más probable es que el pago aún no se haya ejecutado en el "
            "banco, corresponda a otra cuenta, o el banco lo agrupe con otros "
            "movimientos.")
    return "Análisis interno (sin IA): " + " ".join(partes)


def _simulado(asiento: dict, banco: list[dict]) -> str:
    imp = (asiento.get("debe") or 0) or (asiento.get("haber") or 0)
    cand = min(banco, key=lambda b: abs(((b.get("credito") or 0) or (b.get("debito") or 0)) - imp)) \
        if banco else None
    extra = ""
    if cand:
        ic = (cand.get("credito") or 0) or (cand.get("debito") or 0)
        extra = (f' El residual más cercano del banco es "{cand.get("descripcion")}" '
                 f'del {cand.get("fecha")} por $ {ic:,.2f}.')
    return ("SIMULACIÓN (sin IA): este asiento no encontró un movimiento del "
            "extracto con el mismo importe y lado dentro de la tolerancia de "
            "fechas; lo más probable es que el pago aún no se haya ejecutado "
            "en el banco o haya salido con otro importe." + extra)


def analizar_asiento(asiento: dict, banco_residual: list[dict],
                     correcciones: list[dict], cuenta: str = "",
                     simular: bool = False) -> dict:
    """Explicación de por qué el asiento quedó pendiente.

    Cascada de costo: caché persistente (mismo asiento en corridas
    anteriores, cero API) -> presupuesto diario -> IA (Haiku, prompt
    recortado) -> análisis interno sin IA ante cualquier problema.
    Devuelve {"texto", "origen"} con origen en ia|cache|interno|simulado."""
    if simular:
        return {"texto": _simulado(asiento, banco_residual), "origen": "simulado"}

    clave = clave_asiento(asiento)
    cache = _leer_json(RUTA_CACHE, {})
    if clave in cache:
        return {"texto": cache[clave]["texto"], "origen": "cache"}

    if not ai_assist.disponible():
        return {"texto": _interno(asiento, banco_residual,
                                  "No hay credenciales de IA configuradas."),
                "origen": "interno"}
    if not _consumir_presupuesto():
        return {"texto": _interno(
            asiento, banco_residual,
            f"Se alcanzó el tope diario de {MAX_LLAMADAS_DIA} análisis con IA "
            "(protección de costos)."), "origen": "interno"}

    try:
        import anthropic

        imp = (asiento.get("debe") or 0) or (asiento.get("haber") or 0)
        cercanos = sorted(
            banco_residual,
            key=lambda b: abs(((b.get("credito") or 0) or (b.get("debito") or 0)) - imp))[:12]
        filas_banco = [{k: v for k, v in {
            "fecha": b.get("fecha"),
            "descripcion": (b.get("descripcion") or "")[:80],
            "detalle": (b.get("detalle") or "")[:60] or None,
            "comprobante": b.get("comprobante"),
            "credito": b.get("credito") or None,
            "debito": b.get("debito") or None,
        }.items() if v} for b in cercanos]

        prompt = (
            (f"CUENTA: {cuenta}\n" if cuenta else "")
            + "ASIENTO PENDIENTE A EXPLICAR:\n"
            + json.dumps({k: asiento.get(k) for k in
                          ("hoja", "asiento", "fecha", "referencia", "comentario",
                           "debe", "haber")}, ensure_ascii=False)
            + "\n\nRESIDUALES DEL EXTRACTO MÁS CERCANOS POR IMPORTE:\n"
            + json.dumps(filas_banco, ensure_ascii=False)
        )
        if correcciones:
            prompt += ("\n\nCORRECCIONES PREVIAS DEL CLIENTE (criterio a respetar):\n"
                       + json.dumps([{"asiento_tipo": c["firma"],
                                      "correccion": (c.get("correccion") or "")[:200]}
                                     for c in correcciones[:5]], ensure_ascii=False))

        ai_assist._cargar_clave()
        client = anthropic.Anthropic()
        r = client.messages.create(
            model=MODELO, max_tokens=400, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = next((b.text for b in r.content if b.type == "text"), "").strip()
        if not texto:
            return {"texto": _interno(asiento, banco_residual,
                                      "La IA no devolvió análisis."), "origen": "interno"}
        cache[clave] = {"texto": texto, "fecha": date.today().isoformat(),
                        "modelo": MODELO}
        if len(cache) > MAX_CACHE:      # limitar el tamaño del archivo
            viejas = sorted(cache, key=lambda k: cache[k].get("fecha", ""))
            for k in viejas[:len(cache) - MAX_CACHE]:
                cache.pop(k, None)
        _escribir_json(RUTA_CACHE, cache)
        return {"texto": texto, "origen": "ia"}
    except Exception as exc:  # noqa: BLE001 — nunca romper: análisis interno
        return {"texto": _interno(asiento, banco_residual,
                                  f"La IA no respondió ({exc})."),
                "origen": "interno"}
