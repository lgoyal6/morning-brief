#!/usr/bin/env python3
"""Send the dated brief PDF to Discord (stage 3 of 3).

Uses the Discord REST API with a bot token.

DISCORD_TARGET selects the destination:
  * ``user:<user_id>``     -> open a DM channel with that user, then send.
  * ``channel:<channel_id>`` -> send directly to that channel.

On any failure this prints a clear error and exits non-zero, so the workflow is
marked failed AFTER the brief has already been committed and uploaded as an
artifact (this step runs last).
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

    channel_id = resolve_channel_id(token, target)
    send_pdf(token, channel_id, pdf_path, content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
