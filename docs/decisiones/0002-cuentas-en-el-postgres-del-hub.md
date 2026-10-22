# ADR-0002: Centralizar las cuentas bancarias en el Postgres del hub, con copia local de respaldo

Fecha: 2026-09-01 · Estado: aceptado · Reconstruido el 2026-09-16 a partir del commit `eb19591`

## Contexto

Las cuentas bancarias del grupo nacieron escritas en el código: treinta filas de empresa, banco, número y moneda, con el comentario "tabla pasada por el cliente" [`engine/cuentas.py:27-60`]. Cada alta de cuenta era un deploy. En paralelo el hub empezó a administrar marcas y cuentas para todas las aplicaciones de Orbit, así que el mismo dato pasó a existir en dos lados.

## Decisión

El Postgres del hub es la fuente de verdad del catálogo. El conciliador hace un único `SELECT` sobre `cuentas_bancarias` unida a `marcas`, filtrando las activas, con un `connect_timeout` de cinco segundos [`engine/cuentas.py:111-114`]. El motivo quedó escrito en el encabezado del módulo: "lo que el admin cambia ahí se refleja acá sin redeploy".

Si no hay `DATABASE_URL` o la base no responde, el servicio imprime el motivo y sigue con la copia local, y si tampoco la hay, con la semilla histórica [`engine/cuentas.py:94-129`]. El resultado se cachea sesenta segundos, con el comentario "también evita martillar la base si está caída" [`engine/cuentas.py:26`].

Los mapeos contra el FBS quedan locales, porque el número interno del FBS no coincide con el número de cuenta bancaria y el vínculo se aprende una vez por código de cuenta contable [`engine/cuentas.py:9-14`].

## Consecuencias

El alta de una cuenta dejó de necesitar programador, y los tickets `OR-14`, `OR-24` y `OR-25` se cerraron el 2026-09-15 desde el hub [`[jira-OR.md]`].

El costo es que ahora hay dos orígenes del mismo dato y ninguna alerta cuando el de respaldo entra en juego: el servicio sigue andando con cuentas viejas y solo lo cuenta en el log y en `/api/diagnostico`.

El conciliador nunca escribe en el hub, así que un mapeo FBS aprendido en un servicio no viaja al otro.
