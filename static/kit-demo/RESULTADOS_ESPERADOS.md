# Resultados esperados del kit de demo
Generado el 2026-09-15 (fechas relativas a hoy; volvé a correr `python scripts/generar_kit_demo.py` si se ve vieja).

## rumbo-motors-cuenta-corriente.csv — Demo · Rumbo Motors
Cuenta: ciudad 350123456789 (ARS) · id `ciudad-350123456789-ars` · 6 movimientos en el extracto
Mismo resultado sirve para el **modo diario** (`/diario`, solo hace falta este CSV — el mayor se agrega solo) y para el **conciliador mensual** (`/`, subiendo este CSV como extracto **y** `rumbo-motors-cuenta-corriente-mayor.xlsx` como libro mayor).
- **4 de 6 conciliados automáticamente**: los 2 pagos limpios (transferencia recibida y pago a Repuestos SA), la cobranza con 32 centavos de diferencia (método "importe aproximado", queda marcada igual) y uno de los dos pagos duplicados a Insumos XYZ (mismo importe y fecha exacta: el sistema resuelve uno solo).
- **Quedan 2 sin explicar del lado banco** ($57.500 total): el segundo pago duplicado a Insumos XYZ ($45.000, para mostrar cómo se marca/investiga un duplicado a mano) y el interés a favor ($12.500, sin asiento — no tiene contrapartida en el mayor).
- **Queda 1 sin explicar del lado mayor** ($67.000): el cheque Nro 00456123, cargado en el mayor pero nunca acreditado en el banco (rechazado).

## andina-repuestos-cuenta-corriente.csv — Demo · Andina Repuestos
Cuenta: ciudad 350234567891 (ARS) · id `ciudad-350234567891-ars` · 4 movimientos en el extracto
Mismo resultado sirve para el **modo diario** (`/diario`, solo hace falta este CSV — el mayor se agrega solo) y para el **conciliador mensual** (`/`, subiendo este CSV como extracto **y** `andina-repuestos-cuenta-corriente-mayor.xlsx` como libro mayor).
- **4 de 4 conciliados automáticamente (100%)** — este archivo es el caso prolijo: todo cruza solo, sin nada para resolver a mano.
