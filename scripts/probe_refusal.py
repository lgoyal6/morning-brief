#!/usr/bin/env python3
"""TEMPORARY: what shape of headline makes the primary model refuse the brief?

Delete this and .github/workflows/probe-refusal.yml once the answer is recorded.

Established on 2026-08-09: one real headline about India renaming places in
Arunachal Pradesh, injected into an otherwise ordinary 130-item bundle, flips
DeepSeek from writing the brief to returning 13 characters of Chinese refusal.
6 of 9 with it, 0 of 14 without.

Open question this run answers: is the trigger the TOPIC (China, Taiwan, borders)
or the FRAMING (one state actively countering another state's territorial claim)?
Four injected arms, each into the same clean bundle:

  arunachal             real, known to refuse            China sovereignty, countered
  taiwan_tension        real, known NOT to refuse        China adjacent, reportage
  taiwan_sovereignty    SYNTHETIC                        China sovereignty, countered
  falklands_sovereignty SYNTHETIC                        sovereignty, countered, no China

falklands is the control that separates the two hypotheses. It is deliberately
near-identical in structure to the arunachal headline (a state naming sites to
counter another state's claim) with China removed. If arunachal refuses and
falklands does not, the trigger is specific to Chinese territorial claims rather
than to sovereignty disputes in general.

The two synthetic headlines are written to be realistic but they are NOT from the
feed. Say so in anything published off this.

Verdict rule: finish_reason == "content_filter" and nothing else. DeepSeek is a
reasoning model, so a healthy answer spends the whole 200-token cap on its hidden
thinking and returns finish_reason=length with reasoning_tokens=200 and zero
visible content. An earlier version of this probe scored that as a refusal and
reported four false positives.

Run: python scripts/probe_refusal.py
"""

from __future__ import annotations

import json
import os
import sys

import common
from generate_brief import (
    LLM_BASE_URL,
    LLM_MODEL,
    build_foundations_block,
    build_messages,
    build_term_memory_block,
    completion_kwargs,
    create_completion,
    describe_completion,
    fetch_sources,
    pick_foundations,
    previous_brief_excerpt,
    resolve_api_key,
    taught_terms,
)

PROBE_MAX_TOKENS = int(os.environ.get("PROBE_MAX_TOKENS", "200"))
PROBE_REPEATS = int(os.environ.get("PROBE_REPEATS", "3"))


def load_arms() -> dict:
    path = common.REPO_ROOT / "scripts" / "probe_suspects.json"
    return json.loads(path.read_text()) if path.exists() else {}


def refused(resp) -> tuple[bool, str]:
    choice = resp.choices[0]
    body = (choice.message.content or "").strip()
    finish = getattr(choice, "finish_reason", None)
    return finish == "content_filter", f"{finish} {len(body)}ch {body[:40]!r}"


def main() -> int:
    from openai import OpenAI

    api_key = resolve_api_key()
    if not api_key:
        print("ERROR: no API key.", file=sys.stderr)
        return 1

    arms = load_arms()
    if not arms:
        print("ERROR: scripts/probe_suspects.json missing.", file=sys.stderr)
        return 1

    today = common.today_str()
    sources = fetch_sources()
    reinforce, mastered = taught_terms()
    term_memory = build_term_memory_block(reinforce, mastered)
    foundations = build_foundations_block(pick_foundations())
    prior = previous_brief_excerpt(today)

    # Start from a bundle with none of the injected headlines in it, so every arm
    # differs from the control by exactly one item.
    injected_links = {a["link"] for a in arms.values()}
    base = [s for s in sources if s.get("link") not in injected_links]

    client = OpenAI(
        api_key=api_key,
        base_url=LLM_BASE_URL,
        timeout=float(os.environ.get("LLM_TIMEOUT", "600")),
        max_retries=0,
    )

    def call(bundle):
        messages = build_messages(
            bundle, [], today, prior, False, term_memory, foundations
        )
        try:
            resp = create_completion(
                client, completion_kwargs(LLM_MODEL, messages, PROBE_MAX_TOKENS)
            )
        except Exception as exc:
            print(f"      ERROR {exc}", flush=True)
            return None
        is_refusal, detail = refused(resp)
        print(f"      {'REFUSED' if is_refusal else 'ok':8} {detail}", flush=True)
        print(f"      {'':8} {describe_completion(resp)}", flush=True)
        return is_refusal

    plan = [("control", None)] + [(name, item) for name, item in arms.items()]
    print(f"\nBase bundle: {len(base)} items. {PROBE_REPEATS} reps per arm.")
    print(f"Model: {LLM_MODEL}, max_tokens={PROBE_MAX_TOKENS}\n")

    counts = {name: [0, 0] for name, _ in plan}
    for rep in range(1, PROBE_REPEATS + 1):
        for name, item in plan:
            bundle = base if item is None else [item] + base
            print(f"  rep {rep}  {name}", flush=True)
            verdict = call(bundle)
            counts[name][1] += 1
            if verdict:
                counts[name][0] += 1

    print("\n" + "=" * 62)
    print(f"{'arm':24} {'refused':>10}   framing")
    print("=" * 62)
    labels = {
        "control": "no injection",
        "arunachal": "China claim, countered (real)",
        "taiwan_tension": "China adjacent, reportage (real)",
        "taiwan_sovereignty": "China claim, countered (synthetic)",
        "falklands_sovereignty": "non-China claim, countered (synthetic)",
    }
    for name, (refusals, total) in counts.items():
        print(f"{name:24} {refusals:>4}/{total:<5}   {labels.get(name, '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
