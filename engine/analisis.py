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

RUTA_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "datos", "analisis_aprendidos.json")

MODELO = "claude-sonnet-5"


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
                     simular: bool = False) -> str:
    """Devuelve la explicación de la IA para un asiento pendiente."""
    if simular:
        return _simulado(asiento, banco_residual)
    if not ai_assist.disponible():
        raise RuntimeError("No hay credenciales de IA configuradas (ANTHROPIC_API_KEY)")

    import anthropic

    imp = (asiento.get("debe") or 0) or (asiento.get("haber") or 0)
    cercanos = sorted(
        banco_residual,
        key=lambda b: abs(((b.get("credito") or 0) or (b.get("debito") or 0)) - imp))[:40]
    filas_banco = [{
        "fecha": b.get("fecha"), "descripcion": b.get("descripcion"),
        "detalle": b.get("detalle"), "comprobante": b.get("comprobante"),
        "credito": b.get("credito"), "debito": b.get("debito"),
    } for b in cercanos]

    prompt = (
        (f"CUENTA: {cuenta}\n\n" if cuenta else "")
        + "ASIENTO PENDIENTE A EXPLICAR:\n"
        + json.dumps({k: asiento.get(k) for k in
                      ("hoja", "asiento", "fecha", "referencia", "comentario",
                       "debe", "haber")}, ensure_ascii=False)
        + "\n\nMOVIMIENTOS RESIDUALES DEL EXTRACTO (sin conciliar, los más "
          "cercanos por importe):\n"
        + json.dumps(filas_banco, ensure_ascii=False)
    )
    if correcciones:
        prompt += ("\n\nCORRECCIONES PREVIAS DEL CLIENTE sobre análisis de "
                   "asientos parecidos (criterio a respetar):\n"
                   + json.dumps([{"asiento_tipo": c["firma"],
                                  "correccion": c.get("correccion", "")}
                                 for c in correcciones], ensure_ascii=False))

    ai_assist._cargar_clave()
    client = anthropic.Anthropic()
    r = client.messages.create(
        model=MODELO, max_tokens=700, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in r.content if b.type == "text"), "").strip()
