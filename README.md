### Support AI

A self-hosted, non-payable diagnostic assistant for Frappe/ERPNext. It reads (never writes) the
current state of a document, its workflow, related error logs, and any custom code touching its
doctype, then explains **what is wrong, why, and how to fix it**. It never claims to perform a
fix itself.

### Why

- **No paid API required.** Ships with a local [Ollama](https://ollama.com) adapter by default.
  An OpenAI-compatible free-tier hosted API (Groq, OpenRouter, etc.) can be used instead/as well
  — pick per-install in Support AI Settings.
- **Understands standard ERPNext issues**: a Sales Invoice won't submit? Support AI checks
  permissions, mandatory fields, workflow state/allowed transitions, related Error Log entries,
  and (optionally) re-runs `validate()` in a rolled-back savepoint to surface the exact error.
- **Understands your custom apps too.** For the doctype in question it looks at any custom
  `doc_events` hooks, Server Scripts, Client Scripts, Custom Fields, Property Setters, and the
  doctype's own controller source if it lives in a custom app — so the diagnosis accounts for
  what your team actually built, not just stock ERPNext.
- **Diagnosis only.** The LLM is never given write access to anything; it only explains root
  cause and recommended steps.

### Setup

1. **Pick an LLM backend** — Desk → *Support AI Settings*:
   - **Ollama (recommended, free, local)**: install Ollama, `ollama pull llama3.1` (or any model
     you prefer), then set *Ollama Base URL* (default `http://localhost:11434`) and *Ollama Model*.
   - **OpenAI Compatible (free-tier hosted)**: set *API Base URL* (e.g.
     `https://api.groq.com/openai/v1`), *Model Name*, and *API Key*.
   - Use *Test Connection* to verify.
2. Review the **Diagnostics** section in Settings:
   - *Run Live Validation Check* is **off by default**. It re-runs the document's `validate()`
     logic to surface the exact error Frappe would raise. It executes inside a DB savepoint that
     is always rolled back, with a hard 10s timeout — but a savepoint can't undo an email, webhook,
     or external API call a custom hook might trigger. Only turn this on once you've audited the
     custom `validate()` hooks on the doctypes you intend to diagnose.
   - *Core Apps* lets you customize which installed apps are treated as "core" (and therefore
     excluded from the custom-code scan). Defaults to frappe/erpnext/hrms/payments/india_compliance.

### Usage

- Open any document, then **Menu → Ask Support AI** — this opens the chat pre-loaded with that
  document's context (e.g. "Sales Invoice INV-001 won't submit, what's wrong?").
- Or go to the **Support AI** page directly from Desk for a general question.

### Answers from your database first

Support AI checks your data before it asks the AI model. Plain data
questions get an answer from the database, so the answer is correct and
fast. The asking user's permissions always apply.

- **Document IDs and emails.** Type an ID such as `ACC-PINV-2026-17381`
  and Support AI finds that document. Type a user's email and it filters
  by **Created By**.
- **Lists, counts and totals.** For example: "latest 3 purchase invoices",
  "how many sales orders this quarter", "total amount of purchase invoices
  this month".
- **Dates.** "today", "last 7 days", "this week", "last month",
  "this quarter", "this financial year", "between 1 Sep 2026 and 15 Sep
  2026", "since 1 Jan".
- **Amounts.** "above 1 lakh", "less than 50k", "at least 2 crore".
- **Linked values.** "for company Jio", "of supplier Goodedge".
- **Items.** "invoices containing item laptop", "items in
  ACC-PINV-2026-17381".
- **Approvals.** "approval history of ACC-PINV-2026-17381" shows who moved
  the document through each workflow state, and who must act next.
- **Stock and dues.** "stock of item laptop", "total outstanding of
  suppliers", "receivable of customers".
- **Your work.** "What needs my attention?" or "my tasks" lists your
  pending approvals, open tasks and unread notifications. A question with
  the word "todo" alone, for example "find todo X", searches the ToDo
  list instead.
- **Unclear words.** "latest invoices" asks: Sales Invoice or Purchase
  Invoice?
- **Follow-ups.** A follow-up message keeps the document type of your
  earlier question.

Questions that ask "why" or "how to fix" go to the AI model. The model
also gets the real data for any ID or email in the question.

### Chat features

- Document IDs in answers are links. Click one to open the document.
- Lists show as tables, with **Open list** and **Export to CSV** buttons.
- Each new chat shows suggested questions for your roles.
- On a list view, "how many of these" uses the list's filters.
- The AI model's reply shows word by word while it is written.
- Rate each answer with 👍 or 👎.
- Speak a question with the mic button (Chrome and Edge only).
- An error dialog on a form shows **Ask Support AI why**.
- A data change that the AI suggests runs only after you click
  **Confirm**.
- Each answer ends with a **Source** line that shows where the data came
  from.

### Settings for administrators

In **Support AI Settings**:

- **Reasoning Model**: an optional larger model, for example
  `qwen2.5:7b`, for "why / how to fix" questions. Simple data questions
  still use the main model. Pull the model first with `ollama pull`.
- **Stream Replies**: show the reply word by word. On by default.
- **Max Questions per User per Minute**: the default is 20. Set 0 for no
  limit.
- **Sensitive Fields**: fieldnames, or parts of fieldnames, whose values
  Support AI masks. Examples: PAN, bank account, salary. Users with the
  **Role That Can See Sensitive Fields** see the real values.
- **Synonyms**: your own words for document types, for example
  `GRN = Purchase Receipt`.
- **Suggested Questions**: one-click questions for each role.
- **Daily Digest**: each morning, email the users with the **Digest Role**
  a list of what needs their attention.

### Reports

- **Support AI Usage**: answers per day, the share answered from the
  database, the average response time, 👍/👎 counts and answers that
  found nothing.
- **Support AI Unanswered Questions**: questions with a 👎, an error or
  no result, shown next to the answer. Use this report to find missing
  synonyms, suggested questions or knowledge files.

### Tests

The tests are in `support_ai/tests/`. They use core DocTypes (ToDo, User,
Role), so they run on any site. Tests that need ERPNext data skip on a site
without ERPNext. The tests never call an AI model: the model is replaced
by a stub.

| Module | What it tests |
| ------ | ------------- |
| `test_router` | Questions to plans: IDs and emails typed in the question, follow-ups, list-view and form context, status, limits, synonyms, aliases, and questions that go to the AI model. |
| `test_filters` | Date ranges, amounts, linked values and item filters. |
| `test_answers` | Document details, child tables, approval history, counts, clarifying questions, the attention inbox, formatting and masking of sensitive fields. |
| `test_chat` | One chat turn (database or AI model), saved answer details, the reasoning model, rate limit, feedback, export, suggested questions, Confirm/Cancel for data changes, and streaming. |
| `test_admin` | The Usage and Unanswered Questions reports, the daily digest and the settings defaults. |

Run all tests:

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app support_ai
```

Run one module:

```bash
bench --site <test-site> run-tests --module support_ai.tests.test_router
```

Use a test site. Do not run tests on a production site. When an answer is
wrong, add the question as a test case in `test_router`.

### Running in this environment

Ollama and the Frappe web server were installed/started manually here (no root access, so no
systemd unit):

```bash
# Ollama (user-space install, already pulled llama3.2:3b)
OLLAMA_MODELS=$HOME/.local/ollama/models ~/.local/ollama/bin/ollama serve &

# Frappe web server
cd $PATH_TO_YOUR_BENCH && bench serve --port 8000 &
```

Both need to be running for the chat to work; if the machine restarts, re-run the two commands
above. CPU-only inference on `llama3.2:3b` takes roughly 1-2 minutes per reply here — that's
expected, not a bug. For faster responses, either use a smaller model or point Support AI Settings
at a free-tier hosted OpenAI-compatible API instead.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app support_ai
```

### License

mit
# ERPNext-Support
