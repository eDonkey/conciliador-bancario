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
from datetime import date

# Palabras clave de gastos/impuestos bancarios que usualmente no se registran
# asiento por asiento en el mayor.
# Ojo con los términos cortos: sin límite de palabra, "iva " matchea
# "cooperatIVA de seguros" y "com " matchea "teleCOM" (pasó con pagos a
# proveedores reales que cayeron como gastos).
GASTO_RE = re.compile(
    # 25413 suelto cubre el "DBCR 25413 S/DB" del Macro; "inter." cubre los
    # intereses abreviados ("INTER.ADEL.CC S/ACUERD")
    r'comision|(?<![a-z])iva(?![a-z])|impuesto|percepcion|iibb|'
    r'ing\.?\s*brutos|ley ?2[57]\.?[47]?\d*|(?<!\d)25\.?413(?!\d)|mantenimiento|'
    r'sellados|intereses|(?<![a-z])inter\.|(?<![a-z])com\.? ',
    re.IGNORECASE,
)

# SIRCREB: la retencion de ingresos brutos que el banco aplica sobre cada
# acreditacion. Parece un gasto bancario mas, pero NO entra en la nota de
# debito mensual: se hace un recibo aparte, porque se recupera despues contra
# el impuesto. Entonces queda como pendiente de contabilizar (hasta que hagan
# el recibo) y ademas se lista aparte, con el total, para poder hacerlo.
# (?<![a-z])sirc cubre "SIRCREB" (Santander) y el abreviado "REG REC SIRC"
# del BBVA/Frances.
RETENCION_RE = re.compile(r'(?<![a-z])sirc', re.IGNORECASE)


def es_retencion_texto(texto: str) -> bool:
    return bool(RETENCION_RE.search(texto or ""))

NUM_RE = re.compile(r'\d{5,}')

# Ventana para los cruces "a ciegas": mismo importe, sin comprobante ni una
# sola palabra en común. Más allá de esto es más probable que sean dos
# operaciones distintas del mismo monto que la misma.
TOL_CIEGO_DIAS = 10

# Gastos bancarios y pagos de sueldos: tienen solapa propia y el cliente los
# quiere ahí, no pegados al asiento de un proveedor que da parecido. Pueden
# cruzar por comprobante (pase 1) y, sin comprobante, solo con el importe
# EXACTO y prácticamente el mismo día. Nunca por importe aproximado.
TOL_ETIQUETADO_DIAS = 3

# Pagos de sueldos. Tienen solapa propia: con esa lista se arma el asiento de
# sueldos en el FBS y al día siguiente cruzan solos por comprobante.
TERMINOS_HABERES = ("haber", "sueldo", "nomina")


def sin_acentos(texto: str) -> str:
    import unicodedata
    plano = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in plano if not unicodedata.combining(c))


def es_haberes_texto(texto: str) -> bool:
    """'pago haberes', 'acred. sueldos', 'acreditación de nómina'…"""
    plano = sin_acentos(texto)
    return any(t in plano for t in TERMINOS_HABERES)

# --- Nota de débito mensual de gastos bancarios -----------------------------
# Los gastos no se contabilizan día por día: se agrupan en una sola nota de
# débito mensual, separando comisiones gravadas al 21%, intereses al 10,5%
# (cada grupo con su IVA) y los conceptos no gravados (ley 25.413, SIRCREB,
# percepciones).
CATEGORIAS_GASTO = [
    ("IVA 10,5%", re.compile(r'iva\s*10[.,]?5', re.I)),
    ("IVA 21%", re.compile(r'iva', re.I)),
    ("Impuesto ley 25.413", re.compile(r'ley\s*25\.?413|(?<!\d)25\.?413(?!\d)', re.I)),
    ("SIRCREB", re.compile(r'(?<![a-z])sirc', re.I)),
    ("Percepciones IIBB", re.compile(r'iibb|percep', re.I)),   # "percep" abreviado incluido
]
# comisiones e intereses pueden estar gravados al 21% o al 10,5% (feedback del
# cliente): si el concepto trae la alícuota en el texto se respeta; si no, el
# default histórico (comisiones -> 21%, intereses -> 10,5%)
COMISION_RE = re.compile(r'comision|mantenimiento|sellado|com\.? ', re.I)
INTERES_RE = re.compile(r'inter[eé]s|(?<![a-z])inter\.', re.I)
TASA_105_RE = re.compile(r'10[.,]5')
TASA_21_RE = re.compile(r'(?<![\d.,])21(?![\d.,])')
ORDEN_CATEGORIAS = [
    "Comisiones (grav. 21%)", "Intereses saldo deudor (grav. 21%)", "IVA 21%",
    "Comisiones (grav. 10,5%)", "Intereses saldo deudor (grav. 10,5%)", "IVA 10,5%",
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
    d = descripcion or ""
    for nombre, rx in CATEGORIAS_GASTO:
        if rx.search(d):
            return nombre
    if INTERES_RE.search(d):
        if TASA_21_RE.search(d) and not TASA_105_RE.search(d):
            return "Intereses saldo deudor (grav. 21%)"
        return "Intereses saldo deudor (grav. 10,5%)"
    if COMISION_RE.search(d):
        if TASA_105_RE.search(d):
            return "Comisiones (grav. 10,5%)"
        return "Comisiones (grav. 21%)"
    return "Otros"


def _mes_de_asiento(a) -> str | None:
    """Mes (p.ej. 'ENE') al que corresponde un asiento de ND del mayor."""
    m = MES_TXT_RE.search(f"{a.referencia} {a.comentario}")
    if m:
        return m.group(1)[:3].upper()
    return MESES.get(a.fecha.month) if a.fecha else None


def _mes_retencion(r) -> str:
    if not r.fecha:
        return "sin fecha"
    return f"{MESES.get(r.fecha.month, '?')}/{r.fecha.strftime('%y')}"


def resumen_retenciones(retenciones, asientos_libres):
    """Agrupa las retenciones (SIRCREB) por mes y busca en el mayor el recibo
    que las registra.

    El recibo no se puede identificar por el texto: sale con la misma
    referencia que cualquier otro movimiento del mes (un "PO 03513" igual que
    la nota de debito). Se identifica por el importe: que un asiento suelto
    coincida al centavo con el total de las retenciones del mes es prueba de
    sobra. Visto en una conciliacion real: 13 retenciones de septiembre,
    16.210,58 en total, y un unico asiento libre por 16.210,58.

    Devuelve (periodos, ids de asientos usados)."""
    por_mes = defaultdict(list)
    for r in retenciones:
        if r.fecha:
            por_mes[(r.fecha.year, r.fecha.month)].append(r)
        else:
            por_mes[(0, 0)].append(r)

    periodos, usados = [], set()
    for _, items in sorted(por_mes.items()):
        total = round(sum(r.debito - r.credito for r in items), 2)
        etiqueta = _mes_retencion(items[0])
        hasta = max((r.fecha for r in items if r.fecha), default=None)
        # el recibo se hace a fin de mes, a veces en los dias siguientes
        candidatos = [a for a in asientos_libres
                      if a.id not in usados and a.haber
                      and abs(a.haber - total) <= 0.01
                      and (not a.fecha or not hasta
                           or -31 <= (a.fecha - hasta).days <= 40)]
        recibo = candidatos[0] if len(candidatos) == 1 else None
        if recibo:
            usados.add(recibo.id)
        periodos.append({
            "periodo": etiqueta,
            "movimientos": len(items),
            "total_extracto": total,
            "asientos_mayor": [recibo] if recibo else [],
            "total_mayor": round(recibo.haber, 2) if recibo else None,
            "ambiguo": len(candidatos) > 1,
        })
    return periodos, usados


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
            "movimientos": len(items),
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


# --- Tolerancia de importe -------------------------------------------------
# Diferencias chicas entre banco y mayor (comisión descontada en la misma
# transferencia, redondeos de liquidación). Solo se acepta el par si el
# candidato es ÚNICO en ambos sentidos dentro de la tolerancia.
TOL_ABS = 100.0     # piso: ±$100
TOL_PCT = 0.001     # 0,1% del importe
TOL_TOPE = 2000.0   # techo absoluto
# La comisión descontada o el redondeo pasan EN la misma operación: el asiento
# está el mismo día o ahí nomás. Con 45 días esto emparejaba cualquier par de
# importes parecidos del mes (feedback del cliente: "se acerca un poquito el
# monto y ya te hace una sugerencia"). Si además comparten texto, se afloja.
TOL_DIAS = 7
TOL_DIAS_CON_TEXTO = 45


def _tolerancia(importe: float) -> float:
    return min(max(TOL_ABS, importe * TOL_PCT), TOL_TOPE)


def match_tolerancia(banco_libres, asiento_libres, etiquetados=frozenset()):
    """Matches 1 a 1 con diferencia chica de importe. Devuelve
    (pares, ids_banco_usados, ids_mayor_usados).

    Los movimientos de `etiquetados` (gastos bancarios y pagos de sueldos)
    no entran: tienen su propia solapa y no se cruzan por parecido de importe."""
    import bisect

    banco_libres = [m for m in banco_libres if m.id not in etiquetados]
    por_lado_a = {"credito": [], "debito": []}
    for a in asiento_libres:
        lado_banco = 'credito' if a.lado == 'debe' else 'debito'
        por_lado_a[lado_banco].append(a)
    por_lado_b = {"credito": [], "debito": []}
    for m in banco_libres:
        por_lado_b[m.lado].append(m)
    for d in (por_lado_a, por_lado_b):
        for lista in d.values():
            lista.sort(key=lambda x: x.importe)
    imps_a = {k: [a.importe for a in v] for k, v in por_lado_a.items()}
    imps_b = {k: [m.importe for m in v] for k, v in por_lado_b.items()}

    def cerca(x, y):
        f1, f2 = x.fecha, y.fecha
        if not f1 or not f2:
            return False
        tope = TOL_DIAS_CON_TEXTO if _afinidad_par(x, y) else TOL_DIAS
        return abs((f1 - f2).days) <= tope

    def candidatos(ref, lista, imps):
        tol = _tolerancia(ref.importe)
        out = []
        i = bisect.bisect_left(imps, ref.importe - tol)
        while i < len(lista) and imps[i] <= ref.importe + tol:
            x = lista[i]
            if abs(x.importe - ref.importe) > 0.005 and cerca(ref, x):
                out.append(x)
            i += 1
        return out

    pares, usados_b, usados_a = [], set(), set()
    for m in banco_libres:
        cands = candidatos(m, por_lado_a[m.lado], imps_a[m.lado])
        if len(cands) != 1 or cands[0].id in usados_a:
            continue
        a = cands[0]
        # el asiento también debe tener a este movimiento como único candidato
        reversos = candidatos(a, por_lado_b[m.lado], imps_b[m.lado])
        if len(reversos) != 1 or reversos[0].id != m.id:
            continue
        dif = round(m.importe - a.importe, 2)
        pares.append({"banco": m, "asiento": a,
                      "metodo": f"importe aproximado (dif $ {dif:+,.2f})"})
        usados_b.add(m.id)
        usados_a.add(a.id)
    return pares, usados_b, usados_a


PALABRA_RE = re.compile(r'[a-zñ]{4,}')
# palabras que aparecen en casi todos los textos y no distinguen nada
VACIAS = {'banco', 'santander', 'frances', 'galicia', 'macro', 'ciudad', 'cuenta',
          'corriente', 'pesos', 'transferencia', 'realizada', 'recibida', 'pago',
          'pagos', 'terceros', 'varios', 'fecha', 'importe', 'movimiento'}


def _palabras(texto: str) -> set[str]:
    """Palabras significativas de un texto, sin acentos ni relleno."""
    t = (texto or '').lower()
    for a, b in (('á', 'a'), ('é', 'e'), ('í', 'i'), ('ó', 'o'), ('ú', 'u'), ('ü', 'u')):
        t = t.replace(a, b)
    return set(PALABRA_RE.findall(t)) - VACIAS


def _texto_item(x) -> str:
    """Texto buscable, sirva un movimiento del banco o un asiento del mayor."""
    if hasattr(x, "descripcion"):
        return f"{x.descripcion} {x.detalle}"
    return f"{x.referencia} {x.comentario}"


def _afinidad_par(x, y) -> int:
    return len(_palabras(_texto_item(x)) & _palabras(_texto_item(y)))


def _afinidad(m, a) -> int:
    """Cuánto se parecen los textos de un movimiento y un asiento (palabras en
    común). No alcanza por sí sola para cruzar: el mayor del FBS casi nunca
    repite el texto del banco, así que exigirla perdería cruces buenos. Se usa
    para desempatar entre candidatos y para decidir cuánto se puede estirar la
    fecha: compartiendo texto, lejos; a ciegas, al lado."""
    return _afinidad_par(m, a)


def _numeros(texto: str) -> set[str]:
    """Extrae números de referencia (5+ dígitos) de un texto."""
    return {n.lstrip('0') for n in NUM_RE.findall(texto or '') if n.lstrip('0')}


def _clave_rm(texto: str) -> str | None:
    m = re.search(r'RM-?\s*(\d+)', texto or '', re.IGNORECASE)
    return m.group(1).lstrip('0') if m else None


def netear_contraasientos(asientos):
    """Detecta pares asiento + contraasiento dentro de un mismo residual del
    mayor: misma clave (RM o referencia), mismo importe, lados opuestos.
    Son movimientos anulados que nunca van a aparecer en el extracto.

    Devuelve (restantes, pares) donde pares es [{debe, haber}]."""
    debe = defaultdict(list)
    haber = defaultdict(list)
    for a in asientos:
        clave = (_clave_rm(a.comentario) or a.referencia.strip().upper(), round(a.importe, 2))
        (debe if a.lado == 'debe' else haber)[clave].append(a)

    pares, cancelados = [], set()
    for clave, lista_d in debe.items():
        lista_h = haber.get(clave, [])
        for k in range(min(len(lista_d), len(lista_h))):
            pares.append({"debe": lista_d[k], "haber": lista_h[k]})
            cancelados.add(lista_d[k].id)
            cancelados.add(lista_h[k].id)
    restantes = [a for a in asientos if a.id not in cancelados]
    return restantes, pares


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


def _match_pases(movs_banco, asientos, tolerancia_dias=45, etiquetados=frozenset()):
    """Concilia movimientos del banco contra asientos del mayor.

    Devuelve (matches, banco_sin_match, asientos_sin_match) donde matches es
    una lista de dicts {banco, asiento, metodo}.

    `etiquetados` son ids de gastos bancarios y pagos de sueldos: cruzan por
    comprobante como cualquiera, pero sin comprobante solo con el importe
    exacto y casi el mismo día. Sin esto, un IVA o un pago de haberes se
    llevaba puesto el asiento de un proveedor del mismo importe de otra
    semana, y encima lo consumía para siempre.
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

    # De acá en adelante se cruza sin comprobante en común: a los gastos y a
    # los pagos de sueldos se les achica la ventana a unos días.
    def tope_dias(m, base):
        return min(base, TOL_ETIQUETADO_DIAS) if m.id in etiquetados else base

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
            # para un movimiento común la ventana no se achica: que el importe
            # sea único en los dos lados de TODO el período ya es evidencia
            # fuerte, y en la mensual el asiento aparece semanas después.
            if m.fecha and a.fecha and abs((m.fecha - a.fecha).days) > tope_dias(m, tolerancia_dias):
                continue
            detalle = "" if _afinidad(m, a) else " · sin comprobante ni texto en común"
            matches.append({"banco": m, "asiento": a,
                            "metodo": f"importe único{detalle}"})
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
        # greedy por menor distancia de fechas; entre empates gana el que
        # comparte palabras (nombre del beneficiario, concepto) con el asiento
        pares = []
        for m in lista_b:
            for a in lista_a:
                if m.fecha and a.fecha:
                    d = abs((m.fecha - a.fecha).days)
                else:
                    d = 9999
                af = _afinidad(m, a)
                # sin comprobante ni texto en común el cruce es a ciegas: ahí
                # la ventana se achica. Con 45 días terminaba cruzando un pago
                # con un asiento ajeno del mismo importe de dos semanas antes
                # (feedback del cliente) y consumiéndolo para siempre.
                if d <= tope_dias(m, tolerancia_dias if af else TOL_CIEGO_DIAS):
                    pares.append((d, -af, m, a))
        pares.sort(key=lambda p: (p[0], p[1], p[2].id, p[3].id))
        for d, menos_af, m, a in pares:
            if m.id in usados_b or a.id in usados_a:
                continue
            # aviso honesto: acá no hubo comprobante en común, solo importe y
            # fecha. Es el cruce que conviene revisar a mano.
            detalle = "" if menos_af else " · sin comprobante ni texto en común"
            matches.append({"banco": m, "asiento": a,
                            "metodo": f"importe+fecha (±{d}d){detalle}"})
            usados_b.add(m.id)
            usados_a.add(a.id)
    asiento_libres = [a for a in asiento_libres if a.id not in usados_a]

    # los apartados (gastos, haberes) nunca se perdieron: vuelven a la lista de
    # pendientes en su orden original, que es donde el cliente los espera
    cruzados = {m["banco"].id for m in matches}
    banco_libres = [m for m in movs_banco if m.id not in cruzados]

    return matches, banco_libres, asiento_libres


def _etiqueta_previa(m, terminos_gasto=None, excepciones_gasto=None):
    """'gasto', 'haberes' o None, mirando SOLO el movimiento del banco.

    Es la misma decisión que después arma las solapas, adelantada al cruce:
    lo que va a terminar en Gastos bancarios o en Haberes no se cruza a ciegas
    contra un asiento cualquiera del mismo importe."""
    from engine import gastos_conf

    forzado = getattr(m, "_forzar_gasto", None)
    if forzado is True:          # reclasificado a mano: pisa todo
        return "gasto"
    if forzado is None and not gastos_conf.es_gasto(m.descripcion, excepciones_gasto) \
            and (GASTO_RE.search(m.descripcion)
                 or RETENCION_RE.search(m.descripcion)
                 or gastos_conf.es_gasto(m.descripcion, terminos_gasto)):
        return "gasto"
    if es_haberes_texto(f"{m.descripcion} {m.detalle}"):
        return "haberes"
    return None


def conciliar(movs_banco, asientos_e, asientos_o, reglas_aprendidas=None,
              equivalencias=None, terminos_gasto=None, excepciones_gasto=None):
    """Ejecuta la conciliación completa. Devuelve un dict serializable."""
    from engine import reglas as reglas_mod
    from engine import equivalencias as eq_mod
    from engine import gastos_conf

    pendientes_o, cancelados_o = netear_cuenta_o(asientos_o)

    # 0) qué es cada movimiento del banco ANTES de cruzar. Los gastos e
    # impuestos bancarios y los pagos de sueldos van a su propia solapa: se
    # pueden cruzar por comprobante, pero nunca "porque el importe da".
    etiquetas = {m.id: _etiqueta_previa(m, terminos_gasto, excepciones_gasto)
                 for m in movs_banco}
    etiquetados = {i for i, e in etiquetas.items() if e}

    # 1) extracto vs cuenta E
    matches_e, banco_sin_e, e_sin_banco = _match_pases(
        movs_banco, asientos_e, etiquetados=etiquetados)

    # 2) lo que no está en E se busca en la O pendiente de confirmación
    matches_o, banco_sin_nada, o_pend_sin_banco = _match_pases(
        banco_sin_e, pendientes_o, etiquetados=etiquetados)

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

    # 2d) tolerancia estricta de importe (comisiones/redondeos), sin ambigüedad
    pares_tol, usados_b, usados_a = match_tolerancia(
        banco_sin_nada, e_sin_banco + o_pend_sin_banco,
        etiquetados=etiquetados)
    aproximados = len(pares_tol)
    if pares_tol:
        banco_sin_nada = [m for m in banco_sin_nada if m.id not in usados_b]
        e_sin_banco = [a for a in e_sin_banco if a.id not in usados_a]
        o_pend_sin_banco = [a for a in o_pend_sin_banco if a.id not in usados_a]
        for p in pares_tol:
            (matches_e if p["asiento"].hoja == 'E' else matches_o).append(p)

    # 3) clasificar restos del banco (patrón de fábrica + gastos del usuario).
    # Un movimiento reclasificado a mano trae _forzar_gasto (True/False) y esa
    # decisión pisa a los detectores — así la ND mensual también sale bien.
    gastos_bancarios, retenciones, sin_contabilizar = [], [], []
    for m in banco_sin_nada:
        forzado = getattr(m, "_forzar_gasto", None)
        if forzado is None and es_retencion_texto(m.descripcion):
            # SIRCREB: no va a la ND, va un recibo. Queda entre los pendientes
            # de contabilizar (es plata del banco que el mayor todavia no
            # tiene) y repetido en su propia lista para hacer el recibo.
            retenciones.append(m)
            continue
        if m.id in etiquetas:
            # misma decisión que se tomó antes de cruzar: lo apartado del
            # cruce a ciegas es exactamente lo que termina en esta solapa
            es_gasto = etiquetas[m.id] == "gasto"
        elif forzado is not None:
            # decisión manual sobre ESTE movimiento: pisa todo
            es_gasto = forzado
        elif gastos_conf.es_gasto(m.descripcion, excepciones_gasto):
            # concepto aprendido como "nunca es gasto": pisa a los detectores
            es_gasto = False
        else:
            es_gasto = bool(GASTO_RE.search(m.descripcion)
                            or gastos_conf.es_gasto(m.descripcion, terminos_gasto))
        (gastos_bancarios if es_gasto else sin_contabilizar).append(m)

    # 4) nota de débito mensual: agrupar gastos por período y cruzar contra
    #    los asientos "GASTOS BANCARIOS" del mayor
    nota_debito, ids_nd = resumen_nota_debito(
        gastos_bancarios, e_sin_banco + o_pend_sin_banco)
    e_sin_banco = [a for a in e_sin_banco if a.id not in ids_nd]
    o_pend_sin_banco = [a for a in o_pend_sin_banco if a.id not in ids_nd]

    # 4 bis) SIRCREB: lo mismo pero contra el recibo del mes. El mes que tiene
    #        recibo queda explicado; el que no, vuelve a los pendientes de
    #        contabilizar, que es lo que son hasta que lo hagan.
    retencion_periodos, ids_ret = resumen_retenciones(
        retenciones, e_sin_banco + o_pend_sin_banco)
    e_sin_banco = [a for a in e_sin_banco if a.id not in ids_ret]
    o_pend_sin_banco = [a for a in o_pend_sin_banco if a.id not in ids_ret]
    meses_sin_recibo = {p["periodo"] for p in retencion_periodos
                        if p["total_mayor"] is None}
    if meses_sin_recibo:
        sin_contabilizar.extend(r for r in retenciones
                                if _mes_retencion(r) in meses_sin_recibo)
        sin_contabilizar.sort(key=lambda m: (m.fecha or date(1900, 1, 1), str(m.id)))

    # 5) contraasientos: pares anulados dentro del residual del mayor (asiento
    #    y su contrapartida que nunca van a tener movimiento en el banco)
    e_sin_banco, contraasientos = netear_contraasientos(e_sin_banco)

    tot = lambda ms: round(sum(x.importe for x in ms), 2)
    tot_a = lambda as_: round(sum(x.importe for x in as_), 2)

    # % explicado: además de los conciliados, los gastos bancarios cuyo período
    # cruzó contra una nota de débito del mayor están justificados aunque no
    # tengan asiento 1 a 1.
    gastos_explicados = sum(p["movimientos"] for p in nota_debito
                            if p["total_mayor"] is not None)
    # idem las retenciones cuyo mes cruzo contra el recibo del mayor
    retenciones_explicadas = sum(p["movimientos"] for p in retencion_periodos
                                 if p["total_mayor"] is not None)
    explicados = (len(matches_e) + len(matches_o)
                  + gastos_explicados + retenciones_explicadas)

    return {
        "nota_debito": nota_debito,
        "contraasientos": contraasientos,
        "matches_e": matches_e,
        "matches_o": matches_o,
        "banco_sin_contabilizar": sin_contabilizar,
        "gastos_bancarios": gastos_bancarios,
        "retenciones": retenciones,
        "retencion_periodos": retencion_periodos,
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
            "aproximados": aproximados,
            "gastos_explicados": gastos_explicados,
            "retenciones_explicadas": retenciones_explicadas,
            "contraasientos": len(contraasientos),
            "gastos_bancarios": {"cantidad": len(gastos_bancarios), "importe": tot(gastos_bancarios)},
            "retenciones": {"cantidad": len(retenciones), "importe": tot(retenciones)},
            "banco_sin_contabilizar": {"cantidad": len(sin_contabilizar), "importe": tot(sin_contabilizar)},
            "e_sin_banco": {"cantidad": len(e_sin_banco), "importe": tot_a(e_sin_banco)},
            "o_pendientes_sin_banco": {"cantidad": len(o_pend_sin_banco), "importe": tot_a(o_pend_sin_banco)},
            "porcentaje_conciliado": round(
                100.0 * (len(matches_e) + len(matches_o)) / max(1, len(movs_banco)), 1),
            "porcentaje_explicado": round(
                min(100.0, 100.0 * explicados / max(1, len(movs_banco))), 1),
            "nota_debito": {
                "periodos": len(nota_debito),
                "diferencia_total": round(
                    sum(p["diferencia"] for p in nota_debito
                        if p["diferencia"] is not None), 2),
            },
        },
    }
