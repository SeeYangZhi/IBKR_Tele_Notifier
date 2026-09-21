# Contributing

Thanks for your interest. This is a small, focused project — issues and pull requests are welcome.

## Before you start

**Security issues do not belong in public issues.** See [SECURITY.md](SECURITY.md).

**The read-only contract is not negotiable.** This service reads an Interactive Brokers account and nothing more. Pull requests that add order placement, order modification, or cancellation will be declined regardless of quality — the guarantee that this code *cannot* trade is the reason people are willing to point it at a live account. Enhancements to what it *reports* are very welcome.

## Setup

```bash
git clone <your-fork>
cd ibkr-tele-notifier
uv sync --group dev
```

You do not need an IBKR account or Telegram bot to run the test suite — it uses fakes throughout.

## Checks

Run both before opening a PR:

```bash
uv run pytest
uv run ruff check .
```

CI runs the same two commands on Python 3.11, 3.12 and 3.13.

## Testing against a live account

If a change touches fill handling or the summary, please test it against a **paper account** or a test Telegram channel before proposing it:

```bash
cp env.example .env.test && chmod 600 .env.test   # test bot, test channel,
                                                  # distinct IB_CLIENT_ID + STATE_FILE
ENV_FILE=.env.test uv run python ibkr_telegram_notifier.py
```

Say in the PR what you exercised and how. "Tested against paper, opened and closed a position, both messages correct" is worth more than any amount of description.

## Code style

- Match the surrounding code. It is deliberately plain: standard library plus three dependencies, no framework, no class hierarchy where a function will do.
- **Comments explain *why*, not *what*.** Most of the comments in this codebase record a fact about IBKR's API that cost someone an afternoon. If you work around an IBKR quirk, write down the quirk — that comment is the most valuable line in the diff.
- Formatting logic belongs in `ibkr_report.py` and must stay free of I/O and environment reads, so it stays testable.
- Keep line length under 100.

## Things that need care

A few areas where the obvious change is wrong:

- **`realizedPNL == 0.0` means an *opening* trade**, not a zero-profit close, despite what the IBKR docs imply. `ibkr_report.build_fill_message` depends on this. There are tests; please keep them passing.
- **Never sum amounts across currencies without `to_base()`.** Portfolio fields are in the contract's currency; account tags are in the base currency. A missing FX rate must degrade to a disclosed, incomplete total — never a silently wrong one.
- **`sweep_fills()` is not redundant with `commissionReportEvent`.** The event does not re-fire for fills synced during a reconnect, so the sweep is what catches trades made while the gateway was down.
- **Do not lower the `httpx`/`httpcore` log level.** Their `INFO` output contains the bot token.

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) explains each of these in full.

## Deployment changes

`deploy/` acts on a real host. Templates (`*.tmpl`) are rendered by `deploy/setup.sh`; if you add a placeholder, add its substitution there too. Keep `deploy/setup.sh` idempotent — it is expected to be safe to re-run.

## License

By contributing you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).
