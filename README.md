# Conciliador bancario

Web app to reconcile **bank statements** (Banco Santander PDFs, and the XLS/XLSX/CSV exports of the group's
banks) against the general ledger of the **FES/FBS** accounting system (Excel with accounts **E** and **O**, or
pulled straight from the FBS SQL Server). Developer guide: [AGENTS.md](AGENTS.md). Audit: [AUDIT.md](AUDIT.md).

The UI, the Excel exports and every message are in Spanish (rioplatense); the code is in English.

## How it works

The accounting system books movements like this:

- **Account O (operative/transitory):** payment orders and receipts land here by default. They are movements
  *pending confirmation*.
- **Account E:** when the income/expense is confirmed, the counter-entry is made in O and the entry moves to E.
  Account E should match the statement.

The automatic reconciliation:

1. **Parses the statement PDFs** (debit/credit is checked against the balance arithmetic, so reading errors
   can't flip a sign).
2. **Parses the ledger Excel** (sheets "… E" and "… O").
3. **Nets account O**: cancels the entry + counter-entry pairs (movements already confirmed) and keeps only the
   pending ones.
4. **Matches statement vs account E** in three passes: amount + shared reference number → unique amount →
   amount + closest date. (Convention: bank credit ↔ ledger Debe.)
5. Whatever isn't in E is **looked up in the pending O** → those movements are in the bank but still have to be
   confirmed in FES.
6. Classifies the rest: **bank fees/taxes** (commissions, VAT, SIRCREB, law 25.413…) vs **unbooked
   movements**. Fees are also **grouped per period into the monthly debit note**, following the accounting
   criterion: commissions taxed at 21% with their VAT, interest on overdrafts at 10.5% with its VAT, and the untaxed
   concepts (law 25.413, SIRCREB, IIBB perceptions). The system finds the ledger's monthly "GASTOS BANCARIOS"
   entry (and the tax credit notes), compares the totals and shows the difference, checking that the VAT charged
   matches the one computed on the taxed base.
7. **(Optional) AI:** the cases left unsolved are sent to Claude, which suggests hard pairings (N-to-1
   combinations, commission differences, payee/CUIT matches) with a confidence level and a reason. Each suggestion
   can be **accepted with one click**.
8. **Manual reconciliation:** the "Conciliar a mano" tab shows the bank and ledger residuals in two filtered
   panels. Pick items on each side (N to M is supported), see the sum and the difference live, and reconcile the
   group. With a single item selected, the other panel is sorted by closeness of amount to find the counterpart
   fast. Everything can be undone, is saved to disk and goes into the Excel.
9. **Learning:** each manual reconciliation (or accepted AI suggestion) produces a rule in
   `datos/reglas_aprendidas.json` — the "signature" of the bank concept and of the entry. In later
   reconciliations, the pairs that meet a rule (with amounts that add up, even per-day groups with the same sum)
   are matched by themselves with the method "regla aprendida". Undoing a match weakens/removes the rule.

The **daily mode** (`/diario`) does the same per account for the day's statements of every bank, pulls ledger
E and O from FBS by date range (connections configured in the hub), and carries what didn't match over to the
next day's run.

## Usage

On Windows:

```bat
iniciar.bat
```

By hand (any system with Python 3.12):

```bash
pip install -r requirements.txt
python app.py
```

With Docker:

```bash
docker build -t conciliador .
docker run -p 8765:8765 conciliador
```

Open http://localhost:8765 — drop the statement PDFs and the ledger Excel, and press **Conciliar**. At the end
you can **export everything to Excel**.

### Deploy on Railway (or other hosting)

1. On [railway.app](https://railway.app): **New Project → Deploy from GitHub repo** → pick
   `conciliador-bancario`. Railway detects the `Dockerfile` by itself.
2. In **Variables** add:
   - `ANTHROPIC_API_KEY` — for the AI suggestions (optional).
3. In **Settings → Networking → Generate Domain** to get the public URL. **The app has no authentication of its
   own (see AUDIT.md F-01): don't publish a domain before that is solved.**
4. (Optional) Mount a **Volume** on `/app/datos` so the saved reconciliations and the learned rules survive
   redeploys.

### Progress and simulation mode

Processing runs in the background on the server: the UI shows an overlay with the current phase, percentage and
remaining time (`GET /api/progreso/<job>`). If the tab is closed, the process keeps going and coming back offers to
resume the result.

To try the UI without spending API, open it with **`?simular`** in the URL (e.g.
`http://localhost:8765/?simular`): with the AI box ticked, it produces test suggestions marked as SIMULACIÓN,
emulating the per-batch latency.

### AI (optional)

To enable the AI suggestions set the `ANTHROPIC_API_KEY` environment variable, or save the key in
`datos/anthropic_key.txt` (the `datos/` folder is never committed). Without credentials the system works the same
with the deterministic reconciliation.

```bat
set ANTHROPIC_API_KEY=sk-ant-...
iniciar.bat
```

## Structure

```
conciliador-bancario/
├── app.py                  # FastAPI server + Excel export
├── parsers/
│   ├── santander_pdf.py    # statement PDF parser
│   ├── ledger_xlsx.py      # ledger parser (sheets E and O)
│   └── daily_files.py      # daily multi-bank statements and FBS reports
├── engine/
│   ├── matcher.py          # deterministic reconciliation engine
│   ├── rules.py            # learned rules
│   ├── equivalences.py     # statement ↔ ledger vocabulary
│   ├── fee_terms.py        # user-defined bank fee terms
│   ├── accounts.py         # group bank accounts and FBS mapping
│   ├── fbs_sql.py          # read-only FBS SQL Server access
│   ├── ai_assist.py        # suggestions with Claude
│   ├── analysis.py         # AI analysis of pending entries
│   └── ai_usage.py         # AI usage log
└── static/                 # web UI (index.html monthly, diario.html daily)
```

## Result

| Category (as shown in the UI) | Meaning |
|---|---|
| Cruzados con la cuenta E | Bank movement with a confirmed entry in account E |
| Confirmar en FBS (cuenta O) | In the bank and in O: it has to be confirmed in FBS so it moves to E |
| Banco sin asiento | In the statement but neither in E nor in O |
| Gastos bancarios | Bank commissions/taxes without an individual entry |
| Nota de débito por mes | Monthly debit note: fees grouped by tax category vs the ledger entry, with the difference |
| Asientos E sin banco | Confirmed entry that isn't in the statement |
| Asientos O sin banco | Pending in O and not in the bank either |
| Sugerencias IA | Possible hard matches proposed by Claude, for human review |
