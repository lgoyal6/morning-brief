#!/usr/bin/env python3
"""Tests for the gate that decides whether a completion is a publishable brief.

On 2026-08-09 the morning brief arrived as a one-page empty PDF. The primary
model returned 13 characters of Chinese refusal ("你好，我无法给到相关内容。") with
finish_reason=content_filter, and the gate in ``attempt_model`` was a bare
``if content:``, so a non-empty refusal counted as success. Nothing retried,
nothing fell back to Qwen or Claude, the 140-char Markdown rendered to a
masthead and a footer, Discord sent it, and the committed delivery marker then
told all four backstop crons the day was already handled.

Two properties keep that from recurring, and both are tested here:

  * a completion that is not a brief is a FAILED attempt, on the same path as empty,
    so the retry ladder and the model fallbacks both get their turn;
  * a failed run must produce NO brief at all rather than a short one, because
    an exit-1 leaves the delivery slot unclaimed and the next cron retries,
    while a short brief gets delivered and closes the slot.

finish_reason=content_filter additionally short-circuits the per-model ladder:
a filter's verdict on a given prompt does not change when you re-ask it with a
larger max_tokens, so those attempts belong to the next candidate model.

Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_brief as gb  # noqa: E402

# The exact completion that shipped on 2026-08-09, verbatim.
REFUSAL_0809 = "你好，我无法给到相关内容。"

# Briefs written before the prompt was rewritten to mandate sections "## 1."
# through "## 11.". 2026-07-25 has 5 numbered sections and 2026-07-28 has 2, so
# both sit under the gate. They are history, not a calibration target: every
# brief since 2026-07-29 has used the current format, and the gate is set for
# that. If a NEW brief ever needs adding here, the gate is too tight; fix the
# gate instead.
LEGACY_FORMAT = {"2026-07-25", "2026-07-28"}


def make_brief(headers: int = 11, body_chars: int = 5000) -> str:
    """A completion shaped like a real brief: numbered sections and real length.

    Calibrated against the briefs the current prompt produces (41k-66k chars
    with 10-11 ``## N.`` headers), so the defaults clear the gate the way a real
    run does. Returned pre-stripped, because the code under test strips what the
    provider returns and the tests compare against this string directly.
    """
    filler = "Real reporting about the day's news. " * (body_chars // 37 + 1)
    sections = [f"## {i}. Section {i}\n\n{filler}\n" for i in range(1, headers + 1)]
    return ("# Morning Brief\n\n" + "\n".join(sections)).strip()


def completion(content: str, finish: str = "stop", reasoning: str = "") -> SimpleNamespace:
    """A minimal stand-in for an OpenAI ChatCompletion response."""
    message = SimpleNamespace(content=content, reasoning_content=reasoning, model_extra={})
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=message)],
        usage=SimpleNamespace(
            prompt_tokens=26075,
            completion_tokens=len(content) // 4,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
    )


class FakeClient:
    """Replays a scripted list of responses and records what it was asked."""

    def __init__(self, script):
        # script: list of responses, or callables taking the request kwargs.
        self._script = list(script)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        step = self._script.pop(0) if self._script else self._script_exhausted()
        return step(kwargs) if callable(step) else step

    def _script_exhausted(self):
        raise AssertionError(
            f"client called {len(self.calls)} times, more than the script allows"
        )

    @property
    def models_called(self):
        return [c["model"] for c in self.calls]


class LooksLikeBrief(unittest.TestCase):
    """The predicate itself: junk out, real briefs in."""

    def test_rejects_the_2026_08_09_refusal(self):
        self.assertFalse(gb.looks_like_brief(REFUSAL_0809))

    def test_rejects_empty_and_whitespace(self):
        for text in ("", "   ", "\n\n"):
            with self.subTest(text=repr(text)):
                self.assertFalse(gb.looks_like_brief(text))

    def test_rejects_an_english_refusal(self):
        self.assertFalse(
            gb.looks_like_brief("I'm sorry, but I can't help with that request.")
        )

    def test_rejects_long_prose_with_no_sections(self):
        # Length alone is not enough; a thinking trace is long too.
        self.assertFalse(gb.looks_like_brief("Let me think about this. " * 400))

    def test_rejects_a_stub_with_too_few_sections(self):
        self.assertFalse(gb.looks_like_brief(make_brief(headers=3)))

    def test_accepts_a_realistic_brief(self):
        self.assertTrue(gb.looks_like_brief(make_brief()))

    def test_accepts_every_committed_brief(self):
        """Regression floor: the gate must not reject a brief that really shipped.

        The gate now decides whether a run succeeds, so a bar set too high costs
        a morning. Checking it against every brief on disk is the cheapest way to
        keep it honest as the prompt evolves.
        """
        briefs = sorted(REPO_ROOT.glob("briefs/2026-*-ai-tech-market-brief.md"))
        self.assertTrue(briefs, "no committed briefs found to check against")
        checked = 0
        for path in briefs:
            date = path.name[:10]
            text = path.read_text(errors="ignore")
            if date in LEGACY_FORMAT:
                continue
            # The 2026-08-09 refusal is committed as a brief too. It is the one
            # file here that must fail the gate; that is the whole bug.
            expected = REFUSAL_0809 not in text
            with self.subTest(brief=path.name):
                self.assertEqual(gb.looks_like_brief(text), expected)
            checked += 1
        self.assertGreater(checked, 5, "too few briefs checked to be a real floor")


class AttemptModelGate(unittest.TestCase):
    """One model's retry ladder, with the network replaced by FakeClient."""

    def setUp(self):
        # No real backoff: the ladder sleeps 5s then 10s between attempts.
        patcher = mock.patch.object(gb.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        retries = mock.patch.object(gb, "LLM_MAX_RETRIES", 3)
        retries.start()
        self.addCleanup(retries.stop)

    def run_attempt(self, script):
        client = FakeClient(script)
        return client, gb.attempt_model(client, "test-model", [], "key")

    def test_good_completion_is_returned_on_the_first_attempt(self):
        brief = make_brief()
        client, out = self.run_attempt([completion(brief)])
        self.assertEqual(out, brief)
        self.assertEqual(len(client.calls), 1)

    def test_content_filter_refusal_yields_nothing(self):
        """The 2026-08-09 regression: this used to return the refusal as the brief."""
        _, out = self.run_attempt([completion(REFUSAL_0809, finish="content_filter")])
        self.assertEqual(out, "")

    def test_content_filter_does_not_burn_retries_on_the_same_model(self):
        # Script holds exactly one response: a second call would raise.
        client, out = self.run_attempt([completion(REFUSAL_0809, finish="content_filter")])
        self.assertEqual(out, "")
        self.assertEqual(len(client.calls), 1)

    def test_short_junk_retries_the_ladder_then_gives_up(self):
        client, out = self.run_attempt([completion("nope.")] * 3)
        self.assertEqual(out, "")
        self.assertEqual(len(client.calls), 3)

    def test_short_junk_then_a_real_brief_recovers(self):
        brief = make_brief()
        client, out = self.run_attempt([completion("nope."), completion(brief)])
        self.assertEqual(out, brief)
        self.assertEqual(len(client.calls), 2)

    def test_empty_completion_still_retries(self):
        # The pre-existing empty-content path must survive the refactor.
        brief = make_brief()
        client, out = self.run_attempt([completion(""), completion(brief)])
        self.assertEqual(out, brief)
        self.assertEqual(len(client.calls), 2)

    def test_brief_hidden_in_reasoning_content_is_still_salvaged(self):
        brief = make_brief()
        _, out = self.run_attempt([completion("", reasoning=brief)])
        self.assertEqual(out, brief)

    def test_truncated_brief_is_resumed_before_being_judged(self):
        """A finish_reason=length answer must be continued, not rejected as short."""
        head = make_brief(headers=6, body_chars=1000)
        tail = "\n## 7. Bottom Line\n\nThe rest of the brief."
        client, out = self.run_attempt(
            [completion(head, finish="length"), completion(tail)]
        )
        self.assertTrue(out.startswith(head[:200]))
        self.assertIn("Bottom Line", out)
        self.assertEqual(len(client.calls), 2)


class ModelFallback(unittest.TestCase):
    """call_llm's outer loop: a refusing primary must hand off, not ship."""

    def setUp(self):
        patcher = mock.patch.object(gb.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (
            ("LLM_MAX_RETRIES", 3),
            ("LLM_MODEL", "primary"),
            ("LLM_FALLBACK_MODELS", ["backup-a", "backup-b"]),
        ):
            p = mock.patch.object(gb, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_call(self, script):
        client = FakeClient(script)
        with mock.patch("openai.OpenAI", return_value=client):
            return client, gb.call_llm([], "key")

    def test_filtered_primary_falls_back_to_the_next_model(self):
        brief = make_brief()
        client, out = self.run_call(
            [completion(REFUSAL_0809, finish="content_filter"), completion(brief)]
        )
        self.assertEqual(out, brief)
        self.assertEqual(client.models_called, ["primary", "backup-a"])
        self.assertEqual(gb.MODEL_USED, "backup-a")

    def test_all_models_refusing_produces_no_brief(self):
        """Nothing to publish beats something empty: main() exits 1 on "" and the
        delivery slot stays unclaimed for the next cron."""
        client, out = self.run_call(
            [completion(REFUSAL_0809, finish="content_filter")] * 3
        )
        self.assertEqual(out, "")
        self.assertEqual(client.models_called, ["primary", "backup-a", "backup-b"])


if __name__ == "__main__":
    unittest.main()
