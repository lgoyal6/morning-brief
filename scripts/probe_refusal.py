#!/usr/bin/env python3
"""TEMPORARY: find which source item makes the primary model refuse the brief.

Delete this file and .github/workflows/probe-refusal.yml once the answer is
recorded. Same pattern as the earlier "temp: probe which models the LLM endpoint
serves" probe.

Method. Build the real prompt (same fetch, same spec, same 130 items), then ask
for only a couple of hundred output tokens and look at how the model stops:

    finish_reason=content_filter + a short body  ->  REFUSED
    finish_reason=length                         ->  fine, it was writing the brief

Capping max_tokens is what makes this affordable. The refusal is 11 completion
tokens and a healthy answer only has to get 200 tokens in to prove it started,
so each probe costs one prompt (~26k input) and almost no output.

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


def refused(resp) -> tuple[bool, str]:
    choice = resp.choices[0]
    body = (choice.message.content or "").strip()
    finish = getattr(choice, "finish_reason", None)
    # A model that is writing the brief will run into the cap, not stop early.
    return (finish == "content_filter" or len(body) < 200), f"{finish} {len(body)}ch {body[:60]!r}"


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

    print(f"Probing {LLM_MODEL} at {PROBE_MAX_TOKENS} max_tokens.\n")
    run = lambda label, subset: probe(
        client, label, subset, quotes, today, prior, term_memory, foundations
    )

    control = run("control (full bundle)", sources)
    if control is False:
        print(
            "\nCONTROL DID NOT REFUSE. The news has rotated since the refusal, so "
            "removing items proves nothing today. Stopping.",
            file=sys.stderr,
        )
        return 0

    results = {}
    for key in SUSPECTS:
        if hits[key]:
            results[key] = run(f"minus {key}", without(key))
    if len(SUSPECTS) > 1:
        results["both"] = run("minus both suspects", without(*SUSPECTS))

    china = [
        s for s in sources
        if any(w in (s.get("title", "") or "").lower()
               for w in ("china", "chinese", "beijing", "taiwan", "xi "))
    ]
    if china:
        drop = {id(s) for s in china}
        run(f"minus all {len(china)} China items", [s for s in sources if id(s) not in drop])

    print("\nSummary:")
    print(f"  control refused: {control}")
    for k, v in results.items():
        print(f"  minus {k}: {'still refused' if v else 'NO LONGER REFUSED'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
