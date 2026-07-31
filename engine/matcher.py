# -*- coding: utf-8 -*-
"""Motor de conciliación bancaria.

Lógica (según el flujo del sistema contable FES):
  - La cuenta O (operativa/transitoria) recibe por defecto las órdenes de pago
    y recibos. Al confirmarse un movimiento se hace la contrapartida en O y el
    asiento pasa a la cuenta E, que debería reflejar el extracto bancario.
  - Conciliar = extracto vs cuenta E; lo que no está en E se busca en la O
    (movimientos pendientes de confirmación).

Convención de signos:
  crédito del banco (entra plata)  <->  Debe del mayor
  débito del banco  (sale plata)   <->  Haber del mayor
"""
import re
from collections import defaultdict

# Palabras clave de gastos/impuestos bancarios que usualmente no se registran
# asiento por asiento en el mayor.
GASTO_RE = re.compile(
    r'comision|iva |iva 21|iva 10|impuesto|sircreb|percepcion|iibb|'
    r'ley ?2[57]\.?[47]?\d*|mantenimiento|sellados|intereses|com\.? ',
    re.IGNORECASE,
)

NUM_RE = re.compile(r'\d{5,}')

# --- Nota de débito mensual de gastos bancarios -----------------------------
# Los gastos no se contabilizan día por día: se agrupan en una sola nota de
# débito mensual, separando comisiones gravadas al 21%, intereses al 10,5%
# (cada grupo con su IVA) y los conceptos no gravados (ley 25.413, SIRCREB,
# percepciones).
CATEGORIAS_GASTO = [
    ("IVA 10,5%", re.compile(r'iva\s*10[.,]?5', re.I)),
    ("IVA 21%", re.compile(r'iva', re.I)),
    ("Impuesto ley 25.413", re.compile(r'ley\s*25\.?413', re.I)),
    ("SIRCREB", re.compile(r'sircreb', re.I)),
    ("Percepciones IIBB", re.compile(r'iibb|percepcion', re.I)),
    ("Intereses saldo deudor (grav. 10,5%)", re.compile(r'inter[eé]s', re.I)),
    ("Comisiones (grav. 21%)", re.compile(r'comision|mantenimiento|sellado|com\.? ', re.I)),
]
ORDEN_CATEGORIAS = [
    "Comisiones (grav. 21%)", "IVA 21%",
    "Intereses saldo deudor (grav. 10,5%)", "IVA 10,5%",
    "Impuesto ley 25.413", "SIRCREB", "Percepciones IIBB", "Otros",
]
ND_RE = re.compile(r'gastos?\s*banc', re.I)
NC_RE = re.compile(r'impuestos\s+liq|nota\s+de\s+cr[eé]dito|\bNC-', re.I)
MESES = {1: 'ENE', 2: 'FEB', 3: 'MAR', 4: 'ABR', 5: 'MAY', 6: 'JUN',
         7: 'JUL', 8: 'AGO', 9: 'SEP', 10: 'OCT', 11: 'NOV', 12: 'DIC'}
MES_TXT_RE = re.compile(
    r'\b(ENE(?:RO)?|FEB(?:RERO)?|MAR(?:ZO)?|ABR(?:IL)?|MAY(?:O)?|JUN(?:IO)?|'
    r'JUL(?:IO)?|AGO(?:STO)?|SEP(?:TIEMBRE)?|OCT(?:UBRE)?|NOV(?:IEMBRE)?|DIC(?:IEMBRE)?)\b',
    re.I)


def _categoria_gasto(descripcion: str) -> str:
    for nombre, rx in CATEGORIAS_GASTO:
        if rx.search(descripcion):
            return nombre
    return "Otros"


def _mes_de_asiento(a) -> str | None:
    """Mes (p.ej. 'ENE') al que corresponde un asiento de ND del mayor."""
    m = MES_TXT_RE.search(f"{a.referencia} {a.comentario}")
    if m:
        return m.group(1)[:3].upper()
    return MESES.get(a.fecha.month) if a.fecha else None


def resumen_nota_debito(gastos, asientos_libres):
    """Agrupa los gastos bancarios por período de extracto y los compara con
    las notas de débito mensuales registradas en el mayor.

    Devuelve (periodos, ids_asientos_usados)."""
    por_archivo = defaultdict(list)
    for g in gastos:
        por_archivo[g.archivo].append(g)

    # candidatos del mayor: ND (haber) y NC de impuestos (debe)
    nds = [a for a in asientos_libres if a.haber and ND_RE.search(f"{a.referencia} {a.comentario}")]
    ncs = [a for a in asientos_libres if a.debe and NC_RE.search(f"{a.referencia} {a.comentario}")]

    periodos = []
    usados = set()
    for archivo in sorted(por_archivo):
        items = por_archivo[archivo]
        fechas = [g.fecha for g in items if g.fecha]
        hasta = max(fechas) if fechas else None
        mes = MESES.get(hasta.month) if hasta else None
        etiqueta = f"{mes}/{hasta.strftime('%y')}" if hasta else archivo

        cats = defaultdict(lambda: {"cantidad": 0, "importe": 0.0})
        conceptos = defaultdict(lambda: {"cantidad": 0, "importe": 0.0})
        for g in items:
            neto = g.debito - g.credito
            c = _categoria_gasto(g.descripcion)
            cats[c]["cantidad"] += 1
            cats[c]["importe"] += neto
            conceptos[g.descripcion.strip()]["cantidad"] += 1
            conceptos[g.descripcion.strip()]["importe"] += neto

        filas_cat = []
        for nombre in ORDEN_CATEGORIAS:
            if nombre not in cats:
                continue
            fila = {"categoria": nombre,
                    "cantidad": cats[nombre]["cantidad"],
                    "importe": round(cats[nombre]["importe"], 2)}
            if nombre == "IVA 21%" and "Comisiones (grav. 21%)" in cats:
                fila["iva_esperado"] = round(0.21 * cats["Comisiones (grav. 21%)"]["importe"], 2)
            if nombre == "IVA 10,5%" and "Intereses saldo deudor (grav. 10,5%)" in cats:
                fila["iva_esperado"] = round(0.105 * cats["Intereses saldo deudor (grav. 10,5%)"]["importe"], 2)
            filas_cat.append(fila)

        total_extracto = round(sum(f["importe"] for f in filas_cat), 2)

        # asientos del mayor correspondientes a este mes
        asientos_periodo = [a for a in nds if _mes_de_asiento(a) == mes]
        # NCs del mes siguiente al período también corresponden (se liquidan después)
        asientos_nc = [a for a in ncs
                       if a.fecha and hasta and 0 <= (a.fecha - hasta).days <= 40]
        total_mayor = round(sum(a.haber for a in asientos_periodo)
                            - sum(a.debe for a in asientos_nc), 2)
        for a in asientos_periodo + asientos_nc:
            usados.add(a.id)

        periodos.append({
            "periodo": etiqueta,
            "archivo": archivo,
            "categorias": filas_cat,
            "conceptos": sorted(
                [{"descripcion": k, **v, "importe": round(v["importe"], 2)}
                 for k, v in conceptos.items()],
                key=lambda x: -abs(x["importe"])),
            "total_extracto": total_extracto,
            "asientos_mayor": asientos_periodo + asientos_nc,
            "total_mayor": total_mayor if (asientos_periodo or asientos_nc) else None,
            "diferencia": round(total_extracto - total_mayor, 2)
                          if (asientos_periodo or asientos_nc) else None,
        })
    return periodos, usados


def _numeros(texto: str) -> set[str]:
    """Extrae números de referencia (5+ dígitos) de un texto."""
    return {n.lstrip('0') for n in NUM_RE.findall(texto or '') if n.lstrip('0')}


def _clave_rm(texto: str) -> str | None:
    m = re.search(r'RM-?\s*(\d+)', texto or '', re.IGNORECASE)
    return m.group(1).lstrip('0') if m else None


def netear_cuenta_o(asientos_o):
    """Cancela dentro de la O los pares confirmados (asiento + contrapartida).

    Un movimiento confirmado aparece en O dos veces: una en cada lado, con el
    mismo importe y el mismo comprobante (RM / referencia). Lo que queda sin
    cancelar son los movimientos pendientes de confirmación.
    """
    debe = defaultdict(list)
    haber = defaultdict(list)
    for a in asientos_o:
        clave = (_clave_rm(a.comentario) or a.referencia.strip().upper(), round(a.importe, 2))
        (debe if a.lado == 'debe' else haber)[clave].append(a)

    cancelados = set()
    for clave, lista_d in debe.items():
        lista_h = haber.get(clave, [])
        n = min(len(lista_d), len(lista_h))
        for k in range(n):
            cancelados.add(lista_d[k].id)
            cancelados.add(lista_h[k].id)
    pendientes = [a for a in asientos_o if a.id not in cancelados]
    return pendientes, cancelados


def _match_pases(movs_banco, asientos, tolerancia_dias=45):
    """Concilia movimientos del banco contra asientos del mayor.

    Devuelve (matches, banco_sin_match, asientos_sin_match) donde matches es
    una lista de dicts {banco, asiento, metodo}.
    """
    matches = []
    banco_libres = list(movs_banco)
    asiento_libres = list(asientos)

    def indexar(asientos_):
        idx = defaultdict(list)
        for a in asientos_:
            lado_banco = 'credito' if a.lado == 'debe' else 'debito'
            idx[(lado_banco, round(a.importe, 2))].append(a)
        return idx

    # --- Pase 1: importe + número de referencia compartido -----------------
    idx = indexar(asiento_libres)
    usados_a, usados_b = set(), set()
    for m in banco_libres:
        candidatos = idx.get((m.lado, round(m.importe, 2)), [])
        texto_b = f"{m.comprobante or ''} {m.descripcion} {m.detalle}"
        nums_b = _numeros(texto_b)
        if not nums_b:
            continue
        for a in candidatos:
            if a.id in usados_a:
                continue
            nums_a = _numeros(f"{a.referencia} {a.comentario}")
            if nums_b & nums_a:
                matches.append({"banco": m, "asiento": a, "metodo": "importe+referencia"})
                usados_a.add(a.id)
                usados_b.add(m.id)
                break
    banco_libres = [m for m in banco_libres if m.id not in usados_b]
    asiento_libres = [a for a in asiento_libres if a.id not in usados_a]

    # --- Pase 2: importe único (un solo candidato de cada lado) ------------
    idx = indexar(asiento_libres)
    idx_b = defaultdict(list)
    for m in banco_libres:
        idx_b[(m.lado, round(m.importe, 2))].append(m)
    usados_a, usados_b = set(), set()
    for clave, lista_b in idx_b.items():
        lista_a = idx.get(clave, [])
        if len(lista_b) == 1 and len(lista_a) == 1:
            m, a = lista_b[0], lista_a[0]
            if m.fecha and a.fecha and abs((m.fecha - a.fecha).days) > tolerancia_dias:
                continue
            matches.append({"banco": m, "asiento": a, "metodo": "importe único"})
            usados_b.add(m.id)
            usados_a.add(a.id)
    banco_libres = [m for m in banco_libres if m.id not in usados_b]
    asiento_libres = [a for a in asiento_libres if a.id not in usados_a]

    # --- Pase 3: mismo importe, asignación por fecha más cercana -----------
    idx = indexar(asiento_libres)
    usados_a, usados_b = set(), set()
    por_clave = defaultdict(list)
    for m in banco_libres:
        por_clave[(m.lado, round(m.importe, 2))].append(m)
    for clave, lista_b in por_clave.items():
        lista_a = [a for a in idx.get(clave, [])]
        if not lista_a:
            continue
        # greedy por menor distancia de fechas
        pares = []
        for m in lista_b:
            for a in lista_a:
                if m.fecha and a.fecha:
                    d = abs((m.fecha - a.fecha).days)
                else:
                    d = 9999
                if d <= tolerancia_dias:
                    pares.append((d, m, a))
        pares.sort(key=lambda p: p[0])
        for d, m, a in pares:
            if m.id in usados_b or a.id in usados_a:
                continue
            matches.append({"banco": m, "asiento": a,
                            "metodo": f"importe+fecha (±{d}d)"})
            usados_b.add(m.id)
            usados_a.add(a.id)
    banco_libres = [m for m in banco_libres if m.id not in usados_b]
    asiento_libres = [a for a in asiento_libres if a.id not in usados_a]

    return matches, banco_libres, asiento_libres


def conciliar(movs_banco, asientos_e, asientos_o, reglas_aprendidas=None,
              equivalencias=None):
    """Ejecuta la conciliación completa. Devuelve un dict serializable."""
    from engine import reglas as reglas_mod
    from engine import equivalencias as eq_mod

    pendientes_o, cancelados_o = netear_cuenta_o(asientos_o)

    # 1) extracto vs cuenta E
    matches_e, banco_sin_e, e_sin_banco = _match_pases(movs_banco, asientos_e)

    # 2) lo que no está en E se busca en la O pendiente de confirmación
    matches_o, banco_sin_nada, o_pend_sin_banco = _match_pases(banco_sin_e, pendientes_o)

    # 2b) reglas aprendidas de conciliaciones manuales anteriores
    pares_regla, usados_b, usados_a = reglas_mod.aplicar(
        banco_sin_nada, e_sin_banco + o_pend_sin_banco, reglas_aprendidas or [])
    reglas_aplicadas = len(pares_regla)
    if pares_regla:
        banco_sin_nada = [m for m in banco_sin_nada if m.id not in usados_b]
        e_sin_banco = [a for a in e_sin_banco if a.id not in usados_a]
        o_pend_sin_banco = [a for a in o_pend_sin_banco if a.id not in usados_a]
        for p in pares_regla:
            (matches_e if p["asiento"].hoja == 'E' else matches_o).append(p)

    # 2c) diccionario de equivalencias de vocabulario (p. ej. haberes ≈ sueldos)
    pares_eq, usados_b, usados_a = eq_mod.aplicar(
        banco_sin_nada, e_sin_banco + o_pend_sin_banco, equivalencias or [])
    equivalencias_aplicadas = len({p["banco"].id for p in pares_eq})
    if pares_eq:
        banco_sin_nada = [m for m in banco_sin_nada if m.id not in usados_b]
        e_sin_banco = [a for a in e_sin_banco if a.id not in usados_a]
        o_pend_sin_banco = [a for a in o_pend_sin_banco if a.id not in usados_a]
        for p in pares_eq:
            (matches_e if p["asiento"].hoja == 'E' else matches_o).append(p)

    # 3) clasificar restos del banco
    gastos_bancarios, sin_contabilizar = [], []
    for m in banco_sin_nada:
        if GASTO_RE.search(m.descripcion):
            gastos_bancarios.append(m)
        else:
            sin_contabilizar.append(m)

    # 4) nota de débito mensual: agrupar gastos por período y cruzar contra
    #    los asientos "GASTOS BANCARIOS" del mayor
    nota_debito, ids_nd = resumen_nota_debito(
        gastos_bancarios, e_sin_banco + o_pend_sin_banco)
    e_sin_banco = [a for a in e_sin_banco if a.id not in ids_nd]
    o_pend_sin_banco = [a for a in o_pend_sin_banco if a.id not in ids_nd]

    tot = lambda ms: round(sum(x.importe for x in ms), 2)
    tot_a = lambda as_: round(sum(x.importe for x in as_), 2)

    return {
        "nota_debito": nota_debito,
        "matches_e": matches_e,
        "matches_o": matches_o,
        "banco_sin_contabilizar": sin_contabilizar,
        "gastos_bancarios": gastos_bancarios,
        "e_sin_banco": e_sin_banco,
        "o_pendientes_sin_banco": o_pend_sin_banco,
        "o_cancelados": len(cancelados_o),
        "o_pendientes_total": len(pendientes_o),
        "resumen": {
            "movimientos_banco": len(movs_banco),
            "conciliados_e": len(matches_e),
            "en_o_pendientes_confirmar": len(matches_o),
            "reglas_aplicadas": reglas_aplicadas,
            "equivalencias_aplicadas": equivalencias_aplicadas,
            "gastos_bancarios": {"cantidad": len(gastos_bancarios), "importe": tot(gastos_bancarios)},
            "banco_sin_contabilizar": {"cantidad": len(sin_contabilizar), "importe": tot(sin_contabilizar)},
            "e_sin_banco": {"cantidad": len(e_sin_banco), "importe": tot_a(e_sin_banco)},
            "o_pendientes_sin_banco": {"cantidad": len(o_pend_sin_banco), "importe": tot_a(o_pend_sin_banco)},
            "porcentaje_conciliado": round(
                100.0 * (len(matches_e) + len(matches_o)) / max(1, len(movs_banco)), 1),
            "nota_debito": {
                "periodos": len(nota_debito),
                "diferencia_total": round(
                    sum(p["diferencia"] for p in nota_debito
                        if p["diferencia"] is not None), 2),
            },
        },
    }
