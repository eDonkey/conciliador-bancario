# Audit · conciliador-bancario

| | |
| --- | --- |
| Client / team | nave (hub group: every brand's treasury) |
| Criticality | high |
| Audited by / date | Lautaro + Claude Code · 2026-10-06, updated 2026-10-13 on top of `fbs-sql` (PR #3) |
| Standards version | platform-standards v1-1-g3b6a331 |
| Jira epic | TBD (EA) |
| Status | in-progress |

This is the platform-standards report (checklist and findings in the org format). The team's own security audit,
done in parallel on branch `auditoria-seguridad` and merged into `fbs-sql` (PR #3), is [AUDIT.md](../AUDIT.md) in
Spanish; where both cover the same thing, AUDIT.md and its code are the reference and this report only cites them.

## Summary

The reconciliation logic is careful (FBS read-only in three layers, AI only suggests, a person applies every match),
and the git history is clean. The main problem found on 2026-10-06 was exposure: **the app had no authentication**
and the Railway service has a public URL (F-01). The team fixed that in PR #3 (`f317a43`: mandatory password and
signed session, fail closed without `APP_PASSWORD`; `3e25529`: the hub's signed identity), together with the FBS
destination checks (F-03), the AI token budget (F-07), real account numbers out of the code (F-20) and the decryption
of FBS passwords the hub stores encrypted (`452e701`). This branch adds what was still open from the first pass:
job and run ids validated before they become paths (the manual FBS config with its password was still readable
through `/api/resultado/fbs_sql`, F-02/F-04), the stored XSS in inline handlers (F-05), the demo never reaching a
real FBS (F-08), lock hints rejected by the read-only check (F-21), headers, hidden OpenAPI, Anthropic timeouts, CI,
smoke tests and pinned dependencies. Still open: no backup job for `datos/` (F-06, High), one shared password with no
users or roles (F-22), and the Medium/Low items below.

| Severity | Open | Fixed |
| --- | --- | --- |
| Critical | 0 | 2 |
| High | 1 | 5 |
| Medium | 7 | 0 |
| Low | 4 | 3 |

## Inventory

- **Runs on:** (1) Railway, public domain, `Dockerfile`, volume on `/app/datos`, branch `master` per the delivery docs
  (unconfirmed); a second Railway service with `DEMO_MODE=true`. (2) Nave server, NSSM service `conciliador`
  (`app.py`, 127.0.0.1:8765) behind the hub proxy `/app/conciliador-bancario/`; `iniciar_8766.bat` runs `fbs-sql`
  on `0.0.0.0:8766`.
- **Deploys from:** `master` (Railway, unconfirmed) / default-branch clone + `git pull` (on-prem) · **CI:** added in
  this pass (on `fbs-sql`, which contains `master`)
- **Routes:** 47 in `app.py` + `/static`, plus the login page in `engine/acceso.py`; `/health` is answered by the
  access middleware. 26 are POST or DELETE. FBS: `/api/fbs-sql*`, `/api/diario/fbs-sql`, `/api/conciliar` with
  `fbs_cuenta`; Claude: `/api/conciliar` with `usar_ia=si`, `/api/analizar/*`; whole-state export/import:
  `/api/migracion/*`; demo only: `/api/demo/*`
- **Auth:** `engine/acceso.py` on every route except `/login` and `/health`: the hub's signed identity (`X-Orbit-*`,
  HMAC with a key derived from `HUB_CLAVE`) or one shared password (`APP_PASSWORD`) with a signed, expiring cookie;
  without `APP_PASSWORD` only loopback without proxy headers is served
- **Tenant key:** `marca` from the query string / form, a display filter only
- **Data sources:** uploaded bank statements and FBS ledger exports · FBS SQL Server per company (read-only) · hub
  Postgres `marcas`, `cuentas_bancarias`, `fbs_conexiones` (read-only) · `datos/*.json` (all state, read/write)
- **External services:** Anthropic (Sonnet for match suggestions, Haiku for analysis) · New Relic (OTLP)
- **Secrets (names only):** `APP_PASSWORD`, `SESSION_SECRET`, `HUB_CLAVE` / `HUB_CLAVE_ANTERIOR` (or
  `HUB_FIRMA_CLAVE`), `DATABASE_URL`, `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`, `NEW_RELIC_LICENSE_KEY`,
  `FBS_SQL_CLAVE` · FBS passwords in hub `fbs_conexiones` (plaintext, or `v1:` encrypted once the hub turns on
  `HUB_FBS_CIFRADO`) and `datos/fbs_sql.json` (manual mode) · optional `datos/anthropic_key.txt`
- **Personal data:** bank movements carry counterparty names, CUIT and payroll payees; ledger comments carry
  customer and supplier names. Kept forever in `datos/`; unmatched rows are sent to Claude when AI is used.

## Automated scans

| Tool | Result | Notes |
| --- | --- | --- |
| gitleaks (history, `fbs-sql` + this branch) | ✅ clean | |
| pip-audit `requirements.txt`, `requirements-demo.txt` | ✅ clean | after pinning |
| trivy fs (HIGH/CRITICAL) | ⚠️ | no vulnerable packages; DS-0002 container runs as root (F-12) |
| ruff (`ruff.toml` baseline) | ✅ fixed | findings cleaned (also two in the new FBS destination check); lint blocking |
| pytest | ✅ 129 passed | 81 from the team's PR #3, plus read-only regression cases, smoke and security tests |
| semgrep p/default | pending | runs in `security.yml` on the first PR (not installable on this Windows host) |
| docker build | ✅ | image builds with hash-checked requirements, `/health` 200, no `datos/`/`.env` inside |

## Checklist

Only ⚠️ / ❌ rows; everything else ✅ or N/A.

| Id | Result | Note |
| --- | --- | --- |
| SEC-03 | ⚠️ | `datos/anthropic_key.txt` is still supported (F-17); `datos/fbs_sql.json` holds the manual FBS password (0600, never exported, F-02) |
| SEC-06 | ⚠️ | hub `fbs_conexiones` passwords are plaintext until the hub turns on `HUB_FBS_CIFRADO`; this app already decrypts them (`452e701`) |
| SEC-07 | ⚠️ | rotation not documented (F-02 asks for one now) |
| AUTH-01…07 | ⚠️ | login, signed expiring session, rate limit and cookie flags done upstream; one shared password, no users (F-22) |
| AUTHZ-02…06 | ⚠️ | every route needs a session now; no roles (FBS config, export/import, memory delete, diagnostics are open to every signed-in user) and the brand is a filter, not a boundary (F-22) |
| AUTHZ-07 | ⚠️ | `iniciar_8766.bat` / `iniciar_red.bat` listen on the LAN; covered by the password now (F-01) |
| API-01 | ⚠️ | bodies are plain dicts checked by hand; ids validated (F-04) |
| API-03 | ⚠️ | exception text returned to the UI (F-10) |
| API-05 | ⚠️ | `nosniff` + `Referrer-Policy`; no CSP / `frame-ancestors` yet (F-05) |
| API-06 | ⚠️ | no cap on FBS date range, `limite`, history size; no pyodbc query timeout (F-09) |
| API-07 | ❌ | uploads without size limit, in-memory staging without expiry (F-09) |
| API-10 | ⚠️ | manual FBS destination editable only from the machine itself or with `FBS_SQL_EDITABLE`, private addresses only (F-03) |
| DATA-03 | N/A | no database of its own (exception in AGENTS.md) |
| DATA-06 | ⚠️ | money as float in memory and JSON (F-19) |
| DATA-08 / DATA-09 | ❌ | backup script exists (`scripts/backup_datos.py`), not scheduled, no restore test (F-06) |
| DATA-10 / PRIV-01 / PRIV-05 | ❌ | runs kept forever, no inventory or retention, offboarding by hand (F-11) |
| LLM-03 | ⚠️ | "corrections" become prompt context for future analyses; who wrote them is not recorded (F-13) |
| PRIV-04 | ⚠️ | up to 80 bank + 400 ledger rows of free text per AI batch (F-11) |
| DEP-03 | ⚠️ | `.dockerignore` added; root user kept on purpose (F-12); base image not pinned by digest |
| OPS-01 / OPS-02 | ❌ | two installs from two branches (F-14) |
| OPS-03 | ⚠️ | telemetry wired; alerts not verified; the 80 % AI budget warning only goes to the log |
| OPS-04 | ⚠️ | `print()` logging; the full FBS query is printed (F-19) |
| OPS-06 | ⚠️ | on-prem `.env` lives inside the repo folder (F-17) |
| QA-05 | ⚠️ | test-phase leftovers: `DELETE /api/diario/memoria`, "TEMPORAL" `iniciar_8766.bat` (F-18) |
| QA-07 | see branch `refactor/conciliador-bancario-english` |

## Findings

### F-01 · No authentication: the public Railway URL exposed and let anyone change every brand's treasury data
- **Severity:** Critical · **Checklist:** AUTH-*, AUTHZ-02…07, SEC-05, QA-03 · **Jira:** TBD
- **What (2026-10-06):** every route answered anonymously; the brand (`?marca=`) only filtered what the UI listed.
  With the Railway URL anyone could download every run, delete runs and memory, poison learned rules, pull ledgers
  from production FBS and spend Anthropic credit.
- **Fix (done upstream):** `f317a43` mandatory login (`APP_PASSWORD`, signed session with expiry, sign-in limits,
  `Origin` check, fail closed without the password) and `3e25529` the hub's signed identity. See AUDIT.md
  "CRÍTICO: la app no tenía login" and "Identidad firmada del hub".
- **Still to do (owner):** take the Railway URL off the internet or put it behind the hub (AUDIT.md pending 1);
  the shared password itself is F-22.
- **Status:** fixed (upstream)

### F-02 · FBS password readable through the API
- **Severity:** Critical · **Checklist:** SEC-03, SEC-06, API-03 · **Jira:** TBD
- **Where:** `app.py` `_obtener` and the data export; `engine/fbs_sql.py` `guardar_conf`
- **What:** `GET /api/resultado/fbs_sql` returned `datos/fbs_sql.json` verbatim (any `datos/<name>.json` was a
  "job"), and the data export included it. Saving the FBS form also copied `FBS_SQL_CLAVE` from the environment
  into that file. After PR #3 the export no longer includes it, but `/api/resultado/fbs_sql` still returned it to
  any signed-in user.
- **Fix (done):** job ids must be 12 hex chars (this branch); `fbs_sql.json` is never exported or imported
  (upstream `f317a43`); env values are never written to the file (this branch). Tests in `tests/test_security.py`
  and `tests/test_fbs_ia.py`.
- **Still to do (owner):** if the manual FBS mode was ever configured on Railway (or `FBS_SQL_CLAVE` set there),
  rotate that FBS password.
- **Status:** fixed (rotation pending)

### F-03 · Anyone could point the FBS connection at another server and run their own SELECT
- **Severity:** High · **Checklist:** API-10, AUTHZ-06, LLM-04, DATA-02 · **Jira:** TBD
- **Where:** `POST /api/fbs-sql`, `POST /api/fbs-sql/probar`, `engine/fbs_sql.py`
- **Fix (done upstream, `f317a43`):** the destination can only change from the machine itself or with
  `FBS_SQL_EDITABLE=1`, is fixed when `FBS_SQL_SERVIDOR` is set, must resolve to a private address (or be in
  `FBS_SQL_HOSTS_PERMITIDOS`), the stored password is dropped when it changes, and the FBS login is checked for
  write roles before the first query. The query stays editable, validated read-only (F-21).
- **Residual:** `probar` returns the driver's error text (F-10); no query timeout or date-range cap (F-09).
- **Status:** fixed (upstream)

### F-04 · Run ids reached files outside `datos/`
- **Severity:** High · **Checklist:** API-01 · **Jira:** TBD
- **Where:** `app.py` `_obtener`, `_guardar`, `/api/diario/{grupo_id}*`, `POST /api/gastos` (`job_id` in the body)
- **What:** `job_id` from the body of `POST /api/gastos` was joined to a path (`../x` read and rewrote any JSON file
  the process could reach); on Windows `..\` in a path id did the same for every job/run route.
- **Fix (done):** the access middleware rejects `\`, `..` and control characters in the URL (upstream); job ids
  `^[0-9a-f]{12}$` and run ids `^[0-9a-f]{10}$` are checked before any path is built, which also covers ids in
  bodies (this branch). Tests added.
- **Status:** fixed

### F-05 · Stored XSS through bank descriptions and file names
- **Severity:** High · **Checklist:** API-05 · **Jira:** TBD
- **Where:** `static/index.html`, `static/diario.html` (inline `onclick="fn('${esc(x)}')"`)
- **What:** `esc()` escapes HTML, but the browser decodes entities before running an inline handler, so a quote in a
  transfer concept (set by whoever sends the money) or in an uploaded file name broke out of the JS string.
- **Fix (done):** handler arguments go through `jsArg()` (a JSON literal, HTML-escaped). Still to do: a CSP without
  inline scripts, which needs the handlers moved to `addEventListener` (Medium, with F-10).
- **Status:** fixed

### F-06 · No backups of `datos/`, which is the whole state
- **Severity:** High · **Checklist:** DATA-08, DATA-09, JOB-03 · **Jira:** TBD
- **Where:** Railway volume `/app/datos`; on-prem `C:\apps\conciliador\datos`
- **What:** runs, carry-over chain, matched memory, learned rules and FBS mappings are JSON files with no backup
  job, no restore test and no alert.
- **Partly done upstream:** `scripts/backup_datos.py` / `.bat` (dated zip, 30-day retention, no credentials by
  default). Not scheduled anywhere yet, no off-site copy, no restore test (AUDIT.md pending 4).
- **Status:** open — owner: does the Railway volume have backups? Add the script to the Nave nightly task.

### F-07 · AI spend not capped for match suggestions
- **Severity:** High while F-01 was open, Medium after · **Checklist:** LLM-05 · **Jira:** TBD
- **Fix (done):** daily token budget for every AI call with a warning at 80 % and 429 above it, model ids only from
  server config (upstream `f317a43`, `engine/presupuesto_ia.py`); timeouts and `max_retries=2` on the Anthropic
  clients (this branch). Pending (owner): the actual daily number and who receives the warning (AUDIT.md pending 5).
- **Status:** fixed

### F-08 · The demo could reach real FBS and export the service's data
- **Severity:** High · **Checklist:** PRIV-02 · **Jira:** TBD
- **What:** README-DEMO says the demo never connects to FBS; the code did not enforce it.
- **Fix (done):** upstream `96698bc` simulates the FBS for the hub's accounts in `DEMO_MODE`; this branch makes
  `fbs_sql._consultar` refuse any real connection in the demo (the manual mode was not simulated) and the data
  export/import answer 404. Residual: the demo service's `DATABASE_URL` and `ANTHROPIC_API_KEY` must be demo-only
  values (verify on Railway).
- **Status:** fixed

### F-09 · Uploads and in-memory state without limits
- **Severity:** Medium · **Checklist:** API-06, API-07 · **Jira:** TBD
- **What:** uploads are read whole into memory with no size limit; `STAGING`, `PROGRESO` and `RESULTADOS` never
  expire; FBS date range, `/api/diario/conciliados?limite=` and the history are unbounded; no pyodbc query timeout.
- **Fix (proposed):** 20 MB per file / 100 MB per request, TTL for staging and progress, caps on ranges.
- **Status:** open

### F-10 · Internal errors returned to callers
- **Severity:** Medium · **Checklist:** API-03, AUTHZ-06 · **Jira:** TBD
- **What:** job, FBS and AI errors are returned as `str(exc)` (driver messages, hosts). `/api/diagnostico` no longer
  lists env var names or the key length (upstream) and needs a session, but is not admin-only.
- **Fix (proposed):** generic messages + request id, details in logs; a short, sanitized FBS error for the operator.
- **Status:** open

### F-11 · Personal data: no inventory, kept forever, broad AI context
- **Severity:** Medium · **Checklist:** DATA-10, PRIV-01, PRIV-04, PRIV-05 · **Jira:** TBD
- **What:** statements include payroll payees, counterparties and CUITs; runs are never deleted; an AI batch sends
  up to 80 bank and 400 ledger rows of free text.
- **Fix (proposed):** retention period for runs (owner), mask names/ids before sending to Claude, document export
  and delete per brand. Same as AUDIT.md pending 7.
- **Status:** open — owner decision on retention

### F-12 · Container runs as root
- **Severity:** Medium · **Checklist:** DEP-03 · **Jira:** TBD
- **What:** trivy DS-0002. Not changed on purpose: Railway mounts the volume on `/app/datos` owned by root, so a
  non-root user would lose write access to all state.
- **Fix (proposed):** run as a user and `chown` the volume at start (or `RAILWAY_RUN_UID`), tested on a Railway
  preview first; pin the base image by digest.
- **Status:** open

### F-13 · User input becomes AI context and matching rules, without an author
- **Severity:** Medium · **Checklist:** LLM-03 · **Jira:** TBD
- **What:** analysis "corrections" are sent to Claude as "criterio a respetar" in later analyses, and manual matches
  become rules applied to every future run. Since PR #3 only signed-in users can write them, but who did is not
  recorded (the hub user is in `request.state.hub`, AUDIT.md pending 6). The model itself never writes.
- **Status:** open

### F-14 · Two installs, two branches, no CI gate
- **Severity:** Medium · **Checklist:** OPS-01, OPS-02 · **Jira:** TBD
- **What:** Railway builds `master`; the on-prem installer clones the default branch (`master`) while
  `iniciar_8766.bat` (marked "TEMPORAL") runs `fbs-sql` with `--reload` after each `git pull`. `fbs-sql` is 51
  commits ahead of `master` (it contains it). The installer writes `ANTHROPIC_API_KEY` into the app's `.env`, which
  the app never reads.
- **Fix (proposed):** one reference branch (`fbs-sql` today, `main` after the transfer) as default branch, Railway
  and the installer on it with "wait for CI"; read `ANTHROPIC_API_KEY` from `.env` or set it in the NSSM environment.
- **Status:** open — owner: which branch does each install run today? (AUDIT.md pending 8)

### F-15 · API description public, no security headers
- **Severity:** Low · **Checklist:** API-05, API-08, API-09 · **Jira:** TBD
- **Fix (done):** `/docs`, `/redoc`, `/openapi.json` disabled; `X-Content-Type-Options` and `Referrer-Policy` on
  every response (also the login page and the 401/503 answers); `/health` returns the standard `status` and
  `version` next to the existing `ok`.
- **Status:** fixed

### F-16 · No CI, tests, pinned dependencies or env documentation
- **Severity:** Low · **Checklist:** DEP-01, QA-01, QA-02, SEC-04 · **Jira:** TBD
- **Fix (done):** standard CI; smoke and security tests on a throwaway copy of the repo (the team's tests from PR #3
  run there too); dependencies pinned with hashes (including the demo's `xlwt`); `.env.example` with every
  variable; lint clean and blocking; `.dockerignore`.
- **Status:** fixed

### F-17 · Secrets read from inside the app folder
- **Severity:** Low · **Checklist:** SEC-03, OPS-06 · **Jira:** TBD
- **What:** `datos/anthropic_key.txt` is loaded into the environment and the README recommends it; on-prem the
  `.env` lives in the repo folder, readable by whoever can read the app.
- **Fix (proposed):** env only (drop the file once no install uses it); service `.env` outside the repo, ACL to the
  service account.
- **Status:** open

### F-18 · Test-phase leftovers
- **Severity:** Low · **Checklist:** QA-05 · **Jira:** TBD
- **What:** `DELETE /api/diario/memoria` ("fase de prueba"), `iniciar_8766.bat` ("TEMPORAL", kills whatever listens
  on 8765), `migrar_aprendizajes.py` with the production URL hard-coded.
- **Fix (proposed):** remove or put behind an admin role; URL as an argument.
- **Status:** open

### F-19 · Money as float, `print()` logging
- **Severity:** Low · **Checklist:** DATA-06, OPS-04 · **Jira:** TBD
- **What:** amounts are floats rounded to 2 decimals everywhere (consistent, no observed error); logs are `print()`
  and the FBS query is printed in full (no secrets, but noisy).
- **Status:** open (backlog)

### F-20 · Real account numbers in code and history; data on a dev machine
- **Severity:** Low · **Checklist:** PRIV-03 · **Jira:** TBD
- **What:** the group's real bank account numbers (company accounts, not personal data) were seeded in
  `engine/cuentas.py`. Upstream `f317a43` moved them to the git-ignored `config/cuentas_semilla.json`; they remain
  in the git history (AUDIT.md pending 3). The local clone used for this audit has a `datos/` folder with 3 jobs
  and 2 daily runs: check whether they are real and delete them if so.
- **Status:** open (history and local data)

### F-21 · Lock hints passed the read-only FBS check
- **Severity:** Low · **Checklist:** DATA-02 · **Jira:** TBD
- **What:** the validator rewritten in `f317a43` already fixed the `'--'`-inside-a-string bypass found here and in
  contrato-mcp, but `SELECT ... WITH (TABLOCKX)` / `(UPDLOCK, HOLDLOCK)` still passed; inside the never-committed
  transaction they keep the FBS table locked until the rollback.
- **Fix (done):** lock hints are forbidden words; `tests/test_solo_lectura.py` keeps the bypass cases.
- **Status:** fixed

### F-22 · One shared password, no users or roles
- **Severity:** Medium · **Checklist:** AUTH-01…07, AUTHZ-02…06 · **Jira:** TBD
- **What:** direct access (Railway, LAN) uses one `APP_PASSWORD` for everybody: no per-person accounts, no logout of
  a single person, no record of who did what; every signed-in user can change the FBS query, export/import all
  data and delete the matched memory, for every brand. Through the hub the user is known but not used for
  permissions yet.
- **Fix (proposed):** access only through the hub (its signed identity), brand permissions from `usuarios.accesos`,
  an admin role for FBS config, export/import, memory delete and diagnostics; record the user on writes. Same as
  AUDIT.md pending 6 ("deuda del hallazgo 3").
- **Status:** open

## Waiting on answers from the owner

1. F-01: is the Railway service (and its public domain) still used? Can it be taken off the internet or put behind
   the hub?
2. F-02: was the manual FBS mode (or `FBS_SQL_CLAVE`) ever set on Railway? If yes, rotate that password.
3. F-06: does the Railway volume have backups? Schedule `scripts/backup_datos.bat` in the Nave nightly backup?
4. F-14: which branch does each install run today (Railway, Nave server 8765, 8766)? Should `fbs-sql` become the
   default branch?
5. F-11: how long should runs be kept?
