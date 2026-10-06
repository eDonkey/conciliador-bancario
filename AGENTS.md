<!-- BEGIN:caliper-standards -->
> **Org standards:** this repo follows `.standards/STANDARDS.md` (security, APIs,
> databases, LLM, code, tests, git). Read it before changing anything. Rules
> below are specific to this repo; they can be stricter than the standard, never looser.
<!-- END:caliper-standards -->

# conciliador-bancario

Bank reconciliation for the Nave group ("Conciliaciones → Bancaria" in the hub): bank statements (Santander PDF,
and the XLS/XLSX/CSV exports of Santander/Río, BBVA/Francés, Galicia, Macro and Ciudad) against the FES/FBS
general ledger (accounts E and O), with deterministic matching, learned rules, an optional Claude assistant, a
monthly mode (`/`) and a daily mode with carry-over between runs (`/diario`). Functional docs (Spanish):
[README.md](README.md), demo: [README-DEMO.md](README-DEMO.md).

- **Client / team:** nave
- **Criticality:** high — every brand's bank movements and ledger, reads FBS with stored credentials
- **Production:** two installs, both unconfirmed (see docs/AUDIT-standards.md OPS-01):
  - Railway service (public URL, built from the `Dockerfile`, branch `master` per the delivery docs) with a volume
    on `/app/datos`; a second Railway service with `DEMO_MODE=true` is the demo.
  - On-prem on the Nave server, behind the hub proxy (`/app/conciliador-bancario/`): NSSM service `conciliador`
    (`venv\Scripts\python.exe app.py`, 127.0.0.1:8765) installed by `hub-grupo/instalador`, which clones the default
    branch; `iniciar_8766.bat` runs the `fbs-sql` branch on port 8766 instead.
  - `fbs-sql` (contains `master`) is the reference branch; the team's security work landed there (PR #3).
- **Jira:** EA (delivery tickets of the first phase were in OR)
- **Audit:** the team's security audit is [AUDIT.md](AUDIT.md) (Spanish, updated with each fix); the
  platform-standards report is [docs/AUDIT-standards.md](docs/AUDIT-standards.md) (status: in-progress)

## Commands

| What | Command |
| --- | --- |
| Install | `pip install -r requirements-dev.txt` (pinned, hash-checked; production installs `requirements.txt`) |
| Add / upgrade a dependency | edit `requirements.in` (or `requirements-dev.in`), then `uv pip compile requirements.in --universal --python-version 3.12 --generate-hashes --no-header -o requirements.txt` and `uv pip compile requirements-dev.in --universal --python-version 3.12 --generate-hashes --no-header -o requirements-dev.txt`; commit `.in` and `.txt` together |
| Run locally | `python app.py` → http://127.0.0.1:8765 (`/` monthly, `/diario` daily). State goes to `datos/` |
| Run the demo | `pip install -r requirements-demo.txt`, then `DEMO_MODE=true python app.py`: simulated FBS and generated statements for the hub's accounts (`engine/simulacion.py`) |
| Tests | `python -m pytest -q` (runs a throwaway copy of the repo, never touches `datos/`) |
| Lint | `ruff check .` (config in `ruff.toml`; blocking in CI) |
| Typecheck | none yet |
| Build | `docker build -t conciliador-bancario .` |

## Architecture

```
app.py                     FastAPI: every route, background monthly job, daily mode (staging, carry-over, memory),
                           manual matches, vetoes, reclassifications, Excel exports, data export/import
engine/matcher.py          deterministic matching (E, then pending O), bank fees and the monthly debit note
engine/reglas.py           rules learned from manual matches
engine/equivalencias.py    statement ↔ ledger vocabulary pairs (e.g. haberes ≈ sueldos)
engine/gastos_conf.py      user-defined bank-fee terms and exceptions
engine/cuentas.py          group bank accounts (hub Postgres, local fallback, seed in config/) and their FBS mapping
engine/fbs_sql.py          FBS (SQL Server) reads, READ-ONLY (validated single SELECT, rollback-only transaction,
                           login without write roles), allowed destinations
engine/hub_cifrado.py      decrypts FBS passwords the hub stores encrypted (v1, AES-256-GCM with HUB_CLAVE)
engine/acceso.py           access middleware and login page (every route except /login and /health)
engine/seguridad.py        password sessions, sign-in limits, client IP, Origin check, the hub's signed identity
engine/presupuesto_ia.py   daily AI token budget (warning at 80 %, 429 above it)
engine/simulacion.py       demo only: simulated FBS ledger and bank statements in each bank's format
engine/ai_assist.py        Claude match suggestions (monthly mode, opt-in per run)
engine/analisis.py         Claude "why is this ledger entry pending" analysis, with a daily cap and a cache
engine/ia_log.py           log of every Anthropic call (tokens, estimated USD)
engine/telemetria.py       OpenTelemetry → New Relic. COPY of the `monitor` repo: don't edit here
parsers/                   Santander PDF, ledger Excel, and the daily multi-bank/FBS file parsers
static/                    index.html (monthly) and diario.html (daily), vanilla JS, no build step
scripts/backup_datos.py    dated zip of datos/ with 30-day retention (without credentials by default)
migrar_aprendizajes.py     copies the learned rules from one install to another through the export/import API
```

## Data sources and tenancy

| Source | Access | How | Notes |
| --- | --- | --- | --- |
| Uploaded files (bank statements, FBS ledger exports) | read | multipart uploads, parsed in memory | real bank movements of every brand |
| FBS (SQL Server, one per brand/company) | **read-only** | pyodbc (ODBC Driver 17/18) or pymssql; connections from the hub `fbs_conexiones` (generated query on `DetMov`) or the manual setup in `datos/fbs_sql.json` | three read-only layers: validator, rollback-only transaction, login without write roles (checked before the first query) |
| Hub Postgres: `marcas`, `cuentas_bancarias`, `fbs_conexiones` | **read-only** | psycopg2, `DATABASE_URL` | FBS passwords come plain or `v1:`-encrypted (decrypted only when connecting) and are never returned by the API |
| `datos/*.json` | read/write | plain JSON files | all of the app's state: runs (`<job>.json`, `grupo_<id>.json`), carry-over, memory, rules, vocabulary, fee terms, vetoes, reclassifications, "loaded in FBS" marks, AI cache/usage, FBS manual config (with password) |
| Anthropic API | outbound | `anthropic` SDK | unmatched movements/entries (descriptions, amounts, dates) are sent when AI is used |

**Tenant key:** `marca` (brand name) from the query string or form, used only to **filter** accounts and history;
it is not an access boundary (runs, results and learned rules are shared across brands and readable by id).

## Auth

`engine/acceso.py` guards every route except `/login` and `/health`:

- Through the hub: a request with `X-Orbit: 1` and a valid hub signature (`X-Orbit-Timestamp` + `X-Orbit-Firma`,
  key from `HUB_CLAVE`) is authenticated as the forwarded user (`request.state.hub`).
- Directly: one shared password (`APP_PASSWORD`) and a signed, expiring session cookie; sign-in attempts are
  limited per IP and globally. Without `APP_PASSWORD` only the machine itself (loopback, no proxy headers) is served.
- Writes (POST/PUT/DELETE) need a same-origin `Origin`. There are no roles: every signed-in user can do everything,
  and the brand is a filter, not a boundary.

## Repo-specific rules

- **Never write to FBS.** Only single `SELECT`/`WITH` statements go through `fbs_sql._consultar()`; keep the three
  read-only layers. The validator `_validar_solo_lectura` covers the same cases as `contrato-mcp`'s (strings,
  `[names]`, nested comments, lock hints): when one gets a fix, check the other.
- **The hub tables are read-only here.** The app only writes files under `datos/`.
- FBS passwords (hub `fbs_conexiones`, `datos/fbs_sql.json`, `FBS_SQL_CLAVE`) never go to the API, the UI or logs.
- `/health` is answered by the access middleware (`engine/acceso.py`), before auth.
- Job ids are 12 hex chars and daily run ids 10 hex chars: always validate them before joining them to a path.
- The AI only suggests: matches are applied by a person (manual match screen). Keep it that way.
- `engine/telemetria.py` is copied from the `monitor` repo: change it there and copy it back.
- `datos/` holds real data on every install: never commit it, never copy it to a dev machine.
- **Dependencies are pinned with hashes.** Never edit `requirements*.txt` by hand: change the `.in` file and recompile.
- User-facing text (UI, error messages, Excel headers and sheet names, prompts) stays in rioplatense Spanish.

## Glossary

| Domain term | In code | Notes |
| --- | --- | --- |
| conciliación / conciliar | `reconciliation` / `reconcile` | |
| extracto (bancario) | `statement` | |
| movimiento (del banco) | `movement` | class `MovimientoBanco` |
| libro mayor / mayor | `ledger` | FES/FBS "Consulta del Mayor" |
| asiento | `entry` | class `AsientoMayor` |
| cuenta E / cuenta O | `E` / `O` | confirmed vs. operative/transitory account; `hoja` = `sheet` |
| debe / haber | `debit` / `credit` (ledger side) | JSON keys `debe`/`haber` stay |
| débito / crédito | bank `debit` / `credit` | JSON keys `debito`/`credito` stay |
| gasto bancario | `bank_fee` | |
| nota de débito (ND) | `debit_note` | |
| contraasiento | `reversal` | |
| cruce / match | `match` | |
| arrastre | `carry_over` | daily mode: residuals of the previous run |
| memoria de conciliados | `matched_memory` | |
| veto / cruce anulado | `veto` | |
| reclasificación | `reclassification` | |
| regla aprendida | `learned_rule` | |
| equivalencia | `equivalence` | |
| corrida diaria / tablero | `daily_run` / `board` | file `grupo_<id>.json` |
| marca | `brand` | |
| empresa | `company` | |
| haberes / sueldos | `payroll` | |

**Public names that stay in Spanish** (the UI, stored data or other installs depend on them): routes and their
query/path/form parameters, every JSON key of requests, responses and stored files, `datos/` file names, env vars,
Excel sheet names and headers, the prompts sent to Claude, telemetry span names/attributes and the public API of
`engine/telemetria.py`, element ids and CSS classes, `iniciar*.bat`, and every user-facing text.

**Language migration:** not started. Rename-only commits, one module at a time (STANDARDS §6).

## Runbook

- **Diagnostics:** `GET /api/diagnostico` (AI credentials present, `datos/` is a volume, hub/FBS mode, AI spend, monitoring).
- **Restart:** Railway → redeploy · on-prem `nssm restart conciliador` (or `iniciar_8766.bat`).
- **Logs:** Railway service logs · on-prem `conciliador.log` in the app folder (NSSM) · New Relic service `conciliador` when `NEW_RELIC_LICENSE_KEY` is set.
- **Rollback:** Railway → Deployments → previous deployment → Redeploy · on-prem `git checkout <commit>` + restart.
  State in `datos/` is not rolled back.
- **Backup:** `scripts/backup_datos.py` (or `.bat`) zips `datos/`; it still has to be scheduled on each install
  (AUDIT.md, pending item 4). `GET /api/migracion/exportar` returns every JSON in `datos/` except credentials.
- **Owner:** Lautaro
- **Client offboarding (export + delete data):** export with `GET /api/migracion/exportar`, then delete the volume
  (Railway) or the `datos/` folder (on-prem). Runs are not separated by brand: removing one brand means deleting its
  runs by hand (`DELETE /api/diario/{grupo_id}` for each of its daily runs).

## Exceptions to the standards

| Rule | Why | Jira | Until |
| --- | --- | --- | --- |
| §5 Versioned migrations / DB | State is JSON files in `datos/`, no database of its own | TBD | a move to Postgres |
| §4 JSON error shape | Errors are `{"error": "<message>"}`, which the UI reads | TBD | review |

## Environment

All variables are documented in [.env.example](.env.example). Secrets (`APP_PASSWORD`, `SESSION_SECRET`,
`HUB_CLAVE`, `DATABASE_URL`, `ANTHROPIC_API_KEY`, `NEW_RELIC_LICENSE_KEY`, optional `FBS_SQL_CLAVE`) live in the Railway service variables or in the on-prem
service environment; only `DATABASE_URL` and the monitoring variables are also read from a local `.env`.
