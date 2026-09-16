# ADR-0004: Dejar la IA como ayuda opcional, con tope de costo y respaldo determinístico

Fecha: 2026-09-01 · Estado: aceptado · Reconstruido el 2026-09-16 a partir de los commits `5d6944c` y `291e989`

## Contexto

El conciliador usa Claude en dos lugares: sugerir cruces que el motor determinístico no encontró, y explicar asiento por asiento lo que quedó sin conciliar. El segundo uso escala con el tamaño de la corrida, así que una conciliación grande podía disparar cientos de llamadas. El comentario del módulo de sugerencias registra el cambio de modelo que precedió a esta decisión: de Opus a Sonnet 5, "~60% más barato que Opus", con un costo por tanda de entre cinco y diez centavos de dólar [`engine/ai_assist.py:12-14`].

## Decisión

La IA nunca define el resultado. La conciliación determinística se calcula siempre y primero; las sugerencias se agregan después y las confirma una persona [`app.py:279-287`].

Para el análisis por asiento se fijó una cascada de cuatro escalones: caché persistente en disco, presupuesto diario configurable por `ANALISIS_MAX_DIA` con un valor por defecto de 150, llamada a `claude-haiku-4-5` con un tope de 400 tokens de salida, y análisis interno determinístico cuando cualquier escalón se agota o la llamada falla [`engine/analisis.py:25-27,189-269`].

Al día siguiente se sumó la auditoría: un log de las últimas dos mil llamadas con tokens y dólares, con el motivo escrito en el commit, "si la factura sube y este log no lo refleja, el consumo vino de otro lado" [`291e989`; `engine/ia_log.py:13`].

## Consecuencias

Sin credencial de Anthropic el sistema concilia igual y la IA queda en `sin_credenciales` [`app.py:281-283`]. Eso permitió el modo demostración y las pruebas sin gasto.

El costo del diseño es que la calidad del resultado depende del motor determinístico, y ese motor no tiene un solo test. También quedó un modo de simulación que genera sugerencias falsas sin llamar a la API, que hubo que blindar para que nadie lo active sin querer [`8d45d79`].
