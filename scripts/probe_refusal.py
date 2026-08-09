#!/usr/bin/env python3
"""TEMPORARY: find which source item makes the primary model refuse the brief.

Delete this file and .github/workflows/probe-refusal.yml once the answer is
recorded. Same pattern as the earlier "temp: probe which models the LLM endpoint
serves" probe.

Method. Build the real prompt (same fetch, same spec, same 130 items), then ask
for only a couple of hundred output tokens and read finish_reason:

    finish_reason=content_filter              ->  REFUSED
    anything else                             ->  not refused

Do NOT treat "short body" as a refusal. DeepSeek is a reasoning model, so a
healthy answer spends the whole 200-token cap on its hidden thinking trace and
comes back with finish_reason=length, reasoning_tokens=200 and ZERO visible
content. The first version of this probe called that a refusal and reported four
false positives. A real refusal is the opposite shape: reasoning_tokens=0 and a
13-character body.

Capping max_tokens is what makes this affordable either way: a refusal is 11
completion tokens, and a healthy answer only has to reach the cap to prove it
engaged, so each probe costs one prompt (~28k input) and almost no output.

Because the trigger is a property of one day's feed, the useful direction is
INJECTION, not removal: take today's bundle (which does not refuse) and add a
suspect headline back in. If the refusal returns, that item is the cause.

Run: python scripts/probe_refusal.py
"""

from __future__ import annotations

import os
import sys

import common
from generate_brief import (
    LLM_BASE_URL,
    LLM_MODEL,
    build_messages,
    build_foundations_block,
    build_term_memory_block,
    create_completion,
    completion_kwargs,
    describe_completion,
    fetch_sources,
    pick_foundations,
    previous_brief_excerpt,
    resolve_api_key,
    taught_terms,
)

PROBE_MAX_TOKENS = int(os.environ.get("PROBE_MAX_TOKENS", "200"))

# Candidate triggers, matched loosely on the headline.
SUSPECTS = {
    "arunachal": "arunachal",
    "xi-vs-trump": "xi vs trump",
}


def load_archived_suspects() -> dict:
    """The 2026-08-09 suspect items, captured before the feed rotated them out."""
    path = common.REPO_ROOT / "scripts" / "probe_suspects.json"
    if not path.exists():
        return {}
    import json

    return json.loads(path.read_text())


def refused(resp) -> tuple[bool, str]:
    choice = resp.choices[0]
    body = (choice.message.content or "").strip()
    finish = getattr(choice, "finish_reason", None)
    # finish_reason is the ONLY reliable signal here. See the module docstring:
    # an empty body means the thinking trace used the cap, not that it refused.
    return finish == "content_filter", f"{finish} {len(body)}ch {body[:60]!r}"


def probe(client, label, sources, quotes, today, prior, term_memory, foundations):
    messages = build_messages(
        sources, quotes, today, prior, False, term_memory, foundations
    )
    kwargs = completion_kwargs(LLM_MODEL, messages, PROBE_MAX_TOKENS)
    try:
        resp = create_completion(client, kwargs)
    except Exception as exc:
        print(f"  {label:28} ERROR {exc}", flush=True)
        return None
    is_refusal, detail = refused(resp)
    verdict = "REFUSED" if is_refusal else "ok"
    print(f"  {label:28} {verdict:8} ({len(sources)} items) {detail}", flush=True)
    print(f"  {'':28} {describe_completion(resp)}", flush=True)
    return is_refusal


def main() -> int:
    from openai import OpenAI

    api_key = resolve_api_key()
    if not api_key:
        print("ERROR: no API key.", file=sys.stderr)
        return 1

    today = common.today_str()
    sources = fetch_sources()
    quotes = []
    reinforce, mastered = taught_terms()
    term_memory = build_term_memory_block(reinforce, mastered)
    foundations = build_foundations_block(pick_foundations())
    prior = previous_brief_excerpt(today)

    hits = {
        key: [s for s in sources if needle in (s.get("title", "") or "").lower()]
        for key, needle in SUSPECTS.items()
    }
    print(f"\nFetched {len(sources)} sources.")
    for key, matched in hits.items():
        for s in matched:
            print(f"  suspect [{key}]: {(s.get('title') or '')[:100]}")
    print()

    client = OpenAI(
        api_key=api_key,
        base_url=LLM_BASE_URL,
        timeout=float(os.environ.get("LLM_TIMEOUT", "600")),
        max_retries=0,
    )

    def without(*keys):
        drop = {id(s) for k in keys for s in hits.get(k, [])}
        return [s for s in sources if id(s) not in drop]

    def plus(item):
        # Front of the world section, where the refused bundles carried it.
        return [item] + [s for s in sources if s.get("link") != item.get("link")]

    print(f"Probing {LLM_MODEL} at {PROBE_MAX_TOKENS} max_tokens.\n")
    run = lambda label, subset: probe(
        client, label, subset, quotes, today, prior, term_memory, foundations
    )
    results: dict[str, bool | None] = {}

    control = run("control (today's bundle)", sources)
    results["control"] = control

    if control:
        # Today's feed still trips it: subtract to find out what is doing it.
        for key in SUSPECTS:
            if hits[key]:
                results[f"minus {key}"] = run(f"minus {key}", without(key))
        results["minus both"] = run("minus both suspects", without(*SUSPECTS))
    else:
        # Today's feed is clean, so removal proves nothing. Add the 2026-08-09
        # suspects back to a bundle that currently passes: if the refusal
        # returns, that headline is the cause.
        archived = load_archived_suspects()
        if not archived:
            print(
                "\nControl did not refuse and no archived suspects to inject "
                "(scripts/probe_suspects.json missing). Nothing to test.",
                file=sys.stderr,
            )
            return 0
        for key, item in archived.items():
            results[f"plus {key}"] = run(f"plus {key}", plus(item))
        if len(archived) > 1:
            merged = list(archived.values())
            rest = [s for s in sources
                    if s.get("link") not in {i.get("link") for i in merged}]
            results["plus both"] = run("plus both suspects", merged + rest)

    print("\nSummary:")
    for k, v in results.items():
        state = "REFUSED" if v else ("error" if v is None else "ok")
        print(f"  {k:24} {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
