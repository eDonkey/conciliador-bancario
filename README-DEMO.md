# Ambiente de demo (conciliador-bancario)

Mismo código que el de la empresa (rama `fbs-sql`); el ambiente de demo es un
servicio de Railway con `DEMO_MODE=true` como única diferencia. Sin esa
variable, el comportamiento es exactamente el de siempre.

## Qué activa DEMO_MODE

- **FBS simulado** (`engine/simulacion.py`): no hay SQL Server. Las cuentas son
  las del hub de demo (`cuentas_bancarias` con conexión FBS e IDs E/O, las
  siembra `hub-grupo/scripts/seed-demo.js` con las marcas que se eligen al
  preparar la demo) y "Traer E y O del FBS" devuelve los asientos de la
  simulación en lugar de consultar `DetMov`. "Probar conexión" da OK.
- **Extractos generados**: `/diario` y `/` muestran el bloque "Extractos de la
  demo": se elige una cuenta (o todas las de la marca) y un período, y se
  descarga el extracto **en el formato del home banking de su banco**
  (Santander/Río `.xls` de texto, BBVA `.xls`, Macro `.xls`, Ciudad `.csv`), o
  se carga con un clic en la zona de extractos. Sale de la misma simulación
  que el mayor: lo que se sube y lo que trae el FBS cuentan la misma historia.
- `POST /api/demo/reset`: limpia la memoria de conciliados, el arrastre y el
  historial (jobs y tableros). La simulación no guarda nada.
- Header `X-Robots-Tag: noindex, nofollow` en todas las respuestas.
- Fuera de `DEMO_MODE`, ninguna de estas rutas ni UI existen.

## La simulación

Determinística: cada operación nace de una semilla (cuenta + día), así el
mismo día da siempre lo mismo y las fechas siempre son las de esta semana.
Por cuenta y día hábil:

| Caso | Banco | FBS | Dónde se ve |
|---|---|---|---|
| Cobros por transferencia (señas, saldos de unidades) | crédito con CUIT y número de transferencia | recibo en O y su confirmación en E | Conciliados (E) |
| Un cobro todavía sin confirmar | crédito | sólo el alta en O | Pendientes de confirmar (O) |
| Un cobro con diferencia chica (el cliente descontó la comisión) | crédito por un poco menos | recibo por el total | Conciliados, "importe aproximado" |
| Uno de cada tres días, un cobro que nadie registró | crédito | nada | Banco sin contabilizar |
| Depósitos en efectivo, liquidaciones de tarjetas, pagos a proveedores y a la terminal, VEPs de AFIP, cheques de pago diferido | sí | sí, el mismo día | Conciliados |
| Depósito de cheques 48 hs / cheque común emitido | acredita o debita días después | el día del depósito o la emisión | Se resuelve con el arrastre |
| Haberes (4° día hábil del mes) | débito | nada (el asiento de sueldos es de fin de mes) | Haberes sin contabilizar |
| Un pago anulado | nada | asiento y contraasiento en E | Contraasientos |
| Comisiones, IVA, Ley 25.413, SIRCREB | débitos | la nota de débito del mes, el último día hábil | Gastos bancarios (en la mensual cierran contra la ND) |

La cuenta principal de cada marca (la primera en pesos) mueve más que las
otras. Las cuentas en dólares tienen pocas operaciones.

## Recorrido sugerido

- **Diario**: elegir "Hoy" y "Todas las cuentas" → "Cargar en la zona de
  abajo" → "Identificar archivos" → "Traer E y O del FBS" → "Conciliar el día".
  Para mostrar el arrastre: primero el día hábil anterior y después hoy.
- **Mensual**: "Mes anterior" sobre la cuenta principal → "Cargar extracto y
  mayor" → "Conciliar": casi todo cruza y los gastos cierran contra la nota de
  débito.

## Crear el servicio en Railway

1. Servicio desde este repo, rama **`fbs-sql`** (la que usa la empresa). Se
   construye con el `Dockerfile`, que además instala `requirements-demo.txt`
   (`xlwt`, para los `.xls` de BBVA y Macro).
2. Variables: `DEMO_MODE=true` y `DATABASE_URL` del Postgres **de la demo** (el
   mismo del hub de demo, nunca el de producción).
3. Se abre desde el hub de demo, igual que en la empresa: el hub lo publica
   en `/app/conciliador-bancario/` con `HUB_PROXY_CONCILIADOR_BANCARIO` = la
   URL de este servicio.
