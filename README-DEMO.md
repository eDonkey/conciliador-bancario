# Ambiente de demo (conciliador-bancario)

Mismo código que producción; el ambiente de demo es un **segundo servicio de
Railway** apuntado a la misma rama, con `DEMO_MODE=true` como única
diferencia. Sin esa variable, el comportamiento es exactamente el de hoy.

## Qué activa DEMO_MODE

- `GET /api/diario/identificar?demo=1`: además de parsear el extracto que se
  dropeó, busca un fixture sembrado en `datos/demo/<cuenta_id>.json` para esa
  cuenta y lo suma a la conciliación como si también se hubiera subido el
  mayor FBS — sin mostrarlo en la lista de archivos (ROD-6 "05").
- `/diario` (modo diario) muestra el banner de demo, la sección "Kit de
  demo" con los extractos descargables de `static/kit-demo/`, y el botón
  "Reiniciar demo".
- `/` (conciliador **mensual**, la pantalla original de extractos + libro
  mayor) también muestra el banner y su propia sección "Kit de demo" — ahí
  hace falta subir los dos archivos del kit (el CSV del extracto **y** el
  `-mayor.xlsx`), como en el uso real. No tiene lógica de demo propia: es la
  misma `/api/conciliar` de siempre, solo que con archivos de ejemplo.
- `POST /api/demo/reset`: limpia el staging en memoria y el arrastre entre
  corridas (no toca los fixtures ni el kit — son siempre los mismos datos).
- Header `X-Robots-Tag: noindex, nofollow` en todas las respuestas.
- Fuera de `DEMO_MODE`, ninguna de estas rutas ni UI existen.

## Kit de extractos y mayor sembrado

Un solo generador produce todo a la vez, para que nunca quede
desincronizado:

```bash
python scripts/generar_kit_demo.py
```

Por cada cuenta escribe tres archivos que representan el mismo dato de tres
formas distintas:
- `static/kit-demo/<empresa>.csv` — el extracto descargable, formato
  "Ciudad" (el más simple de los que reconoce `parsers/diarios.py`).
- `datos/demo/<cuenta_id>.json` — el mismo mayor pero en el formato que usa
  el modo diario para auto-inyectarlo (`?demo=1`).
- `static/kit-demo/<empresa>-mayor.xlsx` — el mismo mayor pero como
  "Consulta del Mayor" real (hojas "Cuenta E"/"Cuenta O"), para subirlo a
  mano en el conciliador mensual.

Las fechas son relativas a hoy: volvé a correrlo si la demo se ve vieja (por
ejemplo, desde un cron o al arrancar el servicio — todavía no está
automatizado, es un paso manual). El resultado esperado de cada archivo del
kit está documentado y **verificado a mano** (en ambos modos) en
`static/kit-demo/RESULTADOS_ESPERADOS.md`.

**Las cuentas acá (banco/número/moneda) tienen que coincidir exactamente con
las que siembra `hub-grupo/scripts/seed-demo.js`** — son las dos mitades del
mismo dato (el `cuenta_id` se deriva de banco+número+moneda, ver
`engine/cuentas.py`). Si se edita un lado hay que editar el otro.

## Crear el servicio en Railway

1. "New Service" → deploy desde este repo, misma rama que producción.
2. Variables de entorno: `DEMO_MODE=true`, y `DATABASE_URL` apuntando al
   Postgres del hub **de demo** (no el de producción) — así `engine/cuentas.py`
   resuelve las cuentas ficticias sembradas por hub-grupo.
3. No hace falta ninguna credencial de FBS real: el modo demo no se conecta
   a FBS en ningún caso.
