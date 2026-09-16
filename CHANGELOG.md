# Changelog

Una sección por mes. Cada línea dice qué cambió para quien usa el sistema, con el hash o el ticket que lo respalda.

## [Sin publicar]

Todo lo de la rama `origin/fbs-sql`: 31 commits desde el 2026-09-07 que no están en `master` [`git log master..origin/fbs-sql`].

### Agregado
- El libro mayor se trae directo del FBS por consulta a SQL Server, en vez de subir el Excel exportado a mano [`76f791e`].
- Modo hub: la conexión al FBS y el par de cuentas contables de cada cuenta bancaria se configuran en el hub, y la consulta se genera sola [`bbc35bc`].
- Arranque accesible desde la red local del cliente y arranque en el puerto 8766 para la instalación on-premise, este último marcado como temporal por su propio autor [`83d1efd`, `b559d13`].
- Descarga en Excel de lo que se trajo del FBS, en el mismo formato "Consulta del Mayor" de siempre [`f634c85`].
- Copia de equivalencias, gastos, reglas y mapeos desde Railway a la instalación local [`3e29338`].

### Cambiado
- En el modo diario ya no se sube el mayor: se trae de la base [`feb3990`].
- El conciliador mensual acepta una cuenta del hub y un rango de fechas en lugar del Excel [`15d6785`].

### Seguridad
- Protección de solo lectura sobre el SQL Server del FBS, en tres capas: validación de la consulta antes de ejecutarla, transacción sin autocommit con rollback explícito, y permisos del login del lado del servidor [`1e4a143`].

## 2026-09

### Agregado
- Modo demostración completo: kit de extractos descargable, mayor sembrado que se inyecta solo y botón para reiniciar [`932847a`; `[jira-ROD.md]` ROD-6, ROD-7, ROD-10].
- Las conciliaciones se pueden abrir por marca desde el hub [`2e8263a`].
- Las cuentas bancarias pasan a leerse del Postgres del hub, con copia local de respaldo si la base no responde [`eb19591`].
- Solapa propia para los haberes del extracto sin contabilizar, con su hoja en el Excel: es el listado con el que se arma el asiento de sueldos en el FBS [`57baff0`].
- Un cruce automático equivocado se puede anular, y queda vetado para siempre [`76ec38f`].
- Excepciones de concepto, para marcar que algo nunca es un gasto bancario sin necesidad de un programador [`f7a0954`; `[jira-OR.md]` OR-27].
- Auditoría del gasto de IA: tokens y dólares por llamada [`291e989`].
- Exportar e importar todos los datos del servicio, para mudarlo a otro [`2c514ae`].
- El extracto de Santander acepta el export "descargaUltimosMovimientos" [`80a1bb8`].
- Las dos listas de la conciliación manual se ordenan alfabéticamente [`766d9bc`].

### Cambiado
- El análisis con IA por asiento pasa a un modelo más barato, con caché, tope diario y análisis interno cuando el tope se agota [`5d6944c`].
- La confirmación entre la cuenta O y la E pasa a ser estricta: misma referencia, mismo importe y mismo lado [`f6a9a8c`].
- El material de ejemplo del modo demostración sale del repositorio y se genera en cada arranque, con fechas relativas a hoy [`53b76b5`].

### Corregido
- Los pagos a proveedores dejaban de caer como gastos bancarios [`acde316`].
- Los comprobantes alfanuméricos dejaban de romper el detector de gastos, y se suma el SIRC del BBVA [`e4b7e4d`].
- Un plan de cuentas repetido ya no duplica todos los asientos [`0874a1a`].
- Los movimientos repetidos nunca se suprimen: dos impuestos idénticos el mismo día son reales [`f4cd03b`].

## 2026-08

### Agregado
- Modo diario multibanco: un tablero por cuenta bancaria, con el período que cubre cada corrida [`efee321`, `bf2a5db`].
- Arrastre automático de los pendientes entre corridas [`9b9e76e`].
- Memoria persistente de lo conciliado, para que los archivos acumulativos no dupliquen movimientos [`92714ef`, `48d062c`].
- Análisis con IA asiento por asiento sobre lo que quedó pendiente, con una confirmación que el sistema aprende [`9b3029e`].
- Tema claro y oscuro [`867d5da`].
- Lote de pedidos del cliente: equivalencias con varios términos, contraasientos, gastos configurables, resultado compartible por link y orden en la conciliación manual [`134bb32`].

### Cambiado
- Rediseño de la interfaz con la estética del hub del grupo, sin emojis [`3e66b71`].
- Soporte de las variantes de Excel que usa el cliente en el modo diario [`5f53d8c`].

## 2026-07

### Agregado
- Primera versión: parser del extracto de Santander en PDF, parser del mayor con las hojas E y O, motor de conciliación en tres pases, reglas aprendidas, interfaz web y `Dockerfile` [`2d4f94b`].
- Confirmación de los pendientes de la cuenta O [`5acacad`].
- Diccionario de equivalencias entre el vocabulario del extracto y el del sistema [`ea03c0a`].
- Overlay de progreso con tiempo estimado y modo simulación de IA, con indicador visible [`2134774`, `68bf8f6`].
- Diagnóstico de credenciales de IA, que informa sin devolver el valor de ningún secreto [`72f404f`, `f1884c9`].
- Pase de conciliación por tolerancia y el indicador "extracto explicado" [`873e0ae`].

### Cambiado
- La interfaz se sirve sin caché, para que después de cada deploy el navegador traiga la versión nueva [`d295a49`].
- El sistema contable pasa a llamarse FBS en la documentación [`f656439`].

### Eliminado
- Los emojis de títulos y avisos [`a145743`].

### Seguridad
- Se quitó la protección por clave de acceso, a pedido, "para testing" [`6803948`]. Desde esa fecha el servicio no tiene autenticación; ver `docs/decisiones/0006-quitar-la-clave-de-acceso.md`.
- Blindaje contra activar el modo simulación sin querer [`8d45d79`].
