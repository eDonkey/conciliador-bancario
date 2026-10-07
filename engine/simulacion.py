# -*- coding: utf-8 -*-
"""FBS simulado del ambiente de demo (DEMO_MODE).

En la demo no hay SQL Server: el mayor E/O de cada cuenta del hub y el
extracto de su banco salen de la MISMA simulación. Lo que se descarga como
extracto y lo que después "trae el FBS" cuentan la misma historia, con los
casos de todos los días de una concesionaria:

  - cobros de clientes por transferencia, depósitos de cheques (acreditan a
    las 48 hs), liquidaciones de tarjetas, pagos a proveedores y a la
    terminal, cheques propios que se debitan días después de emitidos, VEPs
    de AFIP, haberes (sin contabilizar: el asiento de sueldos es de fin de
    mes) e impuestos y comisiones del banco (van a la nota de débito);
  - la mayoría confirmados (asiento en E y su par en O), algunos todavía en
    O (pendientes de confirmar), alguno sin registrar en el FBS y alguno con
    una diferencia chica de importe (el cliente descontó la comisión).

Es determinística: cada operación sale de una semilla (cuenta + día en que
nació), así el mismo día da siempre lo mismo, un cheque emitido el lunes
aparece en el extracto del miércoles y lo que no cruzó hoy cruza mañana con
el arrastre, como en una cuenta real.

Las cuentas son las del hub (cuentas_bancarias con conexión FBS e IDs E/O):
la demo muestra las marcas y bancos que se hayan sembrado ahí.
"""
from __future__ import annotations

import csv
import hashlib
import io
import random
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# hasta cuántos días antes nace una operación que impacta hoy (cheques)
VENTANA = 14
EPOCA = date(2024, 1, 1)


@dataclass
class Mov:
    fecha: date
    descripcion: str
    detalle: str
    comprobante: str
    importe: float           # + crédito / - débito
    orden: int = 0


@dataclass
class Asiento:
    hoja: str                # 'E' | 'O'
    fecha: date
    numero: int
    referencia: str
    comentario: str
    debe: float = 0.0
    haber: float = 0.0


@dataclass
class Operacion:
    mov: Mov | None
    asientos: list[Asiento] = field(default_factory=list)
    gasto: bool = False      # comisión o impuesto del banco: va a la nota de débito del mes


# ---------------------------------------------------------------- utilidades

def _rng(*partes) -> random.Random:
    h = hashlib.sha256("|".join(str(p) for p in partes).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _plano(s: str) -> str:
    t = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in t if not unicodedata.combining(c))


def _habil(d: date) -> bool:
    return d.weekday() < 5


def _mas_habiles(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if _habil(d):
            n -= 1
    return d


def _habiles(desde: date, hasta: date):
    d = desde
    while d <= hasta:
        if _habil(d):
            yield d
        d += timedelta(days=1)


def ultimo_habil(hoy: date | None = None) -> date:
    d = hoy or date.today()
    while not _habil(d):
        d -= timedelta(days=1)
    return d


def _ar(n: float) -> str:
    """1234567.8 -> '1.234.567,80' (sin signo)."""
    s = f"{abs(n):,.2f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def _redondo(r: random.Random, minimo: float, maximo: float, paso: float = 1000.0) -> float:
    """Un importe: casi siempre redondo (como paga la gente), a veces con centavos."""
    v = r.uniform(minimo, maximo)
    if r.random() < 0.6:
        return float(round(v / paso) * paso)
    return round(v, 2)


# ------------------------------------------------------------------ nombres

APELLIDOS = ("GONZALEZ", "RODRIGUEZ", "GOMEZ", "FERNANDEZ", "LOPEZ", "DIAZ", "MARTINEZ", "PEREZ", "GARCIA",
             "SANCHEZ", "ROMERO", "SOSA", "TORRES", "ALVAREZ", "RUIZ", "RAMIREZ", "FLORES", "ACOSTA",
             "BENITEZ", "MEDINA", "SUAREZ", "HERRERA", "AGUIRRE", "PEREYRA", "GUTIERREZ", "GIMENEZ",
             "MOLINA", "SILVA", "CASTRO", "ROJAS", "ORTIZ", "LUNA", "JUAREZ", "CABRERA", "RIOS", "GODOY",
             "MORALES", "DOMINGUEZ", "PERALTA", "VEGA", "CARRIZO", "QUIROGA", "CASTILLO", "LEDESMA", "OJEDA")
NOMBRES = ("MARIA", "JUAN", "JOSE", "CARLOS", "ANA", "LUIS", "JORGE", "LAURA", "SILVIA", "MARCELO", "DANIEL",
           "PATRICIA", "CLAUDIA", "ROBERTO", "MIGUEL", "SANDRA", "ALEJANDRO", "GABRIELA", "SERGIO", "LUCIANA",
           "FEDERICO", "FLORENCIA", "MARTIN", "PAULA", "NICOLAS", "ROMINA", "DIEGO", "CECILIA", "GUSTAVO")
EMPRESAS_CLIENTE = ("AGROSERVICIOS EL ALGARROBO SA", "TRANSPORTES DEL VALLE SRL", "CONSTRUCTORA LOS ANDES SA",
                    "DISTRIBUIDORA NORTE GRANDE SRL", "LOGISTICA PAMPEANA SA", "ESTUDIO CONTABLE RIVERO",
                    "FRIGORIFICO SAN JORGE SA", "SERVICIOS PETROLEROS PATAGONIA SA")
# sin palabras que el conciliador lee como gasto (comision, iva, impuesto, intereses…)
PROVEEDORES = ("LUBRICANTES PATAGONICOS SA", "AUTOPARTES DEL CENTRO SRL", "GRUPO LOGISTICO DEL SUR SA",
               "LIMPIEZA INTEGRAL BAHIA SRL", "SEGURIDAD PRIVADA ATLAS SA", "AGENCIA CREATIVA NORTE SA",
               "SERVICIOS DE GRUA RUTA 2 SRL", "NEUMATICOS LA PAMPA SA", "FERRETERIA INDUSTRIAL SAN MARTIN",
               "GESTORIA DEL AUTOMOTOR LOPEZ", "TALLER DE CHAPA Y PINTURA ROSALES", "DATACOR INFORMATICA SA",
               "COMBUSTIBLES DEL OESTE SA", "LAVADERO EXPRESS SRL", "IMPRENTA GRAFICA DEL PLATA",
               "SEGUROS LA COSTERA SA", "EDENOR SA", "TELECOM ARGENTINA SA")

# la terminal de cada marca (por palabra clave del nombre de la marca en el hub)
TERMINALES = (
    (("fiat", "jeep", "ram"), "FCA AUTOMOBILES ARGENTINA SA"),
    (("peugeot", "citroen", "ds"), "STELLANTIS ARGENTINA SA"),
    (("renault",), "RENAULT ARGENTINA SA"),
    (("toyota",), "TOYOTA ARGENTINA SA"),
    (("volkswagen", "vw"), "VOLKSWAGEN ARGENTINA SA"),
    (("chevrolet",), "GENERAL MOTORS DE ARGENTINA SRL"),
    (("ford",), "FORD ARGENTINA SCA"),
    (("nissan",), "NISSAN ARGENTINA SA"),
    (("honda",), "HONDA MOTOR DE ARGENTINA SA"),
    (("hyundai",), "HYUNDAI MOTOR ARGENTINA SA"),
)


def terminal_de(marca: str) -> str:
    palabras = set(re.split(r"[^a-z0-9]+", _plano(marca).lower()))
    for claves, nombre in TERMINALES:
        if palabras & set(claves):
            return nombre
    principal = next((p for p in re.split(r"\s*[-–·/]\s*", marca or "") if p.strip()), "LA TERMINAL")
    return f"{_plano(principal).upper().strip()} ARGENTINA SA"


def _persona(r: random.Random) -> tuple[str, str]:
    """(nombre, CUIT sin guiones) de un cliente: casi siempre una persona."""
    if r.random() < 0.15:
        return r.choice(EMPRESAS_CLIENTE), f"30{r.randint(50_000_000, 72_000_000)}{r.randint(0, 9)}"
    nombre = f"{r.choice(APELLIDOS)} {r.choice(NOMBRES)}"
    return nombre, f"{r.choice(('20', '27', '23'))}{r.randint(14_000_000, 46_000_000)}{r.randint(0, 9)}"


def _cuit_proveedor(nombre: str) -> str:
    h = int(hashlib.sha256(nombre.encode()).hexdigest()[:8], 16)
    return f"30{50_000_000 + h % 22_000_000}{h % 10}"


# ------------------------------------------------------------- la cuenta

def con_roles(cuentas: list[dict], todas: list[dict] | None = None) -> list[dict]:
    """Copias de las cuentas con su rol: la primera en pesos de cada marca
    (por orden de alta en el hub) es la principal y mueve más. El rol se
    decide sobre todas las cuentas, así no cambia según cuáles se elijan."""
    todas = todas if todas is not None else cuentas
    principales, vistas = set(), set()
    for c in sorted(todas, key=lambda x: (x.get("db_id") or 0)):
        marca = (c.get("empresa") or "").strip().lower()
        if (c.get("moneda") or "ARS").upper() == "ARS" and marca not in vistas:
            vistas.add(marca)
            principales.add(c["cuenta_id"])
    return [{**c, "rol": "principal" if c["cuenta_id"] in principales else "secundaria"}
            for c in cuentas]


def _escala(cuenta: dict) -> float:
    """Volumen de la cuenta: la principal de la marca mueve más."""
    r = _rng("escala", cuenta["cuenta_id"])
    return (1.0 if cuenta.get("rol") == "principal" else 0.4) * r.uniform(0.75, 1.3)


def _numerador(cuenta: dict, d: date):
    """Números de asiento crecientes y estables por cuenta y día."""
    base = 100_000 + (d - EPOCA).days * 600 + (int(hashlib.sha256(cuenta["cuenta_id"].encode())
                                                       .hexdigest()[:4], 16) % 400)
    n = [base]

    def siguiente():
        n[0] += 1
        return n[0]
    return siguiente


def _par_o(numero, d, hoja_lado, importe, ref, coment) -> list[Asiento]:
    """El alta en O y, al confirmar, su contrapartida en O (se cancelan)."""
    if hoja_lado == "debe":
        return [Asiento("O", d, numero(), ref, coment, debe=importe),
                Asiento("O", d, numero(), ref, f"Confirmación {coment}", haber=importe)]
    return [Asiento("O", d, numero(), ref, coment, haber=importe),
            Asiento("O", d, numero(), ref, f"Confirmación {coment}", debe=importe)]


def _registrar(r, numero, d, lado, importe, ref, coment, estado) -> list[Asiento]:
    """Asientos del FBS de una operación según su estado:
    confirmado (E + par en O), pendiente (sólo el alta en O) o sin_registrar."""
    if estado == "sin_registrar":
        return []
    if estado == "pendiente":
        if lado == "debe":
            return [Asiento("O", d, numero(), ref, coment, debe=importe)]
        return [Asiento("O", d, numero(), ref, coment, haber=importe)]
    e = (Asiento("E", d, numero(), ref, coment, debe=importe) if lado == "debe"
         else Asiento("E", d, numero(), ref, coment, haber=importe))
    return _par_o(numero, d, lado, importe, ref, coment) + [e]


def _estado(r: random.Random) -> str:
    x = r.random()
    if x < 0.80:
        return "confirmado"
    if x < 0.91:
        return "pendiente"
    if x < 0.96:
        return "sin_registrar"
    return "aproximado"


MESES = ("ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC")


def operaciones(cuenta: dict, d: date) -> list[Operacion]:
    """Las operaciones que nacen el día hábil d en la cuenta (su movimiento de
    banco puede caer ese día o después; sus asientos, ese día). El último día
    hábil del mes suma la nota de débito con los gastos del banco del mes."""
    ops = _operaciones_del_dia(cuenta, d)
    if ops and _ultimo_habil_del_mes(d):
        total = 0.0
        x = d.replace(day=1)
        while x <= d:
            dia = ops if x == d else _operaciones_del_dia(cuenta, x)
            total += sum(-o.mov.importe for o in dia if o.gasto and o.mov)
            x += timedelta(days=1)
        if total:
            numero = _numerador(cuenta, d)
            for _ in range(len(ops) * 4 + 50):   # después de los asientos del día
                numero()
            ops.append(Operacion(None, [Asiento(
                "E", d, numero(), "PO 00810", f"GASTOS BANCARIOS {MESES[d.month - 1]}/{d.strftime('%y')}",
                haber=round(total, 2))]))
    return ops


def _ultimo_habil_del_mes(d: date) -> bool:
    x = d + timedelta(days=1)
    while x.month == d.month:
        if _habil(x):
            return False
        x += timedelta(days=1)
    return _habil(d)


def _operaciones_del_dia(cuenta: dict, d: date) -> list[Operacion]:
    if not _habil(d):
        return []
    r = _rng("ops", cuenta["cuenta_id"], d.isoformat())
    usd = (cuenta.get("moneda") or "ARS").upper() == "USD"
    k = _escala(cuenta)
    numero = _numerador(cuenta, d)
    ops: list[Operacion] = []
    orden = [0]

    def mov(fecha, desc, det, comp, importe):
        orden[0] += 1
        return Mov(fecha, desc, det, comp, round(importe, 2), orden[0])

    marca = cuenta.get("empresa") or ""
    if usd:
        # cuenta en dólares: pocas operaciones, importes en U$S
        for _ in range(r.choice((0, 1, 1, 2))):
            quien, cuit = _persona(r)
            imp = _redondo(r, 4_000, 38_000, 100) * k
            imp = float(round(imp, 0)) if r.random() < 0.7 else round(imp, 2)
            trf = r.randint(10_000_000, 99_999_999)
            ref = f"RC 0002-{numero() % 10**8:08d}"
            est = _estado(r)
            ops.append(Operacion(
                mov(d, "TRANSFERENCIA RECIBIDA", f"{trf} {quien} CUIT {cuit}", str(trf), imp),
                _registrar(r, numero, d, "debe", imp, ref,
                           f"Cobro en dólares {quien} - Transf. {trf}", "confirmado" if est == "aproximado" else est)))
        if r.random() < 0.25:
            prov = r.choice(("IMPORTADORA DE REPUESTOS SA", "FLETES INTERNACIONALES SRL"))
            imp = round(r.uniform(1_500, 9_000) * k, 2)
            ref = f"OP 0002-{numero() % 10**8:08d}"
            ops.append(Operacion(
                mov(d, "TRANSFERENCIA ENVIADA", f"{prov} CUIT {_cuit_proveedor(prov)}", "", -imp),
                _registrar(r, numero, d, "haber", imp, ref,
                           f"Pago {prov} CUIT {_cuit_proveedor(prov)}", "confirmado")))
        if _dia_habil_del_mes(d) == 1:
            # primer día hábil del mes: mantenimiento de la cuenta en dólares
            ops.append(Operacion(mov(d, "COMISION MANTENIMIENTO CTA U$S", "", "", -25.0), gasto=True))
            ops.append(Operacion(mov(d, "IVA 21%", "", "", -5.25), gasto=True))
        return ops

    principal = cuenta.get("rol") == "principal"
    creditos = debitos = 0.0
    transf_enviadas = 0

    def cobro_estados(n):
        """La historia del día: en la principal siempre hay un cobro todavía en O
        (pendiente de confirmar) y uno con diferencia chica de importe, y uno de
        cada tres días alguno que nadie registró todavía."""
        if not principal:
            return ["pendiente" if r.random() < 0.1 else "confirmado" for _ in range(n)]
        est = ["confirmado"] * n
        elegidos = r.sample(range(n), 3)
        est[elegidos[0]], est[elegidos[1]] = "pendiente", "aproximado"
        if r.random() < 0.35:
            est[elegidos[2]] = "sin_registrar"
        return est

    # cobros de clientes por transferencia (señas y saldos de unidades)
    n = r.randint(6, 10) if principal else r.randint(2, 5)
    for est in cobro_estados(n):
        quien, cuit = _persona(r)
        sena = r.random() < 0.45
        imp = _redondo(r, 800_000, 3_500_000) if sena else _redondo(r, 6_000_000, 32_000_000, 10_000)
        imp = round(imp * (0.8 + 0.4 * r.random()), 2) if r.random() < 0.25 else imp
        trf = r.randint(10_000_000, 99_999_999)
        pv = r.randint(3_000, 9_800)
        ref = f"RC 0001-{numero() % 10**8:08d}"
        concepto = "Seña" if sena else "Saldo de unidad"
        coment = f"{concepto} PV {pv} {quien}" + (f" - Transf. {trf}" if r.random() < 0.65 else "")
        banco_imp = imp
        if est == "aproximado":
            # el cliente descontó el costo de la transferencia
            banco_imp = round(imp - r.choice((120.0, 250.0, 380.5, 600.0)), 2)
            est = "confirmado"
        ops.append(Operacion(
            mov(d, "TRANSFERENCIA RECIBIDA", f"{trf} {quien} CUIT {cuit}", str(trf), banco_imp),
            _registrar(r, numero, d, "debe", imp, ref, coment, est)))
        creditos += banco_imp

    # depósito en efectivo de la caja: acredita en el día
    if r.random() < (0.6 if principal else 0.2):
        boleta = r.randint(1_000_000, 9_999_999)
        imp = _redondo(r, 400_000, 6_000_000)
        ops.append(Operacion(
            mov(d, "DEPOSITO EN EFECTIVO", f"BOLETA {boleta}", str(boleta), imp),
            _registrar(r, numero, d, "debe", imp, f"DEP {boleta % 100000:05d}",
                       f"Depósito efectivo caja boleta {boleta}", "confirmado")))
        creditos += imp

    # depósito de cheques de terceros: el asiento es de hoy, el banco acredita a las 48 hs
    if principal and r.random() < 0.3:
        boleta = r.randint(1_000_000, 9_999_999)
        imp = round(sum(_redondo(r, 300_000, 4_500_000) for _ in range(r.randint(1, 4))), 2)
        ops.append(Operacion(
            mov(_mas_habiles(d, 2), "DEPOSITO DE CHEQUES 48 HS", f"BOLETA {boleta}", str(boleta), imp),
            _registrar(r, numero, d, "debe", imp, f"DEP {boleta % 100000:05d}",
                       f"Depósito cheques de terceros boleta {boleta}", "confirmado")))
        creditos += imp

    # liquidación de tarjetas (neto de aranceles)
    if r.random() < (0.7 if principal else 0.15):
        tarjeta = r.choice(("VISA", "MASTERCARD", "CABAL"))
        lote = r.randint(100_000, 999_999)
        imp = round(r.uniform(250_000, 2_800_000) * k, 2)
        ops.append(Operacion(
            mov(d, f"ACREDITACION LIQUIDACION {tarjeta}", f"LOTE {lote}", str(lote), imp),
            _registrar(r, numero, d, "debe", imp, f"LIQ {lote}",
                       f"Liquidación cupones {tarjeta} lote {lote}", "confirmado")))
        creditos += imp

    # pagos a proveedores por transferencia
    n = r.randint(4, 8) if principal else r.randint(1, 3)
    pendiente = r.randrange(n) if principal and r.random() < 0.5 else -1
    for i in range(n):
        prov = r.choice(PROVEEDORES)
        cuit = _cuit_proveedor(prov)
        imp = round(_redondo(r, 60_000, 3_800_000, 100) * (1 if r.random() < 0.6 else 1.0137), 2)
        ref = f"OP 0001-{numero() % 10**8:08d}"
        ops.append(Operacion(
            mov(d, "TRANSFERENCIA ENVIADA", f"{prov} CUIT {cuit}", "", -imp),
            _registrar(r, numero, d, "haber", imp, ref, f"Pago {prov} CUIT {cuit}",
                       "pendiente" if i == pendiente else "confirmado")))
        debitos += imp
        transf_enviadas += 1

    # pago a la terminal por unidades 0 km
    if principal and r.random() < 0.3:
        term = terminal_de(marca)
        imp = _redondo(r, 25_000_000, 95_000_000, 10_000)
        ref = f"OP 0001-{numero() % 10**8:08d}"
        ops.append(Operacion(
            mov(d, "TRANSFERENCIA ENVIADA", f"{term} CUIT {_cuit_proveedor(term)}", "", -imp),
            _registrar(r, numero, d, "haber", imp, ref, f"Pago unidades 0 km {term}", "confirmado")))
        debitos += imp
        transf_enviadas += 1

    # cheques de pago diferido: el asiento contra el banco es del día del débito
    for _ in range(r.choice((0, 1, 1, 2)) if principal else r.choice((0, 0, 1))):
        prov = r.choice(PROVEEDORES)
        cheque = r.randint(10_000_000, 99_999_999)
        imp = _redondo(r, 150_000, 2_600_000, 100)
        ref = f"CPD {cheque % 100000:05d}"
        ops.append(Operacion(
            mov(d, "CHEQUE PAGADO POR CAMARA", f"NRO {cheque}", str(cheque), -imp),
            _registrar(r, numero, d, "haber", imp, ref, f"Débito cheque diferido Ch. {cheque} {prov}",
                       "confirmado")))
        debitos += imp

    # un cheque común emitido hoy: el asiento es de hoy, lo cobran en unos días
    if principal and r.random() < 0.35:
        prov = r.choice(PROVEEDORES)
        cheque = r.randint(10_000_000, 99_999_999)
        imp = _redondo(r, 150_000, 1_800_000, 100)
        ref = f"OP 0001-{numero() % 10**8:08d}"
        ops.append(Operacion(
            mov(_mas_habiles(d, r.randint(3, 6)), "CHEQUE PAGADO POR CAMARA", f"NRO {cheque}",
                str(cheque), -imp),
            _registrar(r, numero, d, "haber", imp, ref, f"Pago {prov} Ch. {cheque}", "confirmado")))

    # VEP de AFIP (los días de vencimiento)
    if principal and 8 <= d.day <= 22 and r.random() < 0.25:
        vep = r.randint(10**9, 10**10 - 1)
        imp = _redondo(r, 1_200_000, 9_500_000, 1)
        concepto = r.choice(("IVA DDJJ", "F.931 SUSS", "GANANCIAS ANTICIPO", "INGRESOS BRUTOS CM"))
        ref = f"OP 0001-{numero() % 10**8:08d}"
        ops.append(Operacion(
            mov(d, "PAGO VEP AFIP", f"VEP {vep} {concepto}", str(vep), -imp),
            _registrar(r, numero, d, "haber", imp, ref, f"Pago VEP {vep} {concepto}", "confirmado")))
        debitos += imp

    # haberes: el cuarto día hábil del mes; el asiento de sueldos es de fin de mes
    if principal and _dia_habil_del_mes(d) == 4:
        imp = _redondo(r, 18_000_000, 46_000_000, 1)
        ops.append(Operacion(mov(d, "PAGO DE HABERES", f"ACREDITACION SUELDOS {d.strftime('%m/%Y')}",
                                 "", -imp)))
        debitos += imp

    # un pago que se anuló: asiento y contraasiento en E, sin movimiento en el banco
    if principal and r.random() < 0.12:
        prov = r.choice(PROVEEDORES)
        imp = _redondo(r, 90_000, 900_000, 100)
        ref = f"OP 0001-{numero() % 10**8:08d}"
        ops.append(Operacion(None, [
            Asiento("E", d, numero(), ref, f"Pago {prov}", haber=imp),
            Asiento("E", d, numero(), ref, f"Anulación OP {prov}", debe=imp)]))

    # gastos del banco: no se contabilizan día por día (van a la nota de débito mensual)
    if principal and transf_enviadas:
        com = 950.0 * transf_enviadas
        ops.append(Operacion(mov(d, "COMISION TRANSFERENCIAS", f"{transf_enviadas} OPERACIONES", "", -com), gasto=True))
        ops.append(Operacion(mov(d, "IVA 21%", "S/COMISIONES", "", -round(com * 0.21, 2)), gasto=True))
        if r.random() < 0.4:
            ops.append(Operacion(mov(d, "PERCEPCION IVA RG 2408", "", "", -round(com * 0.03, 2)), gasto=True))
    if creditos:
        ops.append(Operacion(mov(d, "IMP. LEY 25.413 S/CREDITOS", "", "", -round(creditos * 0.006, 2)),
                                 gasto=True))
        if principal and r.random() < 0.5:
            ops.append(Operacion(mov(d, "SIRCREB", "REG. RECAUDACION IIBB", "",
                                     -round(creditos * 0.009, 2)), gasto=True))
    if debitos:
        ops.append(Operacion(mov(d, "IMP. LEY 25.413 S/DEBITOS", "", "", -round(debitos * 0.006, 2)),
                                 gasto=True))
    return ops


def _dia_habil_del_mes(d: date) -> int:
    n = 0
    x = d.replace(day=1)
    while x <= d:
        if _habil(x):
            n += 1
        x += timedelta(days=1)
    return n


# -------------------------------------------------------- lo que se consulta

def movimientos(cuenta: dict, desde: date, hasta: date) -> list[Mov]:
    """Los movimientos del banco con fecha en [desde, hasta] (nunca futuros)."""
    hasta = min(hasta, date.today())
    out = []
    for d in _habiles(desde - timedelta(days=VENTANA), hasta):
        for op in operaciones(cuenta, d):
            if op.mov and desde <= op.mov.fecha <= hasta:
                out.append(op.mov)
    return sorted(out, key=lambda m: (m.fecha, m.orden))


def asientos(cuenta: dict, desde: date, hasta: date) -> list[Asiento]:
    """Los asientos del mayor (E y O) con fecha en [desde, hasta]."""
    hasta = min(hasta, date.today())
    out = []
    for d in _habiles(desde, hasta):
        for op in operaciones(cuenta, d):
            out.extend(a for a in op.asientos if desde <= a.fecha <= hasta)
    return sorted(out, key=lambda a: (a.fecha, a.numero))


def filas_detmov(cuenta: dict, desde: date, hasta: date) -> list[dict]:
    """Lo que devolvería la query generada sobre DetMov (ver fbs_sql._query_detmov)
    para las cuentas E y O de esta cuenta bancaria."""
    filas = []
    nombre = f"{cuenta.get('banco_nombre') or cuenta.get('banco', '').upper()} CC " \
             f"{'U$S' if (cuenta.get('moneda') or '').upper() == 'USD' else '$'} {cuenta.get('numero', '')}"
    for a in asientos(cuenta, desde, hasta):
        plan = cuenta.get("plan_e") if a.hoja == "E" else cuenta.get("plan_o")
        if not plan:
            continue
        filas.append({"hoja": a.hoja, "codigo": str(plan), "DetMovNro": a.numero,
                      "DetFecha": datetime(a.fecha.year, a.fecha.month, a.fecha.day),
                      "DetRef": a.referencia, "DetComenta": a.comentario,
                      "DetDebe": a.debe, "DetHaber": a.haber, "nombre_fbs": nombre})
    return filas


def _saldo_inicial(cuenta: dict, d: date) -> float:
    r = _rng("saldo", cuenta["cuenta_id"], d.isoformat())
    usd = (cuenta.get("moneda") or "").upper() == "USD"
    base = (180_000 if usd else 145_000_000) * _escala(cuenta)
    return round(base * r.uniform(0.85, 1.15), 2)


# --------------------------------------------------------------- extractos
# Cada banco en el formato que exporta su home banking (el que reconoce
# parsers/diarios.py), con el número de cuenta adentro para que el conciliador
# la identifique sola.

FORMATOS = {"santander": "Santander/Río (.xls de texto)", "frances": "BBVA/Francés (.xls)",
            "macro": "Macro (.xls)", "ciudad": "Ciudad (.csv)", "galicia": "Galicia (.xlsx)"}


def _con_saldos(cuenta, movs):
    """[(mov, saldo después del movimiento)] con saldo corrido desde el inicial del día."""
    out, saldo, dia = [], 0.0, None
    for m in movs:
        if m.fecha != dia:
            dia = m.fecha
            saldo = _saldo_inicial(cuenta, dia)
        saldo = round(saldo + m.importe, 2)
        out.append((m, saldo))
    return out


def _santander(cuenta, movs, desde, hasta) -> bytes:
    moneda = "Dólares" if (cuenta.get("moneda") or "").upper() == "USD" else "Pesos"
    lineas = ["", "Movimientos del Día" if desde == hasta else "Últimos Movimientos", "",
              f"Cuenta Corriente en {moneda} Nro. {cuenta['numero']}",
              f"Período: {desde.strftime('%d/%m/%Y')} al {hasta.strftime('%d/%m/%Y')}", "",
              "\t".join(("Fecha", "Sucursal Origen", "Desc. Sucursal", "Cod. Operativo", "Referencia",
                         "Concepto", f"Importe {moneda}", f"Saldo {moneda}"))]
    for i, (m, saldo) in enumerate(_con_saldos(cuenta, movs)):
        concepto = m.descripcion + (f"  - {m.detalle}" if m.detalle else "")
        imp = ("-" if m.importe < 0 else "") + _ar(m.importe)
        lineas.append("\t".join((m.fecha.strftime("%d/%m/%Y"), "0742", "CASA CENTRAL",
                                 str(4000 + (i * 37) % 900), m.comprobante or "",
                                 concepto, imp, _ar(saldo))))
    return ("\r\n".join(lineas) + "\r\n").encode("latin-1", errors="replace")


def _ciudad(cuenta, movs, desde, hasta) -> bytes:
    simbolo = "U$S" if (cuenta.get("moneda") or "").upper() == "USD" else "$"
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(("Cuenta", "Sucursal", "Fecha", "Importe", "Comprobante", "Descripcion", "Saldo"))
    for m, saldo in _con_saldos(cuenta, movs):
        desc = m.descripcion + (f" {m.detalle}" if m.detalle else "")
        w.writerow((f"{cuenta['numero']} {simbolo}", "0001", m.fecha.strftime("%d/%m/%Y"),
                    ("-" if m.importe < 0 else "") + _ar(m.importe), m.comprobante or "", desc, _ar(saldo)))
    return buf.getvalue().encode("latin-1", errors="replace")


def _xlwt():
    try:
        import xlwt
    except ImportError:
        raise RuntimeError("Para generar el extracto de este banco falta la librería xlwt "
                           "(pip install xlwt).") from None
    return xlwt


def _bbva(cuenta, movs, desde, hasta) -> bytes:
    xlwt = _xlwt()
    wb = xlwt.Workbook(encoding="utf-8")
    ws = wb.add_sheet("Movimientos del Día" if desde == hasta else "Movimientos Históricos")
    simbolo = "U$S" if (cuenta.get("moneda") or "").upper() == "USD" else "$"
    ws.write(0, 0, "BBVA")
    ws.write(0, 1, "Consulta de movimientos")
    ws.write(1, 0, "Empresa:")
    ws.write(1, 1, (cuenta.get("empresa") or "").upper())
    ws.write(2, 0, "Cuenta:")
    ws.write(2, 1, f"{cuenta['numero']} (CC {simbolo})")
    ws.write(3, 0, "Período:")
    ws.write(3, 1, f"{desde.strftime('%d/%m/%Y')} al {hasta.strftime('%d/%m/%Y')}")
    cab = ("Fecha", "Fecha Valor", "Concepto", "Código", "Número Documento", "Oficina", "Crédito", "Débito",
           "Detalle")
    for j, c in enumerate(cab):
        ws.write(5, j, c)
    for i, (m, _saldo) in enumerate(_con_saldos(cuenta, movs), start=6):
        f = m.fecha.strftime("%d/%m/%Y")
        ws.write(i, 0, f)
        ws.write(i, 1, f)
        ws.write(i, 2, m.descripcion)
        ws.write(i, 3, str(100 + (i * 13) % 800))
        ws.write(i, 4, m.comprobante or "")
        ws.write(i, 5, "0109")
        ws.write(i, 6, m.importe if m.importe > 0 else "")
        ws.write(i, 7, -m.importe if m.importe < 0 else "")
        ws.write(i, 8, m.detalle or "")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _macro(cuenta, movs, desde, hasta) -> bytes:
    xlwt = _xlwt()
    wb = xlwt.Workbook(encoding="utf-8")
    ws = wb.add_sheet("Movimientos")
    fecha_estilo = xlwt.easyxf(num_format_str="DD/MM/YYYY")
    usd = (cuenta.get("moneda") or "").upper() == "USD"
    ws.write(0, 0, "CUENTA CORRIENTE BANCARIA")
    ws.write(1, 0, "Número")
    ws.write(1, 1, cuenta["numero"])
    ws.write(2, 0, "Moneda")
    ws.write(2, 1, "DOLAR ESTADOUNIDENSE" if usd else "PESOS")
    ws.write(3, 0, "Titular")
    ws.write(3, 1, (cuenta.get("empresa") or "").upper())
    cab = ("Fecha", "Nro. de Referencia", "Causal", "Concepto", "Importe", "Saldo")
    for j, c in enumerate(cab):
        ws.write(5, j, c)
    for i, (m, saldo) in enumerate(_con_saldos(cuenta, movs), start=6):
        ws.write(i, 0, datetime(m.fecha.year, m.fecha.month, m.fecha.day), fecha_estilo)
        ws.write(i, 1, m.comprobante or "")
        ws.write(i, 2, m.detalle or "")
        ws.write(i, 3, m.descripcion)
        ws.write(i, 4, m.importe)
        ws.write(i, 5, saldo)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _galicia(cuenta, movs, desde, hasta) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Movimientos"
    ws.append(["Fecha", "Descripción", "Origen", "Débitos", "Créditos", "Grupo de Conceptos", "Concepto",
               "Número de Terminal", "Observaciones Cliente", "Número de Comprobante",
               "Leyendas Adicionales 1", "Leyendas Adicionales 2", "Leyendas Adicionales 3",
               "Leyendas Adicionales 4", "Tipo de Movimiento", "Saldo"])
    for m, saldo in _con_saldos(cuenta, movs):
        ws.append([datetime(m.fecha.year, m.fecha.month, m.fecha.day), m.descripcion, "",
                   -m.importe if m.importe < 0 else 0, m.importe if m.importe > 0 else 0, "", "", "", "",
                   m.comprobante or "", m.detalle or "", "", "", "", "", saldo])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_ESCRITORES = {"santander": (_santander, "xls"), "frances": (_bbva, "xls"), "macro": (_macro, "xls"),
               "ciudad": (_ciudad, "csv"), "galicia": (_galicia, "xlsx")}


def nombre_archivo(cuenta: dict, desde: date, hasta: date) -> str:
    _, ext = _ESCRITORES.get(cuenta.get("banco"), (_ciudad, "csv"))
    num = re.sub(r"[^0-9A-Za-z]+", "-", cuenta.get("numero") or "").strip("-")
    marca = re.sub(r"[^0-9A-Za-z]+", "-", _plano(cuenta.get("empresa") or "")).strip("-")
    rango = desde.isoformat() if desde == hasta else f"{desde.isoformat()}_a_{hasta.isoformat()}"
    banco = {"frances": "BBVA", "santander": "Santander", "macro": "Macro", "ciudad": "Ciudad",
             "galicia": "Galicia"}.get(cuenta.get("banco"), cuenta.get("banco", "banco"))
    return f"{banco}_{marca}_{num}_{(cuenta.get('moneda') or '').upper()}_{rango}.{ext}"


def extracto(cuenta: dict, desde: date, hasta: date) -> tuple[str, bytes, int]:
    """(nombre de archivo, contenido, cantidad de movimientos) del extracto."""
    escritor, _ = _ESCRITORES.get(cuenta.get("banco"), (_ciudad, "csv"))
    movs = movimientos(cuenta, desde, hasta)
    return nombre_archivo(cuenta, desde, hasta), escritor(cuenta, movs, desde, hasta), len(movs)


def zip_extractos(cuentas: list[dict], desde: date, hasta: date) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for c in cuentas:
            nombre, contenido, n = extracto(c, desde, hasta)
            if n:
                z.writestr(nombre, contenido)
    return buf.getvalue()
