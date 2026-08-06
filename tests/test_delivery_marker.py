#!/usr/bin/env python3
"""Tests for the delivery marker — the contract the whole schedule hangs on.

``briefs/.delivery.json`` is the only durable record of "today's brief actually
reached Discord". Five triggers race to deliver each morning (one Cloudflare
dispatch, four GitHub backstop crons) and the marker is what stops them from
sending five copies. A separate Cloudflare watchdog reads the same file to decide
whether to raise an alarm. So two independent failure modes live here:

  * marker read too eagerly -> a real miss is suppressed and no brief ever lands
  * marker not written / not seen -> duplicate sends, or a false 🚨 every morning

The marker is also the one thing in this repo read by two languages. Python
writes it (scripts/common.py) and TypeScript reads it (trigger/src/index.ts),
with no shared schema between them, so a rename on either side would break the
watchdog silently. There is a test for exactly that below.

Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import common  # noqa: E402

WORKER_SRC = REPO_ROOT / "trigger" / "src" / "index.ts"


class MarkerRoundTrip(unittest.TestCase):
    """common.py's own read/write behaviour, against a temp briefs dir."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = (common.BRIEFS_DIR, common.DELIVERY_PATH)
        common.BRIEFS_DIR = self.tmp
        common.DELIVERY_PATH = self.tmp / ".delivery.json"

    def tearDown(self) -> None:
        common.BRIEFS_DIR, common.DELIVERY_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self):
        common.mark_scheduled_delivery("2026-08-06")
        self.assertEqual(common.scheduled_delivered_date(), "2026-08-06")

    def test_absent_marker_reads_as_never_delivered(self):
        # Must be None, not a crash and not today: a missing marker means the
        # brief has not been sent, so the next cron should go ahead and send.
        self.assertIsNone(common.scheduled_delivered_date())

    def test_corrupt_marker_reads_as_never_delivered(self):
        # Failing open (returning today) would suppress the send entirely.
        common.DELIVERY_PATH.write_text("{ not json")
        self.assertIsNone(common.scheduled_delivered_date())

    def test_marker_without_the_key_reads_as_never_delivered(self):
        common.DELIVERY_PATH.write_text(json.dumps({"other": "2026-08-06"}))
        self.assertIsNone(common.scheduled_delivered_date())

    def test_marker_is_overwritten_not_appended(self):
        common.mark_scheduled_delivery("2026-08-05")
        common.mark_scheduled_delivery("2026-08-06")
        self.assertEqual(common.scheduled_delivered_date(), "2026-08-06")
        json.loads(common.DELIVERY_PATH.read_text())  # still a single valid doc

    def test_marker_is_committable_json(self):
        # It gets git-committed by the workflow, so it needs a trailing newline
        # or every write shows up as a no-newline-at-EOF diff.
        common.mark_scheduled_delivery("2026-08-06")
        raw = common.DELIVERY_PATH.read_text()
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(json.loads(raw), {"scheduled_date": "2026-08-06"})

    def test_creates_the_briefs_dir_if_absent(self):
        shutil.rmtree(self.tmp)
        common.mark_scheduled_delivery("2026-08-06")
        self.assertEqual(common.scheduled_delivered_date(), "2026-08-06")


class DateFormat(unittest.TestCase):
    def test_today_is_an_iso_date(self):
        self.assertRegex(common.today_str(), r"^\d{4}-\d{2}-\d{2}$")

    def test_today_follows_the_configured_timezone(self):
        # The brief's "today" is Pacific, not the runner's UTC. GitHub runners
        # are UTC, so an evening Pacific run would otherwise file under tomorrow.
        self.assertEqual(common.TZ_NAME, "America/Los_Angeles")
        self.assertEqual(common.la_now().tzinfo.zone, "America/Los_Angeles")


class CrossLanguageContract(unittest.TestCase):
    """Python writes the marker; the Cloudflare watchdog reads it. No shared
    schema enforces that, so pin the three things both sides must agree on."""

    def setUp(self) -> None:
        self.worker = WORKER_SRC.read_text()

    def test_worker_reads_the_key_python_writes(self):
        key = "scheduled_date"
        self.assertIn(key, json.dumps({"scheduled_date": "x"}))
        self.assertIn(
            key,
            self.worker,
            "trigger/src/index.ts must read the same key common.py writes",
        )

    def test_worker_points_at_the_path_python_writes(self):
        rel = common.DELIVERY_PATH.relative_to(common.REPO_ROOT).as_posix()
        wrangler = (REPO_ROOT / "trigger" / "wrangler.jsonc").read_text()
        self.assertIn(
            rel,
            wrangler,
            f"DELIVERY_MARKER_PATH in wrangler.jsonc must be {rel}",
        )

    def test_both_sides_use_the_same_timezone(self):
        wrangler = (REPO_ROOT / "trigger" / "wrangler.jsonc").read_text()
        self.assertIn(common.TZ_NAME, wrangler)

    def test_worker_and_workflow_agree_on_the_dispatch_event(self):
        # If these drift, the Cloudflare cron fires an event nothing listens for
        # and the brief silently falls back to the late GitHub crons.
        wrangler = (REPO_ROOT / "trigger" / "wrangler.jsonc").read_text()
        workflow = (REPO_ROOT / ".github" / "workflows" / "morning-brief.yml").read_text()
        event = re.search(r'"DISPATCH_EVENT_TYPE":\s*"([^"]+)"', wrangler).group(1)
        self.assertRegex(workflow, rf"types:\s*\[{re.escape(event)}\]")


class SkipDecision(unittest.TestCase):
    """The end-to-end behaviour behind 'a run that already delivered is green'.

    Runs the real generate_brief.py in a throwaway copy of scripts/ so the repo's
    own committed marker is never touched. The skip path returns before any
    network or LLM call, so this costs nothing.
    """

    def _run(self, marker: str | None, event: str, timeout: int = 60):
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(REPO_ROOT / "scripts", tmp / "scripts")
            (tmp / "briefs").mkdir()
            if marker is not None:
                (tmp / "briefs" / ".delivery.json").write_text(
                    json.dumps({"scheduled_date": marker}) + "\n"
                )
            env = {
                **os.environ,
                "GITHUB_EVENT_NAME": event,
                # Belt and braces: if the skip is wrongly not taken, fail fast on
                # the first outbound request instead of fetching real sources.
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "HTTP_PROXY": "http://127.0.0.1:1",
            }
            env.pop("FORCE_REGENERATE", None)
            return subprocess.run(
                [sys.executable, str(tmp / "scripts" / "generate_brief.py")],
                capture_output=True, text=True, env=env, timeout=timeout,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    SKIPPED = "already delivered — skipping"

    def test_scheduled_run_skips_and_succeeds_when_already_delivered(self):
        # The behaviour that keeps four redundant backstop crons from turning the
        # Actions tab red every morning: skipping is success, not failure.
        r = self._run(common.today_str(), "schedule")
        self.assertIn(self.SKIPPED, r.stdout)
        self.assertEqual(r.returncode, 0, f"skip must exit 0\nstderr:\n{r.stderr}")

    def test_repository_dispatch_skips_too(self):
        # The Cloudflare trigger is a delivery run like any cron, so it dedups.
        r = self._run(common.today_str(), "repository_dispatch")
        self.assertIn(self.SKIPPED, r.stdout)
        self.assertEqual(r.returncode, 0)

    def test_a_stale_marker_does_not_suppress_today(self):
        # The failure that costs a whole day's brief: yesterday's marker must
        # never be mistaken for today's.
        r = self._run("2026-01-01", "schedule")
        self.assertNotIn(self.SKIPPED, r.stdout)

    def test_a_missing_marker_does_not_suppress_today(self):
        r = self._run(None, "schedule")
        self.assertNotIn(self.SKIPPED, r.stdout)

    def test_manual_dispatch_is_never_suppressed(self):
        # A human pressing "Run workflow" is iterating and must always regenerate,
        # even on a day the brief already went out.
        r = self._run(common.today_str(), "workflow_dispatch")
        self.assertNotIn(self.SKIPPED, r.stdout)


if __name__ == "__main__":
    unittest.main()
