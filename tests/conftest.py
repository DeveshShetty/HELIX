"""Test setup: every service gets its own throw-away SQLite file, and the broker
address points at a closed port, so background consumers and relays fail fast and
never touch a real RabbitMQ. Run with `make test` (Docker) or `pytest -q tests`.
"""
import importlib
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="helix-tests-"))
os.environ.update({
    "RABBIT_HOST": "127.0.0.1", "RABBIT_PORT": "1",      # nothing listens: no real broker
    "AI_BUDGET_DB": str(TMP / "ai_budget.db"),
    "LLM_MODE": "ollama",                               # summariser: exercise the AI-draft checks
    "LOG_LEVEL": "WARNING",
})

_loaded: dict[str, object] = {}


def load_service(name: str):
    """Import services.<name>.main once, with its own database file."""
    if name not in _loaded:
        os.environ["DB_PATH"] = str(TMP / f"{name}.db")
        _loaded[name] = importlib.import_module(f"services.{name}.main")
        logging.getLogger("pika").setLevel(logging.CRITICAL)   # expected: no broker in tests
    return _loaded[name]


def wait_until(cond, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met in time")
        time.sleep(0.1)
