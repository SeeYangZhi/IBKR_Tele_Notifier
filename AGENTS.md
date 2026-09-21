# DOX framework

- DOX is highly performant AGENTS.md hierarchy installed here
- Agent must follow DOX instructions across any edits

## Core Contract

- AGENTS.md files are binding work contracts for their subtrees
- Work products, source materials, instructions, records, assets, and durable docs must stay understandable from the nearest applicable AGENTS.md plus every parent AGENTS.md above it

## Read Before Editing

1. Read the root AGENTS.md
2. Identify every file or folder you expect to touch
3. Walk from the repository root to each target path
4. Read every AGENTS.md found along each route
5. If a parent AGENTS.md lists a child AGENTS.md whose scope contains the path, read that child and continue from there
6. Use the nearest AGENTS.md as the local contract and parent docs for repo-wide rules
7. If docs conflict, the closer doc controls local work details, but no child doc may weaken DOX

Do not rely on memory. Re-read the applicable DOX chain in the current session before editing.

## Update After Editing

Every meaningful change requires a DOX pass before the task is done.

Update the closest owning AGENTS.md when a change affects:

- purpose, scope, ownership, or responsibilities
- durable structure, contracts, workflows, or operating rules
- required inputs, outputs, permissions, constraints, side effects, or artifacts
- user preferences about behavior, communication, process, organization, or quality
- AGENTS.md creation, deletion, move, rename, or index contents

Update parent docs when parent-level structure, ownership, workflow, or child index changes. Update child docs when parent changes alter local rules. Remove stale or contradictory text immediately. Small edits that do not change behavior or contracts may leave docs unchanged, but the DOX pass still must happen.

## Hierarchy

- Root AGENTS.md is the DOX rail: project-wide instructions, global preferences, durable workflow rules, and the top-level Child DOX Index
- Child AGENTS.md files own domain-specific instructions and their own Child DOX Index
- Each parent explains what its direct children cover and what stays owned by the parent
- The closer a doc is to the work, the more specific and practical it must be

## Child Doc Shape

- Create a child AGENTS.md when a folder becomes a durable boundary with its own purpose, rules, responsibilities, workflow, materials, or quality standards
- Work Guidance must reflect the current standards of the project or user instructions; if there are no specific standards or instructions yet, leave it empty
- Verification must reflect an existing check; if no verification framework exists yet, leave it empty and update it when one exists

Default section order:
- Purpose
- Ownership
- Local Contracts
- Work Guidance
- Verification
- Child DOX Index

## Style

- Keep docs concise, current, and operational
- Document stable contracts, not diary entries
- Put broad rules in parent docs and concrete details in child docs
- Prefer direct bullets with explicit names
- Do not duplicate rules across many files unless each scope needs a local version
- Delete stale notes instead of explaining history
- Trim obvious statements, repeated rules, misplaced detail, and warnings for risks that no longer exist

## Closeout

1. Re-check changed paths against the DOX chain
2. Update nearest owning docs and any affected parents or children
3. Refresh every affected Child DOX Index
4. Remove stale or contradictory text
5. Run existing verification when relevant
6. Report any docs intentionally left unchanged and why

## User Preferences

When the user requests a durable behavior change, record it here or in the relevant child AGENTS.md

## Project: IBKR → Telegram Notifier

**Purpose:** Read-only service that watches an Interactive Brokers account and posts to a Telegram channel on every fill (open/close), plus a once-daily snapshot of all open positions. It MUST NEVER place or modify orders.

This is a public, open-source repository. Assume every change will be read by strangers running it against their own live brokerage accounts.

**Project-wide contracts (no child doc may weaken these):**

- **Read-only, permanently.** Add no order-placing code. Enforced at the IBC layer (`ReadOnlyApi=yes`) and by the application only ever reading. A PR that adds trading is out of scope by definition, not a judgement call.
- **Secrets never enter the repo.** Real credentials live only in operator-populated files on the host: `<project>/.env` and the IBC `config.ini` (both chmod 600). The tracked `deploy/config.ini` is a credential-free template and CI fails the build if `IbLoginId`/`IbPassword` are non-blank. Never print, log, echo, or commit a secret. The `httpx`/`httpcore` loggers are pinned to WARNING so the bot-token URL never reaches the logs — do not lower them.
- **No personal infrastructure in tracked files.** No usernames, hostnames, cloud project ids, or absolute home paths. Host coordinates belong in `deploy/deploy.env` (gitignored); host-specific paths are template placeholders rendered by `deploy/setup.sh`.
- **Stack:** `ib_async` (NOT ib_insync). Telegram via plain `httpx` POST (NOT python-telegram-bot). Dependencies managed with `uv`; `uv.lock` is committed.
- **Formatting logic stays pure.** `ibkr_report.py` takes an `IB` handle as an argument and performs no I/O and no environment reads, so it stays unit-testable. Do not reintroduce module-global state or import-time side effects there.
- **Tests and lint must pass** before a change is done: `uv run pytest` and `uv run ruff check .`.

**Domain rules that are easy to break** — each is documented with its rationale in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), which is the authoritative reference. Read it before touching fill handling, currency, or the 2FA/systemd behaviour:

- `realizedPNL == 0.0` means an **opening** trade, not a zero-profit close.
- `sweep_fills()` is not redundant with `commissionReportEvent` — the event does not re-fire for fills synced on reconnect.
- Never sum across currencies without `to_base()`; a missing FX rate must produce a *disclosed incomplete* total, never a silently wrong one.
- The disconnected-summary warning is the health signal. Do not replace it with a heartbeat.
- `ibc-gateway.service` uses `Restart=on-failure`, never `always`.

## Child DOX Index

- **`deploy/`** — [deploy/AGENTS.md](deploy/AGENTS.md): deployment & service infrastructure — systemd unit templates, IBC config, the installer, and the on-host deploy half.

Top-level files owned directly by this root doc:

- `ibkr_telegram_notifier.py` — the notifier service: connection loop (self-reconnecting), fill notifications, daily summary scheduling, ops alerting (`send_ops`, `_OpsLogForwarder`), execId persistence.
- `ibkr_ops_bot.py` — Telegram ops-control bot: long-polls the OPS bot for `/positions`, `/resume`, `/pause`, `/status`, `/help` and runs them via a narrowly scoped passwordless `sudo systemctl`. Auth: channel posts from `OPS_CHAT_ID`; DMs from `OPS_ALLOWED_USER_IDS`. Its own service so it works when the notifier/gateway are down.
- `ibkr_report.py` — pure message/report building shared by both services. Money formatting via `money`/`smoney`: `$` for USD, `<amount> <CUR>` otherwise.
- `tests/` — pytest suite over the report layer; fakes only, no IB connection or credentials needed.
- `pyproject.toml` / `uv.lock` — uv project (deps: ib_async, httpx, python-dotenv; dev: pytest, ruff) and its lockfile.
- `deploy.sh` — one-shot deploy: `./deploy.sh [prod|test|keep]` pushes code, `uv sync`, switches channel, restarts services (via `deploy/remote-deploy.sh` on the host). Reads `deploy/deploy.env`; supports `ssh` and `gcloud` transports. Preferred over manual scp/ssh.
- `env.example` — template for `.env`; documents every key.
- `README.md` — user-facing overview, configuration, and run/deploy instructions.
- `docs/ARCHITECTURE.md` — design rationale and the IBKR API gotchas. The most important doc in the repo.
- `docs/DEPLOYMENT.md` — the deployment runbook: host requirements (x86-64 only), the agent-vs-human split with reasons, checkpointed phases, verification, and the operating rules for an agent on the host. Read it before deploying or advising on deployment.
- `SECURITY.md` — threat model, credential handling, the ops-bot authorization model, the sudo grant.
- `CONTRIBUTING.md` — setup, checks, review expectations.
- `.github/workflows/ci.yml` — lint + tests on 3.11/3.12/3.13, shellcheck, and a guard asserting no credentials in `deploy/config.ini`.
