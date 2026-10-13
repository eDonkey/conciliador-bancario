# Demo environment (conciliador-bancario)

Same code as the company's (branch `fbs-sql`); the demo environment is a
Railway service whose only difference is `DEMO_MODE=true`. Without that
variable, the behavior is exactly the usual one.

## What DEMO_MODE turns on

- **Simulated FBS** (`engine/simulacion.py`): there is no SQL Server. The
  accounts are the demo hub's (`cuentas_bancarias` with an FBS connection and
  E/O ids, seeded by `hub-grupo/scripts/seed-demo.js` with the brands chosen
  when the demo is prepared) and "Traer E y O del FBS" returns the
  simulation's entries instead of querying `DetMov`. "Probar conexión" says OK.
- **Generated statements**: `/diario` and `/` show the "Extractos de la demo"
  block: pick an account (or all of the brand's) and a period, and the
  statement downloads **in the home banking format of its bank**
  (Santander/Río text `.xls`, BBVA `.xls`, Macro `.xls`, Ciudad `.csv`), or
  loads with one click into the statements area. It comes from the same
  simulation as the ledger: what is uploaded and what FBS returns tell the
  same story.
- `POST /api/demo/reset`: clears the matched memory, the carry-over and the
  history (jobs and boards). The simulation stores nothing.
- `X-Robots-Tag: noindex, nofollow` header on every response.
- Outside `DEMO_MODE`, none of these routes or UI exist.

## The simulation

Deterministic: each operation is born from a seed (account + day), so the
same day always gives the same result and the dates are always this week's.
Per account and business day:

| Case | Bank | FBS | Where it shows |
|---|---|---|---|
| Collections by transfer (deposits, unit balances) | credit with CUIT and transfer number | receipt in O and its confirmation in E | Reconciled (E) |
| A collection not confirmed yet | credit | only the O entry | Pending confirmation (O) |
| A collection with a small difference (the customer deducted the fee) | credit for a bit less | receipt for the total | Reconciled, "approximate amount" |
| One day out of three, a collection nobody recorded | credit | nothing | Bank not booked |
| Cash deposits, card settlements, payments to suppliers and to the manufacturer, AFIP VEPs, deferred-payment cheques | yes | yes, the same day | Reconciled |
| 48-hour cheque deposit / regular cheque issued | credited or debited days later | on the day of the deposit or issue | Solved by the carry-over |
| Payroll (4th business day of the month) | debit | nothing (the payroll entry is booked at month end) | Payroll not booked |
| A cancelled payment | nothing | entry and reversal in E | Reversals |
| Fees, VAT, Law 25.413, SIRCREB | debits | the month's debit note, on the last business day | Bank fees (in the monthly mode they close against the debit note) |

Each brand's main account (the first one in pesos) moves more than the
others. The dollar accounts have few operations.

## Suggested walkthrough

- **Daily**: pick "Hoy" and "Todas las cuentas" → "Cargar en la zona de
  abajo" → "Identificar archivos" → "Traer E y O del FBS" → "Conciliar el
  día". To show the carry-over: first the previous business day and then
  today.
- **Monthly**: "Mes anterior" on the main account → "Cargar extracto y
  mayor" → "Conciliar": almost everything matches and the fees close against
  the debit note.

## Creating the service on Railway

1. A service from this repo, branch **`fbs-sql`** (the one the company
   uses). It builds with the `Dockerfile`, which also installs
   `requirements-demo.txt` (`xlwt`, for the BBVA and Macro `.xls` files).
2. Variables: `DEMO_MODE=true` and the `DATABASE_URL` of the **demo**
   Postgres (the demo hub's, never production's).
3. It opens from the demo hub, same as at the company: the hub publishes it
   at `/app/conciliador-bancario/` with `HUB_PROXY_CONCILIADOR_BANCARIO` = this
   service's URL.
