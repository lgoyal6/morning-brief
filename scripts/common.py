"""Shared helpers for the morning-brief pipeline.

Every script is run as ``python scripts/<name>.py`` from the repo root, so the
``scripts/`` directory is on ``sys.path`` and ``import common`` works.

The three stages (generate -> render -> send) coordinate through a small state
file (``briefs/.brief_state.json``) written by ``generate_brief.py``. Each stage
also falls back to recomputing today's date, so the pipeline still works if the
state file is missing (e.g. running a single stage by hand).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytz

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIEFS_DIR = REPO_ROOT / "briefs"
# Per-run scratch state (gitignored, ephemeral): how the three stages of a
# single run coordinate.
STATE_PATH = BRIEFS_DIR / ".brief_state.json"
# Cross-run delivery marker (COMMITTED, persists between runs): records the last
# date a *scheduled* run claimed the morning-delivery slot. This is what lets the
# two daily DST crons dedup without a manual/local run being able to suppress the
# morning send. Unlike STATE_PATH it must be committed, so it is not gitignored.
DELIVERY_PATH = BRIEFS_DIR / ".delivery.json"

# Timezone used to decide "today". Overridable for testing.
TZ_NAME = os.environ.get("BRIEF_TZ", "America/Los_Angeles")

# Filename slug shared by every generated artifact.
BRIEF_SLUG = "ai-tech-market-brief"


def la_now() -> datetime:
    """Current time in the brief's timezone (default America/Los_Angeles)."""
    return datetime.now(pytz.timezone(TZ_NAME))


def today_str() -> str:
    """Today's date as YYYY-MM-DD in the brief's timezone."""
    return la_now().strftime("%Y-%m-%d")


def md_path(date: str | None = None) -> Path:
    return BRIEFS_DIR / f"{date or today_str()}-{BRIEF_SLUG}.md"


def dated_pdf_path(date: str | None = None) -> Path:
    return BRIEFS_DIR / f"{date or today_str()}-{BRIEF_SLUG}.pdf"


def latest_pdf_path() -> Path:
    return BRIEFS_DIR / f"latest-{BRIEF_SLUG}.pdf"


def sources_path(date: str | None = None) -> Path:
    return BRIEFS_DIR / f"{date or today_str()}-sources.json"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def save_state(**updates) -> dict:
    """Merge ``updates`` into the on-disk state file and return the result."""
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    state.update(updates)
    STATE_PATH.write_text(json.dumps(state, indent=2))
    return state


def scheduled_delivered_date() -> str | None:
    """Date (YYYY-MM-DD) a scheduled run last claimed the morning-delivery slot.

    Read from the committed ``DELIVERY_PATH`` marker so it survives across the
    separate GitHub Actions runs (each run is a fresh checkout). Returns ``None``
    if no scheduled run has ever recorded a delivery.
    """
    if DELIVERY_PATH.exists():
        try:
            return json.loads(DELIVERY_PATH.read_text()).get("scheduled_date")
        except (ValueError, OSError):
            return None
    return None


def mark_scheduled_delivery(date: str) -> None:
    """Record that a scheduled run has claimed ``date``'s morning-delivery slot.

    Written to the committed ``DELIVERY_PATH`` so the day's second DST cron (and
    any re-run) sees it and skips, while manual/local runs never touch it.
    """
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    DELIVERY_PATH.write_text(json.dumps({"scheduled_date": date}, indent=2) + "\n")


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean-ish environment variable."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")
