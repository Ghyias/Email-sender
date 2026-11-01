# MailPilot — Local Multi-Gmail Client + Local-LLM Outreach System

**Build spec for Cursor. Read this whole file before writing code. Build in the phases listed in `CURSOR_PHASE_PROMPTS.md`, one phase per session.**

---

## 0. One-paragraph summary

A local-only desktop web app (runs on `http://127.0.0.1:8765`, Windows first) that connects to 1–10 Gmail accounts using **app passwords over IMAP/SMTP**, syncs their mail into a local SQLite database, and gives one unified inbox with rich filtering (date range, sender, sender group such as "professors", replied / not replied, account, label, has-attachment, full-text). It imports contact lists from CSV / XLSX / TXT, uses a **small local LLM via Ollama** only for two jobs — (1) writing personalised email drafts and (2) choosing which CV variant to attach — and then sends those drafts in **rate-limited bulk** or **at scheduled times**, with the chosen CV attached, always after human approval. No cloud APIs. No data leaves the machine except the emails themselves going to Gmail.

---

## 1. Hard constraints (non-negotiable)

1. **Local only.** Bind the server to `127.0.0.1`, never `0.0.0.0`. No telemetry, no external API calls except `imap.gmail.com`, `smtp.gmail.com`, and `localhost:11434` (Ollama).
2. **LLM scope is narrow.** The LLM is used ONLY for: draft generation, draft rewriting on request, and CV-variant selection. It never sends mail, never decides recipients, never runs without the user pressing a button or an approved campaign rule.
3. **Nothing is sent without approval.** Every draft passes through `draft → approved → queued → sent/failed`. Bulk "approve all" is allowed but must be an explicit user action with a confirmation dialog showing the count, accounts, and schedule.
4. **App passwords are never stored in plain text.** Encrypt with Fernet; keep the Fernet key in the OS keyring (Windows Credential Manager via `keyring`). Never log them, never return them to the frontend.
5. **Respect Gmail limits** (see §7). Conservative defaults; hard ceilings the user cannot exceed in the UI.
6. **Do not build deliverability-evasion features**: no content spinning, no hidden text, no tracking pixels, no header spoofing, no rotating accounts to bypass a single account's limit for the same campaign beyond what the user configures per account.
7. **Never mark mail as read during sync** — use `BODY.PEEK[]`.

---

## 2. Verified external facts the design depends on (checked Nov 2026)

| Fact | Consequence for design |
|---|---|
| Google still issues **app passwords** for accounts with 2-Step Verification; they are unavailable if the account is enrolled in Advanced Protection; all app passwords are **revoked when the main Google password changes**. | Onboarding wizard must explain 2SV + app password creation; app must detect `AUTHENTICATIONFAILED` and show "app password revoked — re-enter" instead of retry-looping. |
| Plain-password ("less secure apps") access is gone. | Only app passwords. (OAuth is a possible later upgrade — out of scope.) |
| IMAP: `imap.gmail.com:993` SSL. SMTP: `smtp.gmail.com:465` SSL (or 587 STARTTLS). | Use 465 SSL by default. |
| Personal Gmail: about **500 recipients/day**, counted across To+Cc+Bcc on a rolling 24 h window; SMTP caps **100 recipients per message**; 25 MB message size incl. attachments. Exceeding can block sending for up to 24 h. | Default cap 30/account/day, hard ceiling 150. One recipient per message for outreach (never Bcc-blast). Reject attachments > 20 MB. |
| Gmail's SMTP server automatically saves a copy of sent mail to **Sent**. | Do NOT IMAP-APPEND sent messages to Sent (would duplicate). |
| Gmail IMAP supports extensions **X-GM-MSGID, X-GM-THRID, X-GM-LABELS, X-GM-RAW** (Gmail search syntax over IMAP). | Use `[Gmail]/All Mail` as the single sync source; dedupe by X-GM-MSGID; threads by X-GM-THRID; server-side search via X-GM-RAW. |
| Gmail folder names are **localised** (e.g. "[Gmail]/已发邮件"). | Find folders by SPECIAL-USE flags (`\All`, `\Sent`, `\Drafts`, `\Trash`), never by hard-coded name. |
| Ollama serves a local API on `localhost:11434`, supports **structured outputs** (`format` = JSON schema) and a `think` flag for reasoning models. | All LLM calls use JSON-schema output and `think: false` for speed. |

---

## 3. Tech stack (chosen to minimise code volume for Cursor)

Single Python process, server-rendered UI. No separate JS build step.

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | One language end-to-end |
| Web | FastAPI + Uvicorn | Async, typed, simple |
| UI | Jinja2 templates + **HTMX** + Tailwind (CDN) + Alpine.js (tiny interactions) | No React/Node toolchain → far fewer tokens |
| DB | SQLite (WAL mode) via SQLAlchemy 2.0 + Alembic | Local, zero-admin; FTS5 for search |
| IMAP | `IMAPClient` | Native support for Gmail X-GM-* extensions |
| SMTP | stdlib `smtplib` + `email.message.EmailMessage` | Enough |
| Scheduler | APScheduler 3.x with `SQLAlchemyJobStore` (same SQLite) | Jobs survive restart |
| Import | `pandas` + `openpyxl` | CSV/XLSX/TXT |
| Secrets | `cryptography` (Fernet) + `keyring` | Encrypted app passwords |
| LLM | Ollama HTTP API via `httpx` | Local, swappable models |
| Embeddings | Ollama `/api/embed` | CV matching |
| HTML safety | `nh3` (or `bleach`) to sanitise received HTML; render in sandboxed `<iframe srcdoc>` | Received mail is untrusted |
| Validation | `pydantic` v2, `email-validator` | |
| Tests | `pytest`, `pytest-asyncio`; fake IMAP/SMTP via `aiosmtpd` for SMTP | |
| Optional desktop wrapper | `pywebview` | Opens the app in a native window |

`requirements.txt` must pin versions.

---

## 4. Folder layout

```
mailpilot/
  app/
    main.py                 # FastAPI app, startup/shutdown, scheduler boot
    config.py               # pydantic Settings, paths, defaults
    db.py                   # engine, session, WAL pragma, FTS5 setup
    models.py               # SQLAlchemy models (§5)
    security.py             # Fernet + keyring helpers
    mail/
      imap_sync.py          # per-account sync worker
      smtp_send.py          # build + send EmailMessage
      parse.py              # MIME parsing, address normalisation, snippet
      threads.py            # reply detection
    llm/
      ollama_client.py      # chat + embed, retries, timeouts, health check
      prompts.py            # all prompt templates in one place
      drafting.py           # draft generation + validators
      cv_matcher.py         # embedding + LLM rerank
    importers/
      tabular.py            # CSV/XLSX/TXT → normalized contacts
    scheduling/
      jobs.py               # send jobs, sync jobs, misfire handling
      limits.py             # per-account rate limiting
    routes/                 # one router per page
      inbox.py accounts.py contacts.py templates.py cvs.py
      campaigns.py drafts.py queue.py settings.py
    templates/              # Jinja2 HTML (+ partials for HTMX)
    static/
  data/                     # created at runtime; git-ignored
    mailpilot.db
    cvs/                    # CV PDFs (user copies or points to existing folder)
    attachments_cache/
    logs/
  tests/
  alembic/
  requirements.txt
  run.bat                   # starts Ollama check + uvicorn + opens browser
  README.md
```

---

## 5. Data model (SQLite)

All timestamps stored as **UTC ISO-8601**; convert for display using the user's timezone setting (default `Asia/Shanghai`).

**accounts** — `id, email (unique), display_name, enc_app_password, daily_cap (default 30), min_delay_s (default 90), max_delay_s (default 240), signature_html, active, last_sync_at, all_mail_uidvalidity, all_mail_last_uid, sync_window_days (default 365), status (ok|auth_failed|error), status_msg`

**messages** — `id, account_id, gm_msgid, gm_thrid, uid, message_id_hdr, in_reply_to, references_hdr, from_addr, from_name, to_addrs (json), cc_addrs (json), subject, date_utc, snippet, body_text, body_html (nullable, lazy), labels (json), has_attachments, is_unread, is_from_me, size_bytes`
- Unique `(account_id, gm_msgid)`.
- Index on `date_utc`, `from_addr`, `gm_thrid`, `message_id_hdr`.
- **messages_fts** (FTS5): `subject, body_text, from_name, from_addr`, content-linked to messages.

**contacts** — `id, email (unique, lowercased), full_name, title, institution, department, country, timezone (IANA, nullable), research_summary, recent_papers (text), profile_url, groups (json, e.g. ["professor","china"]), region_type (china|western|other), source_file, do_not_contact, bounced, notes, created_at`

**templates** — `id, name, language (en|zh), region_type, subject_template, body_template, llm_instructions, forbidden_phrases (json), max_words`
Templates use Jinja placeholders: `{{ contact.full_name }}`, `{{ contact.institution }}`, `{{ llm.personal_paragraph }}`, etc.

**cv_variants** — `id, key (unique, e.g. "ml"), file_path, label, description (1–3 sentences: what this CV emphasises), keywords (json), embedding (blob), embedding_model, updated_at`

**user_profile** — single row: `full_name, short_bio, research_interests, signature_default, facts_allowed (json list of true statements the LLM may use), facts_forbidden (json, e.g. ["GPA","CGPA"])`

**campaigns** — `id, name, template_id, account_ids (json), contact_filter (json), followup_days (nullable), send_window (json: {"mode":"recipient_local","start":"09:00","end":"11:30"}), created_at, status`

**drafts** — `id, campaign_id, contact_id, account_id, subject, body_text, body_html, cv_variant_id, cv_reason, cv_confidence, cv_needs_review, llm_model, prompt_hash, validation_flags (json), status (draft|approved|queued|sent|failed|cancelled), is_followup, parent_draft_id, created_at, updated_at`

**send_queue** — `id, draft_id, account_id, scheduled_utc, status (pending|sending|sent|failed|missed|cancelled), attempts, last_error, smtp_message_id, sent_utc`

**send_log** — append-only audit: `id, ts_utc, account_id, to_addr, subject, draft_id, result, error`

**settings** — key/value JSON.

---

## 6. Feature specs

### 6.1 Accounts (onboarding)
- "Add account" form: email, display name, app password (16 chars, spaces stripped), daily cap.
- On save: test IMAP login AND SMTP login (`ehlo` + `login`, no send). Show precise error:
  - `AUTHENTICATIONFAILED` → "Wrong or revoked app password. Check 2-Step Verification is on and generate a new one."
  - IMAP disabled → link text explaining Settings → Forwarding and POP/IMAP.
- Encrypt password, store. Kick off initial sync.
- Help panel with step-by-step: enable 2SV → myaccount.google.com/apppasswords → name it "MailPilot" → paste.
- Per-account toggle: active / paused. Edit cap and delay. "Re-enter app password".
- Max 10 accounts (configurable constant).

### 6.2 Sync engine
- One background worker per active account (thread or asyncio task wrapping IMAPClient), serialised DB writes via a single writer queue.
- Locate `\All` folder by SPECIAL-USE. Select read-only.
- **Initial sync:** `SEARCH SINCE <today - sync_window_days>`; fetch in batches of 200: `X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.20000>` (cap text). Parse, store snippet (first 200 chars) and body_text; store HTML only when a message is opened (lazy fetch by UID).
- **Incremental:** if UIDVALIDITY unchanged, fetch `UID > all_mail_last_uid`; also refresh FLAGS/labels for last 500 UIDs to catch read/unread/label changes. If UIDVALIDITY changed → full resync of the window.
- Use IMAP IDLE optional (phase 8); default polling every 5 min (configurable).
- `is_from_me` = from_addr equals any configured account address.
- Progress shown in UI (HTMX polling a `/sync/status` partial).
- Robust: reconnect with exponential backoff; one failing account must not stop others.

### 6.3 Unified inbox & filters
List view (virtualised paging, 50/page) + reading pane.
Filters (combinable, persisted in URL query string so they can be bookmarked):
- Account(s)
- Date range (from/to, presets: today, 7d, 30d, custom)
- Sender: exact address, domain (e.g. `*.edu`, `*.ac.uk`, `*.edu.cn`), or **contact group** (e.g. "professor") — joins `messages.from_addr` to `contacts.email`
- Direction: received / sent by me
- **Reply status** (see 6.4): "replies to my outreach", "outreach awaiting reply", "awaiting reply > N days"
- Unread only, has attachments, Gmail label
- Full-text query (FTS5) — plus an "advanced: run Gmail search on server" box that uses X-GM-RAW for a single account
- Sort: date desc/asc, sender
Reading pane: headers, sanitized HTML in sandboxed iframe (`sandbox` without `allow-scripts`), **remote images blocked by default** with "load images" button, attachments listed with download, thread view by gm_thrid.
Actions on a message: mark read/unread (IMAP STORE flags — the only write to IMAP besides optional label/archive), archive, star, "reply" (opens composer with In-Reply-To/References set), "create contact from sender".

### 6.4 Reply detection
- Build a set of `smtp_message_id`s from `send_queue` (sent by the app) plus `message_id_hdr` of all `is_from_me` messages.
- A received message is a **reply** if its `In-Reply-To` or any id in `References` is in that set, OR it shares `gm_thrid` with an app-sent message.
- Mark the corresponding draft's contact as `replied` (derived view or column `last_reply_utc` on contacts).
- Bounce detection: from `mailer-daemon@googlemail.com` / subject "Delivery Status Notification" whose body references a sent Message-ID → set `contacts.bounced = true`, cancel future follow-ups.

### 6.5 Contact import (CSV / XLSX / TXT)
- Upload file → preview first 20 rows → **column-mapping UI** (auto-guess by header names: email/e-mail/mail, name, title, university/institution, country, research/interests/summary, papers, url/homepage, group).
- TXT: one email per line, or `name <email>`, or tab-separated — auto-detect.
- XLSX: choose sheet.
- Normalise: lowercase/strip emails, validate with `email-validator` (syntax only, no network check), dedupe within file and against DB (options: skip / update empty fields / overwrite).
- Assign groups and region_type on import (e.g. a dropdown "these contacts are: professor, china").
- Import report: imported, updated, skipped-duplicate, invalid (downloadable CSV of rejects).
- Optional: timezone inferred from country (map country → most common IANA tz; user can edit).

### 6.6 CV library
- Point at a folder (e.g. existing `D:\Email Sender\data\cvs\`) or upload PDFs.
- For each PDF: key (from filename), label, **description written by the user** (what this CV emphasises), keywords. Optionally "auto-suggest description" → extract text with `pypdf`, ask LLM to summarise in 2 sentences; user must confirm.
- Compute embedding of `label + description + keywords` with the embedding model; recompute when text or embedding model changes.
- Validate: file exists, is PDF, size < 5 MB (warn), < 20 MB (block).

### 6.7 Templates & user profile
- Template editor with live preview against a chosen contact.
- Two default templates seeded: "Western PhD enquiry (EN)" and "China / CSC enquiry (EN, simpler wording)". The user edits them.
- User profile page: bio, interests, **allowed facts list** (the only claims about the user the LLM may make), **forbidden phrases list** (default includes `GPA`, `CGPA`, `grade point`), signature.

### 6.8 Draft generation (LLM job #1)
Input to the LLM (and nothing else): template.llm_instructions, user_profile allowed facts, contact fields (name, title, institution, research_summary, recent_papers), region_type, language, max_words.
Output (JSON schema enforced via Ollama `format`):
```json
{"subject": "string", "body": "string", "referenced_items": ["string"]}
```
`referenced_items` = the specific research topics/paper titles from the contact data that the draft mentions.

**Post-generation validators** (each produces a flag; any flag → draft shows a warning badge and cannot be bulk-approved without ticking "I reviewed flagged drafts"):
1. Forbidden phrase present (case-insensitive regex).
2. Word count > max_words × 1.15 or < 60.
3. Each `referenced_items` entry must appear (fuzzy ≥ 0.85, rapidfuzz) in research_summary or recent_papers — **catches invented paper titles**.
4. Any quoted title in body ("…" or “…”) not found in contact data → hallucination flag.
5. Contact name / institution in body must match the contact row.
6. Contains placeholder leftovers like `[Name]`, `{{`, `<insert`.
7. Language check: if template language is `en`, ratio of CJK characters < 2% (and vice versa).
On a hard failure (1, 6) auto-retry once with the violation stated in the prompt; if still failing, keep the draft with flags.

Generation runs as a background batch with a progress bar; concurrency 1 (local model), queue persists across restarts. User can stop/resume.

Draft review screen: table of drafts with contact, account, subject, CV chosen (+ confidence), flags; click → side-by-side: contact data | editable draft | CV reason. Buttons: regenerate, "make shorter", "more formal", change CV, approve, reject.

### 6.9 CV selection (LLM job #2) — hybrid, explainable
1. **Rule override first:** if contact has a group/keyword mapping set by the user (e.g. `medical imaging → medical`), use it, confidence 1.0, reason "rule".
2. **Embedding retrieval:** embed `research_summary + recent_papers` with the embedding model; cosine similarity vs each cv_variant embedding; keep top 3 with scores.
3. **LLM rerank:** give the LLM the contact research text and ONLY the 3 candidate CVs (key + description). JSON schema:
```json
{"cv_key": "one of the 3 keys", "reason": "<= 40 words", "confidence": 0.0}
```
4. **Disagreement / uncertainty check:** set `cv_needs_review = true` if LLM pick ≠ embedding top-1 AND top-1 margin over the pick > 0.05, or confidence < 0.6, or research text is shorter than 30 words (too little evidence) → fall back to the `general` CV and flag.
5. Store reason + scores so the user sees *why*.

**Evaluation harness (important):** a page "CV matcher accuracy" where the user labels 20–30 contacts with the correct CV once; the app then reports accuracy of (a) embedding-only, (b) LLM-only, (c) hybrid, per model. This lets the user choose models from evidence instead of guesswork.

### 6.10 Sending — bulk & scheduled
- **Composer** (single email): from-account, to, subject, body, attach CV from library or any file, send now / schedule.
- **Campaign flow:** choose template → choose contacts (by filter: group, imported file, not-yet-contacted, not do_not_contact, not bounced, not replied) → choose sending accounts → generate drafts → review/approve → schedule.
- **Account assignment:** each contact gets one sending account (round-robin weighted by remaining daily capacity). Once a contact has been emailed from an account, follow-ups always use the **same account and thread**.
- **Scheduling modes:**
  - "Send now, spread out": queue with random delay in `[min_delay_s, max_delay_s]` per account.
  - "At a specific date/time" (user timezone).
  - "Recipient local window": e.g. 09:00–11:30 in the contact's timezone on the next weekday; jittered within the window.
- **Limits enforced at dequeue time** (not just at scheduling): count `send_log` successes in the last rolling 24 h per account; if at cap, push job to the next window and mark it "deferred (cap)". Global safety: max 1 message per account per `min_delay_s`.
- **Message building:** `EmailMessage`, From = `"Display Name" <account>`, plain-text part + simple HTML part (same content, `<p>` paragraphs), signature, attachment as `application/pdf` with clean filename (e.g. `Ghyias_Mir_CV.pdf` — configurable pattern, NOT the internal key), `Message-ID` generated with `email.utils.make_msgid(domain="gmail.com")` and stored before sending, `Date` header. For follow-ups set `In-Reply-To` and `References` and prefix subject with `Re: ` exactly once.
- **Send:** `smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60)`; login; `send_message`. On success write send_log + mark draft sent. On `SMTPAuthenticationError` → pause that account, mark status auth_failed, notify. On `SMTPDataError 550/5.4.5` (daily limit) → pause account 24 h. Transient (4xx, timeouts) → retry ×3 with backoff, then failed.
- **Misfire handling:** scheduled jobs only fire while the app/PC is running. On startup, for overdue jobs: if overdue < 2 h → send; else mark `missed` and show a banner "N scheduled emails were missed — reschedule?" (never silently blast a backlog).
- **Kill switch:** big "Pause all sending" button in header; also per campaign.
- **Dry-run mode** (settings): everything works but SMTP is replaced by writing `.eml` files into `data/outbox_dryrun/`. Default ON until the user turns it off — first-run safety.

### 6.11 Follow-ups (optional per campaign)
If `followup_days` set: after N days with no reply/bounce, auto-create a follow-up **draft** (status draft, needs approval), short, same thread.

### 6.12 Settings
Timezone; sync interval; sync window; Ollama URL; drafting model; embedding model; temperature (default 0.4); dry-run; default delays/caps; attachment filename pattern; data folder location; "export everything" (contacts, drafts, send_log → XLSX); "wipe local mail cache".

### 6.13 Dashboard
Per account: synced count, last sync, sent today / cap, status. Campaigns: drafted / approved / sent / replied / bounced, reply rate. Ollama health (model loaded? tokens/s from last call).

---

## 7. Sending limits — defaults and ceilings

| Setting | Default | Hard ceiling in UI | Rationale |
|---|---|---|---|
| Messages per account per rolling 24 h | 30 | 150 | Google's ~500/day is an upper bound; cold outreach well below it protects account reputation |
| Recipients per message | 1 | 1 for campaigns | Personalised 1:1 mail; never Bcc blasts |
| Delay between sends (same account) | 90–240 s random | min 30 s | Human-like pacing |
| Attachment size | warn > 5 MB | block > 20 MB | 25 MB Gmail cap incl. encoding overhead |
| New account warm-up (optional) | day1 10, +5/day to cap | — | Fresh accounts are fragile |

---

## 8. LLM integration details

### 8.1 Models (user-selectable in Settings; defaults below)
- Drafting: `qwen3.5:4b` (8 GB RAM machines) — upgrade to `qwen3.5:9b` or `gemma4:12b` when 16 GB RAM / ≥8 GB VRAM.
- Embeddings: `qwen3-embedding:0.6b` (multilingual, long context). Alternative: `nomic-embed-text` (smaller, English-centric).

### 8.2 Ollama call conventions
- `POST /api/chat` with `{"model", "messages", "format": <json schema>, "stream": false, "think": false, "options": {"temperature": 0.4, "num_ctx": 8192}, "keep_alive": "10m"}`.
- `POST /api/embed` with `{"model", "input": [list]}` — batch inputs.
- Timeout 120 s; 2 retries; if `think` is rejected by a non-thinking model, retry without the field.
- Health check on startup: `GET /api/tags`; if the configured model is missing, show the exact `ollama pull <model>` command in the UI.
- Log prompt hash, model, latency, token counts (from response `eval_count`) — never log app passwords; contact data in logs only at DEBUG.

### 8.3 Prompt rules (in `llm/prompts.py`)
System prompt for drafting must state:
- Write as the user, first person, plain professional tone, no flattery clichés ("I am thrilled", "esteemed", "groundbreaking").
- Use ONLY facts from ALLOWED_FACTS about the user. Never invent achievements, grades, publications.
- Mention 1–2 specific items from the professor's research data; do not invent paper titles.
- Never mention items in FORBIDDEN.
- Respect max_words; one clear ask; say the CV is attached.
- For region_type=china: shorter sentences, simple vocabulary, mention CSC scholarship plan if template says so.
- Output JSON only.
Provide two few-shot examples (one western, one china) in the prompt file, editable by the user.

---

## 9. Security & privacy checklist
- Server bound to 127.0.0.1; add a random session token cookie set on first load to block other local processes' CSRF; CSRF token on POST forms.
- Fernet key in keyring; DB contains only ciphertext.
- Sanitize received HTML (nh3), render in sandboxed iframe, block remote images by default (they leak IP/open-tracking).
- Attachments saved with sanitized filenames into `attachments_cache/`, never auto-opened.
- `.gitignore` covers `data/`, `*.db`, `.env`.
- Logs rotate (5 × 5 MB).

---

## 10. Testing requirements
- Unit: importers (CSV/XLSX/TXT edge cases: BOM, semicolon delimiter, merged header rows, duplicate emails, invalid emails), MIME parsing (multipart, encoded headers, Chinese subjects), reply detection, rate limiter (rolling window), validators (each flag), CV matcher decision logic (with stubbed embeddings).
- Integration: local SMTP server via `aiosmtpd` to assert headers, attachment, threading headers; IMAP sync against a fixture mailbox (mock IMAPClient responses).
- LLM tests use a fake Ollama client returning canned JSON — tests must not require Ollama running.
- A `make check` / `scripts/check.bat` running ruff + pytest.

---

## 11. Non-goals (do not build)
OAuth, mobile app, multi-user, cloud sync, open/click tracking, email warm-up networks, scraping professor pages (contacts come from the user's files), automatic sending without approval.

---

## 12. Definition of done
1. Add 2 real accounts → both sync last 365 days without marking anything read.
2. Filter: "replies from contacts in group professor, last 30 days" returns correct results.
3. Import a 200-row XLSX with messy headers → mapping works, report shows rejects.
4. Generate 20 drafts with `qwen3.5:4b` → each has subject, body, CV + reason; validators flag a deliberately planted "GPA" in a template instruction.
5. Approve 10, schedule in recipient-local 09:00–11:30 window → in dry-run, 10 `.eml` files appear with correct headers and PDF attached; no account exceeds cap.
6. Turn dry-run off, send 1 test email to yourself → arrives with attachment, appears once in Sent.
7. Restart app with pending jobs → jobs resume; overdue > 2 h marked missed.
8. `pytest` green.
