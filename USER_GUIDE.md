# MailPilot User Guide

Local multi-Gmail client + local-LLM outreach for PhD professor emails. Runs only on `http://127.0.0.1:8765`. Mail uses Gmail IMAP/SMTP; drafts and the Workflow Guide coach use **Ollama on localhost**.

Also see `EMAIL_HUB_SPEC.md` (product spec) and the in-app **Guide** page (`/guide`).

---

## 1. What MailPilot does

| Area | What you get |
|------|----------------|
| Accounts | Multiple Gmail mailboxes, Fernet-encrypted app passwords, per-account daily caps |
| Inbox | Local sync with `BODY.PEEK` (never marks read), filters, reply/bounce detection |
| Contacts | CSV/XLSX/TXT import with column mapping; China professors CSV auto-normalize |
| CVs | PDF library + embeddings for best-CV matching |
| Templates / Profile | Seeded Western + China/CSC templates; allowed facts for the LLM |
| Campaigns / Drafts | Generate → validate → approve → queue |
| Queue | Rate-limited sends; **dry-run ON by default** (writes `.eml` files) |
| Guide | Checklist + Ollama coach that knows the outreach playbook |

---

## 2. Install on Windows

1. Install **Python 3.11+** and [Ollama](https://ollama.com).
2. Clone the repo somewhere writable (e.g. `Documents\Email-sender`), **not** `System32`.
3. Double-click `run.bat` (creates `.venv`, installs deps, opens the browser).
4. Pull models (names must match **Settings**):

```bash
ollama pull qwen3.5:4b
ollama pull qwen3-embedding:0.6b
```

5. If Ollama returns 404 on CV suggest / draft: the model name is missing — run `ollama list` and either pull it or change Settings → drafting model.

### Gmail app password

1. Enable 2-Step Verification.
2. Create an app password named **MailPilot**: https://myaccount.google.com/apppasswords  
3. Paste into Accounts → Add (spaces are stripped).
4. Enable IMAP in Gmail Settings → Forwarding and POP/IMAP.

---

## 3. Smooth first-run checklist

Use **Guide** in the sidebar (or `/guide`). It shows live status and can ask Ollama “what next?”.

Recommended order:

1. **Settings** — confirm Ollama URL `http://127.0.0.1:11434`, models, leave **dry-run ON**.
2. **Accounts** — add each Gmail; set **Owner key** (`usama` / `faizan`); optional default CV.
3. **Profile** (`/profile`) — one profile per owner key: name, allowed facts, forbidden phrases.
4. **CVs** — upload PDFs; set the same owner key; add a short description (or LLM-suggest).
5. **Contacts** — import professors (see §4).
6. **Templates** — for China list use **China / CSC enquiry**.
7. **Campaigns** — pick template, owner filter, contact group (`usama_batch` / `faizan_batch`).
8. **Drafts** — generate, fix flags, approve.
9. **Queue** — with dry-run ON, inspect `data/outbox_dryrun/*.eml` before turning dry-run off.

---

## 4. Professors CSV — why options looked “missing”

File: `professors_complete_verified.csv` (verified China botany / forestry faculty).

### Source columns

`full_name`, `university_en`, `university_cn`, `department`, `professor_name_cn`, `professor_name_en`, `title`, `email`, `email_source_url`, `research_direction`, `verification_status`, `notes`

### Quality (377 rows in the verified export)

| Issue | Count | Effect in MailPilot |
|-------|------:|---------------------|
| Duplicate / invalid emails | 0 | All rows importable |
| Missing English name | ~105 | Display falls back to CN/`full_name`; tagged `needs_english_name` |
| Title = “not stated” | ~108 | Cleared → default **Professor** |
| Research = “not stated” | ~46 | Cleared; tagged `needs_research` |
| **No `country` column** | all | Old UI left country/timezone blank and region defaulted to western |
| **No `groups` column** | all | Groups stayed empty until you typed them |

Those last two are why **country / region / groups** seemed absent on the map screen — the CSV simply did not have those fields. MailPilot now **detects** this schema on upload and fills them.

### Auto-normalize (Contacts → Upload)

When headers look like a China professors export, import:

- Prefers `professor_name_en` for `full_name` (CN kept in notes)
- Sets `country=China`, `timezone=Asia/Shanghai`, `region_type=china`
- Maps `university_en` → institution, `research_direction` → research_summary, `email_source_url` → profile_url
- Strips `not stated` / N/A placeholders
- Assigns groups: `professor`, `china`, `botany`, plus alternating `usama_batch` / `faizan_batch`
- Adds `needs_english_name` / `needs_research` when data is thin
- Shows a QA panel on the map page (counts + batch sizes)

Leave **Assign groups** empty on that screen so per-row batch tags are kept. Region defaults to **china**.

### Optional CLI prep

```bash
python scripts/prepare_professors_csv.py professors_complete_verified.csv -o samples/professors_mailpilot_ready.csv
```

Writes a MailPilot-ready CSV + `.qa.json`. You can import either the raw verified file (auto-normalize) or the prepared file.

### Campaign filters after import

- Whole China list: group `china` or `professor`
- Usama’s half: `usama_batch`
- Faizan’s half: `faizan_batch`
- Thin research: `needs_research` (enrich before drafting, or accept shorter personalization)

---

## 5. Multi-sender (6 accounts, two people)

1. Tag each mailbox **Owner key** = `usama` or `faizan`.
2. Tag each CV with the same owner key.
3. Save **Profile** per owner key (facts the LLM may claim).
4. New campaign: set **Owner filter** (auto-picks that person’s accounts) **or** paste all account IDs.
5. Generation assigns contacts by remaining daily capacity; sticky account on follow-ups; CV picker only sees that owner’s PDFs.

---

## 6. How the local LLM is used

| Job | Model setting | Purpose |
|-----|---------------|---------|
| Draft generation | `drafting_model` | Subject/body JSON; China region → shorter English + CSC if template says so |
| CV description / rerank | drafting + `embedding_model` | Suggest CV blurb; pick best CV for research text |
| Workflow coach | `drafting_model` | `/guide` — next steps from live checklist + playbook |

All calls go through `app/llm/ollama_client.py` → `127.0.0.1:11434`. If Ollama is down, the Guide still shows **heuristic** next steps.

Draft validators (flags block blind bulk-approve): forbidden phrases, length, invented references, hallucinated quotes, name/institution mismatch, placeholders, language (CJK ratio).

---

## 7. Safety

- **Dry-run ON** until you turn it off in Settings. Outbound → `data/outbox_dryrun/`.
- **Pause all sending** kill switch in the sidebar.
- Secrets: Fernet key in OS keyring (fallback `data/.fernet_key` / `MAILPILOT_FERNET_KEY`).
- Bind address: `127.0.0.1` only.
- Logs: `data/logs/mailpilot.log` (rotating).

---

## 8. Page map

| Path | Role |
|------|------|
| `/` | Dashboard — accounts, campaign funnel, Ollama health |
| `/guide` | Checklist + ask local LLM coach |
| `/docs/user-guide` | This guide rendered in the app |
| `/accounts` | Gmail accounts, owner keys, caps |
| `/inbox` | Synced mail, filters |
| `/contacts` | List + CSV import wizard |
| `/cvs` | PDF library |
| `/templates`, `/profile` | Templates + user facts |
| `/campaigns` | Wizard + generate |
| `/drafts` | Review / rewrite / approve |
| `/queue` | Scheduled sends |
| `/settings` | Models, dry-run, export/wipe |

---

## 9. Troubleshooting

| Symptom | Fix |
|---------|-----|
| Browser error -102 / connection refused | App not running — use `run.bat`; check nothing else bound to 8765 |
| Startup `SessionLocal` assert | Pull latest branch (fixed); restart `run.bat` |
| Ollama 404 on draft/CV | `ollama pull <model>`; match Settings name to `ollama list` |
| Import region still western | Use raw China professors CSV (auto-detect) or set Region = china on map page |
| Empty groups after import | Don’t overwrite groups on map if CSV already has them; re-import with empty Assign groups |
| Wrong person’s CV attached | Set matching `owner_key` on account + CV + profile |
| Clone failed under System32 | Clone to `Documents` instead |

---

## 10. Tests & layout

```bash
pytest -q
bash scripts/check.sh   # ruff + pytest
```

```
app/           FastAPI routes, mail/, llm/, importers/, scheduling/
samples/       Example MailPilot-ready professors CSV (+ QA JSON)
scripts/       prepare_professors_csv.py, check.sh
tests/
USER_GUIDE.md  This file
EMAIL_HUB_SPEC.md
```
