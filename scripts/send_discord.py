#!/usr/bin/env python3
"""Send the dated brief PDF to Discord (stage 3 of 3).

Uses the Discord REST API with a bot token.

DISCORD_TARGET selects the destination:
  * ``user:<user_id>``     -> open a DM channel with that user, then send.
  * ``channel:<channel_id>`` -> send directly to that channel.

On any failure this prints a clear error and exits non-zero, so the workflow is
marked failed AFTER the brief has already been committed and uploaded as an
artifact. On success it claims the day's delivery slot (briefs/.delivery.json),
which the workflow commits in a following step — the slot is deliberately not
claimed until Discord confirms delivery.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

import common

load_dotenv()

API_BASE = "https://discord.com/api/v10"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}"}


def open_dm_channel(token: str, user_id: str) -> str:
    resp = requests.post(
        f"{API_BASE}/users/@me/channels",
        headers={**_headers(token), "Content-Type": "application/json"},
        json={"recipient_id": user_id},
        timeout=30,
    )
    if not resp.ok:
        raise SystemExit(
            f"Discord: failed to open DM with user {user_id} "
            f"(HTTP {resp.status_code}): {resp.text[:500]}\n"
            "Hint: the bot must share a server with you and your DM privacy must allow it."
        )
    return resp.json()["id"]


def resolve_channel_id(token: str, target: str) -> str:
    if target.startswith("user:"):
        return open_dm_channel(token, target.split(":", 1)[1].strip())
    if target.startswith("channel:"):
        return target.split(":", 1)[1].strip()
    raise SystemExit(
        f"Discord: DISCORD_TARGET must start with 'user:' or 'channel:' (got: {target!r})"
    )


def send_text(token: str, channel_id: str, content: str) -> None:
    """Post a plain message. Used by the watchdog, which has no PDF to attach."""
    resp = requests.post(
        f"{API_BASE}/channels/{channel_id}/messages",
        headers={**_headers(token), "Content-Type": "application/json"},
        json={"content": content},
        timeout=30,
    )
    if not resp.ok:
        raise SystemExit(
            f"Discord: alert send failed (HTTP {resp.status_code}): {resp.text[:800]}"
        )
    print(f"Discord: alert sent to channel {channel_id}.")


def send_pdf(token: str, channel_id: str, pdf_path: Path, content: str) -> None:
    url = f"{API_BASE}/channels/{channel_id}/messages"
    for attempt in range(2):
        with open(pdf_path, "rb") as fh:
            files = {"files[0]": (pdf_path.name, fh, "application/pdf")}
            data = {"payload_json": json.dumps({"content": content})}
            resp = requests.post(url, headers=_headers(token), data=data, files=files, timeout=90)

        if resp.ok:
            print(f"Discord: sent {pdf_path.name} to channel {channel_id}.")
            return

        # Handle rate limiting with one retry.
        if resp.status_code == 429 and attempt == 0:
            try:
                retry_after = float(resp.json().get("retry_after", 2))
            except Exception:
                retry_after = 2.0
            print(f"Discord: rate limited, retrying in {retry_after:.1f}s ...", file=sys.stderr)
            time.sleep(retry_after + 0.5)
            continue

        raise SystemExit(
            f"Discord: send failed (HTTP {resp.status_code}): {resp.text[:800]}"
        )


def main() -> int:
    state = common.load_state()

    if state.get("action") == "skipped":
        print("No brief regenerated this run — not sending to Discord.")
        return 0

    # Manual dispatch is for iterating on the pipeline, so it does NOT send by
    # default — that's what kept overnight test runs from DMing the brief at 1am
    # (and it never touches the scheduled morning slot either). Re-run the
    # workflow with the `send` input set to true to actually deliver. Scheduled
    # and local runs (GITHUB_EVENT_NAME unset) always send.
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event == "workflow_dispatch" and not common.env_flag("DISCORD_SEND"):
        print(
            "Manual dispatch — not sending to Discord "
            "(re-run with the `send` input set to true to deliver)."
        )
        return 0

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    target = os.environ.get("DISCORD_TARGET", "").strip()

    if common.env_flag("DISCORD_DRY_RUN"):
        print(f"DISCORD_DRY_RUN: would send to target {target!r} (skipping actual send).")
        return 0

    if not token:
        raise SystemExit("Discord: DISCORD_BOT_TOKEN is not set.")
    if not target:
        raise SystemExit("Discord: DISCORD_TARGET is not set.")

    date = state.get("date") or common.today_str()
    pdf_path = Path(state.get("pdf") or common.dated_pdf_path(date))
    if not pdf_path.exists():
        pdf_path = common.latest_pdf_path()
    if not pdf_path.exists():
        raise SystemExit(f"Discord: no PDF to send (looked for {pdf_path}).")

    content = (
        f"🗞️ **Morning Brief — {date}**\n"
        "AI · Tech Infrastructure · Markets · Geopolitics"
    )
    # Say it here, not only in an Actions annotation nobody reads at breakfast.
    if state.get("truncated"):
        content += (
            "\n\n⚠️ _This brief hit the output limit and the tail is incomplete — "
            "raise `LLM_MAX_TOKENS` or `LLM_MAX_CONTINUATIONS`._"
        )

    channel_id = resolve_channel_id(token, target)
    send_pdf(token, channel_id, pdf_path, content)

    # Claim the day's delivery slot only now that Discord has confirmed the send.
    # Generation used to claim it, so a successful generate + failed send burned
    # the slot and every later cron skipped — the one failure mode the extra crons
    # exist to cover. Scheduled runs only: a manual or local send must never be
    # able to suppress the real morning delivery.
    if state.get("scheduled"):
        common.mark_scheduled_delivery(date)
        print(f"Claimed morning-delivery slot for {date} (send confirmed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
