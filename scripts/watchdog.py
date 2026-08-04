#!/usr/bin/env python3
"""Alert if today's brief never got delivered.

Every other failure signal this repo has is conditional on a run existing. A
failed run turns the workflow red and emails you; a truncated brief now says so
in the message. But the worst failure mode is silent: GitHub drops the scheduled
run, nothing executes, and there is nothing to be red. That is what happened on
2026-08-03 (nothing sent) and 2026-08-04 (the 08:00 cron never fired).

So this checks the *outcome* rather than any run: is today's delivery marker
committed? It runs after the last brief cron has had time to finish. If the
marker is missing, it DMs an alert and exits non-zero so there is an email too.

Deliberately dumb: no GitHub API calls, no run inspection. The marker is the
single source of truth for "did the brief actually reach Discord", because
send_discord.py only writes it after Discord confirms.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

import common
from send_discord import resolve_channel_id, send_text

load_dotenv()


def main() -> int:
    today = common.today_str()
    delivered = common.scheduled_delivered_date()

    if delivered == today:
        print(f"Brief for {today} was delivered — nothing to alert.")
        return 0

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    target = os.environ.get("DISCORD_TARGET", "").strip()
    now = common.la_now().strftime("%H:%M %Z")
    runs_url = os.environ.get("RUNS_URL", "").strip()

    lines = [
        f"🚨 **No morning brief for {today}.**",
        f"It is {now} and no delivery has been recorded "
        f"(last delivered: {delivered or 'never'}).",
        "",
        "Likely causes, in order of past frequency:",
        "• GitHub dropped the scheduled run (it fires nothing and reports nothing)",
        "• every generation attempt failed — check the run log",
        "• the brief generated but Discord rejected the send",
    ]
    if runs_url:
        lines += ["", runs_url]
    message = "\n".join(lines)

    if common.env_flag("DISCORD_DRY_RUN"):
        print(f"DISCORD_DRY_RUN: would alert target {target!r} with:\n{message}")
        return 1

    if not token or not target:
        # No way to reach you — still fail loudly so the red run is the signal.
        print(
            "Watchdog: brief is MISSING and DISCORD_BOT_TOKEN/DISCORD_TARGET are "
            "unset, so no alert could be sent.",
            file=sys.stderr,
        )
        return 1

    send_text(token, resolve_channel_id(token, target), message)
    # Non-zero on purpose: the Discord DM is the primary alert, a red run plus
    # GitHub's failure email is the backup in case the DM itself is the problem.
    return 1


if __name__ == "__main__":
    sys.exit(main())
