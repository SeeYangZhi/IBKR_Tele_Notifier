"""Test bootstrap.

`ibkr_telegram_notifier` reads its Telegram credentials at import time, so the
scheduling tests need placeholder values in the environment before that import
happens. ENV_FILE is pointed at a nonexistent path so a developer's real .env is
never picked up by the test run.
"""
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-1000000000000")
os.environ["ENV_FILE"] = "/nonexistent/.env.for-tests"
