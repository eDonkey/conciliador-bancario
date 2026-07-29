# Conciliador bancario

Sistema web para conciliar extractos de **Banco Santander** (PDF) contra el
libro mayor del sistema contable **FES** (Excel con las cuentas **E** y **O**).

## Cómo funciona

El sistema contable registra los movimientos así:

- **Cuenta O (operativa/transitoria):** las órdenes de pago y recibos entran acá
  por defecto. Son movimientos *pendientes de confirmación*.
- **Cuenta E:** al confirmar el ingreso/egreso, se hace la contrapartida en la O
  y el asiento pasa a la E. La cuenta E debería coincidir con el extracto.

La conciliación automática hace:

1. **Parsea los PDFs** del extracto (débito/crédito se valida con la aritmética
   de saldos, por lo que es a prueba de errores de lectura).
2. **Parsea el Excel** del mayor (hojas "… E" y "… O").
3. **Netea la cuenta O**: cancela los pares asiento + contrapartida (movimientos
   ya confirmados) y deja solo los pendientes.
4. **Concilia extracto vs cuenta E** en tres pases: importe + número de
   referencia compartido → importe único → importe + fecha más cercana.
   (Convención: crédito del banco ↔ Debe del mayor.)
5. Lo que no está en E lo **busca en la O pendiente** → esos movimientos están
   en el banco pero falta confirmarlos en el FES.
6. Clasifica el resto: **gastos/impuestos bancarios** (comisiones, IVA, SIRCREB,
   ley 25.413…) vs **movimientos sin contabilizar**.
   Los gastos además se **agrupan por período en la nota de débito mensual**,
   replicando el criterio contable: comisiones gravadas al 21% con su IVA,
   intereses sobre saldo deudor al 10,5% con su IVA, y los conceptos no
   gravados (ley 25.413, SIRCREB, percepciones IIBB). El sistema detecta el
   asiento "GASTOS BANCARIOS" del mayor de cada mes (y las NC de impuestos),
   compara los totales y muestra la diferencia, con control de que el IVA
   cobrado coincida con el calculado sobre la base gravada.
7. **(Opcional) IA:** los casos que quedan sin resolver se mandan a Claude, que
   sugiere emparejamientos difíciles (combinaciones N-a-1, diferencias por
   comisión, coincidencia de beneficiario/CUIT) con nivel de confianza y motivo.
   Cada sugerencia se puede **aceptar con un clic**.
8. **Conciliación manual:** la pestaña "✋ Conciliar manualmente" muestra los
   residuales del banco y del mayor en dos paneles con filtros. Seleccionás
   ítems de cada lado (soporta N contra M), ves la suma y la diferencia en
   vivo, y conciliás el grupo. Al seleccionar un solo ítem, el otro panel se
   reordena por cercanía de importe para encontrar la contrapartida rápido.
   Todo se puede deshacer, queda guardado en disco y sale en el Excel.
9. **Aprendizaje:** cada conciliación manual (o sugerencia de IA aceptada)
   genera una regla en `datos/reglas_aprendidas.json` — la "firma" del concepto
   bancario y la del asiento. En las próximas conciliaciones, los pares que
   cumplan una regla (con importes que cierren, incluso grupos por día que
   suman igual) se concilian solos con método "regla aprendida". Deshacer un
   match debilita/elimina la regla.

## Uso

En Windows:

```bat
iniciar.bat
```

Manual (cualquier sistema con Python 3.11+):

```bash
pip install -r requirements.txt
python app.py
```

Con Docker:

```bash
docker build -t conciliador .
docker run -p 8765:8765 conciliador
```

Abre http://localhost:8765 — arrastrá los PDFs del extracto y el Excel del
mayor, y apretá **Conciliar**. Al final podés **exportar todo a Excel**.

### Deploy en Railway (u otro hosting)

1. En [railway.app](https://railway.app): **New Project → Deploy from GitHub repo**
   → elegir `conciliador-bancario`. Railway detecta el `Dockerfile` solo.
2. En **Variables** agregar:
   - `ANTHROPIC_API_KEY` — para las sugerencias con IA (opcional).
   - `CLAVE_ACCESO` — recomendado en deploys públicos: la app pide esta clave
     antes de dejar operar (los extractos son datos sensibles).
3. En **Settings → Networking → Generate Domain** para obtener la URL pública.
4. (Opcional) Montar un **Volume** en `/app/datos` para que las conciliaciones
   guardadas y las reglas aprendidas sobrevivan a los redeploys.

### IA (opcional)

Para habilitar las sugerencias con IA definí la variable de entorno
`ANTHROPIC_API_KEY`, o guardá la clave en `datos/anthropic_key.txt` (la carpeta
`datos/` nunca se sube al repo). Sin credenciales, el sistema funciona igual
con la conciliación determinística.

```bat
set ANTHROPIC_API_KEY=sk-ant-...
iniciar.bat
```

## Estructura

```
conciliador/
├── app.py                  # servidor FastAPI + export a Excel
├── parsers/
│   ├── santander_pdf.py    # parser del extracto PDF
│   └── mayor_xlsx.py       # parser del mayor (hojas E y O)
├── engine/
│   ├── matcher.py          # motor de conciliación determinística
│   └── ai_assist.py        # sugerencias con Claude (claude-opus-5)
└── static/index.html       # interfaz web
```

## Resultado

| Categoría | Significado |
|---|---|
| Conciliados (E) | Movimiento del banco con asiento confirmado en la cuenta E |
| Pendientes de confirmar (O) | Está en el banco y en la O: hay que confirmarlo en el FBS para que pase a la E |
| Banco sin contabilizar | Está en el extracto pero no aparece ni en E ni en O |
| Gastos bancarios | Comisiones/impuestos del banco sin asiento individual |
| Gastos por mes (ND) | Nota de débito mensual: gastos agrupados por categoría impositiva vs el asiento del mayor, con diferencia |
| Mayor E sin banco | Asiento confirmado que no aparece en el extracto |
| O pendientes sin banco | Pendiente en la O que tampoco está en el banco |
| Sugerencias IA | Posibles matches difíciles propuestos por Claude, para revisión humana |
