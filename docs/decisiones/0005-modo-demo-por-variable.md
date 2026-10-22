# ADR-0005: Implementar el modo demostración como una variable de entorno, no como fork

Fecha: 2026-09-15 · Estado: aceptado · Reconstruido el 2026-09-16 a partir del commit `932847a` y de `README-DEMO.md`

## Contexto

El equipo comercial necesitaba mostrar Orbit funcionando de punta a punta, sin credenciales del FBS y sin datos de clientes reales [`[jira-ROD.md]` ROD-1]. Las opciones eran una rama de demostración, un repositorio aparte con datos falsos, o el mismo código con un interruptor.

Una rama o un fork significan mantener dos versiones y que la demostración muestre algo que ya no es el producto.

## Decisión

Mismo código, mismo servicio, una variable. `DEMO_MODE` acepta `1`, `true`, `on` o `si`, y el ambiente de demostración es un segundo servicio de Railway apuntado a la misma rama, con esa variable como única diferencia [`app.py:51`; `README-DEMO.md:1-5`]. Fuera de esa variable, ni las rutas ni la interfaz de demostración existen.

Con la variable activa aparecen tres cosas: la inyección automática del mayor sembrado cuando se arrastra un extracto del kit, el kit descargable con su resultado esperado, y `POST /api/demo/reset`, que limpia el staging, el arrastre, la memoria y las corridas sin tocar los fixtures [`app.py:526-591,1725-1773`]. Un middleware agrega `X-Robots-Tag: noindex, nofollow` a todas las respuestas.

El kit se regenera en cada arranque con fechas relativas a hoy, "así la demo nunca se ve vieja", y por eso no se versiona [`scripts/generar_kit_demo.py:190-223`; `app.py:1774-1781`].

## Consecuencias

La demostración siempre corre la versión de hoy y el código de producción no se ramifica.

El precio es que el código de demostración vive dentro del de producción, con condicionales que hay que respetar en cada cambio. Ya hubo un arreglo por material generado que volvió a entrar al repositorio "por la prueba de arranque" [`8b3c5c1`].

Queda una dependencia sin verificar: las cuentas del kit tienen que coincidir exactamente con las que siembra el hub de demostración, y eso se sostiene a mano [`README-DEMO.md:52-55`].
