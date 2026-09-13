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
