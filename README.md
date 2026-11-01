# MailPilot

Local multi-Gmail client + local-LLM outreach system. Runs only on `http://127.0.0.1:8765`. No cloud APIs; mail goes through Gmail IMAP/SMTP; LLM calls go to Ollama on localhost.

See `EMAIL_HUB_SPEC.md` for the full product specification.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) (for draft generation / CV matching)
- Gmail accounts with **2-Step Verification** and an **app password**
- IMAP enabled in Gmail Settings → Forwarding and POP/IMAP

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install -r requirements.txt
```

Pull default models (adjust if you use others in Settings):

```bash
ollama pull qwen3.5:4b
ollama pull qwen3-embedding:0.6b
```

## Run

**Windows:** double-click `run.bat` (creates venv if needed, opens the browser).

**Linux/macOS:**

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Then open http://127.0.0.1:8765

## Gmail app password

1. Enable 2-Step Verification.
2. Open https://myaccount.google.com/apppasswords
3. Create a password named **MailPilot**.
4. Paste it into Accounts → Add account (spaces are stripped).

## Multi-sender (e.g. Usama + Faizan, 6 Gmail accounts)

1. On **Accounts**, set **Owner key** on each mailbox (`usama` or `faizan`) and optionally a **Default CV**.
2. On **CVs**, set the same **owner key** on each PDF so only that person’s CV is attached.
3. On **Profile**, save one profile per owner key (facts + name used in drafts).
4. **Campaigns** → leave account IDs blank and set **Owner filter** to `usama` or `faizan` (auto-picks that person’s accounts), or paste all 6 IDs to load-balance everyone.
5. Generate → the app assigns contacts by remaining daily capacity, sticky per contact for follow-ups, and picks CVs only from the sending account’s owner.

## Safety defaults

- **Dry-run is ON** until you turn it off in Settings. With dry-run, outbound mail is written as `.eml` files under `data/outbox_dryrun/` instead of SMTP.
- App passwords are encrypted with Fernet; the key lives in the OS keyring (or `data/.fernet_key` / `MAILPILOT_FERNET_KEY` as fallback).
- Sync uses `BODY.PEEK` and never marks mail as read.
- Server binds to `127.0.0.1` only.

## Tests

```bash
pytest -q
# or
bash scripts/check.sh   # ruff + pytest
```

Tests use a fake Ollama client and mocked IMAP — no network required.

## Layout

```
app/           FastAPI app, routes, mail/llm/importers/scheduling
data/          SQLite DB, CVs, logs, dry-run outbox (gitignored contents)
tests/
alembic/
EMAIL_HUB_SPEC.md
```
