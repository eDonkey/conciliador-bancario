# -*- coding: utf-8 -*-
"""Asistencia con IA (Claude) para los casos que la conciliación determinística
no pudo resolver: diferencias de importe (comisiones descontadas), agrupaciones
(varios movimientos del banco = un asiento, o viceversa) y coincidencias por
descripción/beneficiario.

Requiere ANTHROPIC_API_KEY en el entorno (o sesión de `ant auth login`).
"""
import json
import os

MODEL = "claude-opus-5"
MAX_BANCO = 80     # límites por llamada para no exceder contexto
MAX_MAYOR = 400

RUTA_CLAVE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "datos", "anthropic_key.txt")


def _cargar_clave() -> bool:
    """Busca credenciales: variable de entorno o datos/anthropic_key.txt."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if os.path.exists(RUTA_CLAVE):
        try:
            clave = open(RUTA_CLAVE, encoding="utf-8").read().strip()
        except OSError:
            return False
        if clave:
            os.environ["ANTHROPIC_API_KEY"] = clave
            return True
    return False

SCHEMA = {
    "type": "object",
    "properties": {
        "sugerencias": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ids_banco": {"type": "array", "items": {"type": "string"}},
                    "ids_mayor": {"type": "array", "items": {"type": "string"}},
                    "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
                    "motivo": {"type": "string"},
                },
                "required": ["ids_banco", "ids_mayor", "confianza", "motivo"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sugerencias"],
    "additionalProperties": False,
}

SYSTEM = """Sos un experto en conciliaciones bancarias de Argentina.
Recibís movimientos de un extracto de Banco Santander que no pudieron conciliarse
automáticamente, y asientos del libro mayor (cuenta E = confirmados, cuenta O =
pendientes de confirmación) que tampoco tienen contrapartida.

Convención: un crédito del banco corresponde a un Debe del mayor; un débito del
banco corresponde a un Haber del mayor.

Proponé emparejamientos probables considerando:
- combinaciones 1 a N o N a 1 cuya suma coincida (o difiera por una comisión chica),
- coincidencias de beneficiario/CUIT/número de comprobante en los textos,
- proximidad de fechas.
Solo proponé pares donde tengas un argumento concreto; no fuerces coincidencias.
Indicá confianza "alta" solo si los importes cierran exactamente o hay un número
de referencia compartido."""


def disponible() -> bool:
    if _cargar_clave():
        return True
    # perfil de `ant auth login`
    cfg = os.path.join(os.path.expanduser("~"), ".config", "anthropic")
    appdata = os.path.join(os.environ.get("APPDATA", ""), "Anthropic")
    return os.path.isdir(cfg) or os.path.isdir(appdata)


def _fila_banco(m):
    return {
        "id": m.id, "fecha": m.fecha.isoformat() if m.fecha else None,
        "lado": m.lado, "importe": m.importe,
        "texto": f"{m.comprobante or ''} {m.descripcion} {m.detalle}".strip(),
    }


def _fila_mayor(a):
    return {
        "id": a.id, "hoja": a.hoja,
        "fecha": a.fecha.isoformat() if a.fecha else None,
        "lado": a.lado, "importe": a.importe,
        "texto": f"{a.referencia} {a.comentario}".strip(),
    }


def cantidad_tandas(movs_banco) -> int:
    return max(1, -(-len(movs_banco) // MAX_BANCO))  # ceil


def _bloque_glosario(glosario) -> str:
    """Texto del diccionario de equivalencias para incluir en el prompt."""
    if not glosario:
        return ""
    lineas = "\n".join(f'- "{p["extracto"]}" (extracto) ≈ "{p["sistema"]}" (sistema)'
                       for p in glosario)
    return ("\n\nGLOSARIO DE EQUIVALENCIAS definido por el cliente — el mismo "
            "concepto tiene distinto nombre en el extracto y en el sistema "
            "contable; usalo para vincular movimientos:\n" + lineas)


def simular_sugerencias(movs_banco, asientos_mayor, progreso=None, glosario=None):
    """Modo simulación: genera sugerencias de prueba SIN llamar a la API.
    Emula la latencia por tandas para poder probar la interfaz de progreso."""
    import time

    if not movs_banco or not asientos_mayor:
        return []
    tandas = min(3, cantidad_tandas(movs_banco))
    for t in range(tandas):
        if progreso:
            progreso(t + 1, tandas)
        time.sleep(4)

    sugerencias = []
    confianzas = ["alta", "media", "baja"]
    usados = set()
    for i, m in enumerate(movs_banco[:12]):
        candidato = None
        for a in asientos_mayor:
            if a.id in usados:
                continue
            lado_ok = (m.lado == "credito") == (a.lado == "debe")
            if lado_ok:
                if candidato is None or abs(a.importe - m.importe) < abs(candidato.importe - m.importe):
                    candidato = a
        if candidato is None:
            continue
        usados.add(candidato.id)
        sugerencias.append({
            "ids_banco": [m.id],
            "ids_mayor": [candidato.id],
            "confianza": confianzas[i % 3],
            "motivo": f"SIMULACIÓN (sin IA): par de prueba por cercanía de importe "
                      f"(${m.importe:,.2f} vs ${candidato.importe:,.2f}).",
        })
    return sugerencias


def sugerir_matches(movs_banco, asientos_mayor, progreso=None, glosario=None):
    """Pide a Claude sugerencias de conciliación. Devuelve lista de dicts.
    `progreso(tanda, total)` se invoca al comenzar cada tanda."""
    import anthropic

    if not movs_banco or not asientos_mayor:
        return []

    _cargar_clave()
    client = anthropic.Anthropic()
    sugerencias = []
    total_tandas = cantidad_tandas(movs_banco)

    for i in range(0, len(movs_banco), MAX_BANCO):
        if progreso:
            progreso(i // MAX_BANCO + 1, total_tandas)
        lote_banco = movs_banco[i:i + MAX_BANCO]
        lote_mayor = asientos_mayor[:MAX_MAYOR]
        prompt = (
            "MOVIMIENTOS DEL BANCO SIN CONCILIAR:\n"
            + json.dumps([_fila_banco(m) for m in lote_banco], ensure_ascii=False)
            + "\n\nASIENTOS DEL MAYOR SIN CONTRAPARTIDA:\n"
            + json.dumps([_fila_mayor(a) for a in lote_mayor], ensure_ascii=False)
            + _bloque_glosario(glosario)
        )
        try:
            # Con fallback del lado del servidor: si un clasificador de seguridad
            # rechaza el pedido, la API lo reintenta con otro modelo.
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                system=SYSTEM,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": prompt}],
                extra_body={"fallbacks": "default"},
            )
        except anthropic.BadRequestError:
            # SDK/organización sin soporte del beta de fallbacks: llamada normal
            response = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": prompt}],
            )
        if response.stop_reason == "refusal":
            continue
        texto = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(texto)
            sugerencias.extend(data.get("sugerencias", []))
        except (json.JSONDecodeError, AttributeError):
            continue

    return sugerencias
