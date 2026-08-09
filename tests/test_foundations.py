#!/usr/bin/env python3
"""Tests for the Concept Spotlight syllabus and its separate generation model.

The syllabus is a queue: teach the first concept nobody has spotlighted yet. So
anything that stops a concept being RECOGNISED as taught blocks the entire track
behind it, silently, while the prompt re-issues the same concept every day.

That is what froze the world-affairs track. The model titles its own sections and
does not copy the syllabus name exactly:

    wrote "The Taiwan Strait"            syllabus "Taiwan Strait"
    wrote "Market Order vs. Limit Order" syllabus "Market Order vs Limit Order"

Both were written in full and neither matched, so both were asked for again --
Taiwan Strait was delivered on 2026-08-07 and was still being asked for on 08-08
and 08-09, with OPEC, Sanctions and NATO stalled behind it. Note what this was NOT:
the model never refused to write it. _concept_key is the fix and the ConceptKey
cases below are the regression test.

Also tested here, as backstops for the same shape of failure:

  * the syllabus steps past a concept that keeps failing, and steps BACK to it
    once the track moves again, so nothing is ever dropped;
  * the optional separate Spotlight model (off by default) and its splice.

The splice is load-bearing whenever it is on: we emit the heading ourselves so
spotlight_dates() can always match it. If that round trip breaks, every concept
looks undelivered forever and the syllabus never advances at all.

Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import common  # noqa: E402
import generate_brief as gb  # noqa: E402

TRACK = [
    ("Alpha", "cover alpha"),
    ("Bravo", "cover bravo"),
    ("Charlie", "cover charlie"),
]


def completion(content: str, finish: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish,
                                 message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=200, completion_tokens=len(content) // 4,
                              completion_tokens_details=None),
    )


class FakeClient:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class ConceptKey(unittest.TestCase):
    """Heading wording drift must not read as "never taught"."""

    def test_the_2026_08_07_taiwan_strait_regression(self):
        self.assertEqual(
            gb._concept_key("The Taiwan Strait"), gb._concept_key("Taiwan Strait")
        )

    def test_the_2026_08_07_limit_order_regression(self):
        self.assertEqual(
            gb._concept_key("Market Order vs. Limit Order"),
            gb._concept_key("Market Order vs Limit Order"),
        )

    def test_tolerates_leading_articles_case_and_punctuation(self):
        for written, syllabus in (
            ("A Chokepoint", "Chokepoint"),
            ("the sanctions", "Sanctions"),
            ("OPEC and OPEC+", "OPEC and OPEC+"),
            ("Brent vs. WTI Crude", "Brent vs WTI Crude"),
            ("Capex (Capital Expenditure)", "Capex"),
            ("NATO and Article 5:", "NATO and Article 5"),
        ):
            with self.subTest(written=written):
                self.assertEqual(gb._concept_key(written), gb._concept_key(syllabus))

    def test_still_tells_genuinely_different_concepts_apart(self):
        """Normalising must not collapse two real syllabus entries into one."""
        keys = [gb._concept_key(n) for n, _ in gb.FOUNDATIONS_MARKETS + gb.FOUNDATIONS_WORLD]
        self.assertEqual(len(keys), len(set(keys)), "two syllabus entries share a key")
        self.assertNotEqual(
            gb._concept_key("Strait of Hormuz"), gb._concept_key("Strait of Malacca")
        )

    def test_every_syllabus_name_has_a_nonempty_key(self):
        for name, _ in gb.FOUNDATIONS_MARKETS + gb.FOUNDATIONS_WORLD:
            with self.subTest(concept=name):
                self.assertTrue(gb._concept_key(name).strip())


class TrackPick(unittest.TestCase):
    """The queue, with delivery dates and brief dates passed in directly."""

    def setUp(self):
        p = mock.patch.object(gb, "FOUNDATIONS_STUCK_AFTER", 3)
        p.start()
        self.addCleanup(p.stop)

    def test_picks_the_head_when_the_track_is_moving(self):
        delivered = {}
        briefs = ["2026-08-01", "2026-08-02"]
        self.assertEqual(gb.track_pick(TRACK, delivered, briefs)[0], "Alpha")

    def test_skips_concepts_already_delivered(self):
        delivered = {"alpha": "2026-08-01"}
        briefs = ["2026-08-01"]
        self.assertEqual(gb.track_pick(TRACK, delivered, briefs)[0], "Bravo")

    def test_defers_the_head_once_the_track_stalls(self):
        """Three briefs since the last delivery means three failed asks."""
        delivered = {"alpha": "2026-08-01"}
        briefs = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"]
        self.assertEqual(gb.track_pick(TRACK, delivered, briefs)[0], "Charlie")

    def test_does_not_defer_one_brief_early(self):
        # Two stalled briefs is under the threshold; a transient failure must not
        # cost a concept nobody actually had trouble with.
        delivered = {"alpha": "2026-08-01"}
        briefs = ["2026-08-01", "2026-08-02", "2026-08-03"]
        self.assertEqual(gb.track_pick(TRACK, delivered, briefs)[0], "Bravo")

    def test_a_deferred_concept_comes_back_after_the_track_advances(self):
        """Postponed, never dropped: this is the whole safety property."""
        delivered = {"alpha": "2026-08-01", "charlie": "2026-08-05"}
        briefs = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]
        self.assertEqual(gb.track_pick(TRACK, delivered, briefs)[0], "Bravo")

    def test_never_indexes_past_the_end_of_the_track(self):
        delivered = {}
        briefs = [f"2026-08-{d:02d}" for d in range(1, 29)]  # a very long stall
        self.assertEqual(gb.track_pick(TRACK, delivered, briefs)[0], "Charlie")

    def test_exhausted_track_returns_nothing(self):
        delivered = {"alpha": "1", "bravo": "2", "charlie": "3"}
        self.assertIsNone(gb.track_pick(TRACK, delivered, ["2026-08-01"]))

    def test_nothing_delivered_yet_still_starts_at_the_head(self):
        self.assertEqual(gb.track_pick(TRACK, {}, [])[0], "Alpha")


class SpliceRoundTrip(unittest.TestCase):
    """splice_spotlights must produce headings spotlight_dates() can read back."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = common.BRIEFS_DIR
        common.BRIEFS_DIR = self.tmp
        self.addCleanup(lambda: setattr(common, "BRIEFS_DIR", self._saved))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_brief(self, text, date="2026-08-10"):
        (self.tmp / f"{date}-{common.BRIEF_SLUG}.md").write_text(text)

    def test_inserts_into_the_foundations_section(self):
        md = "# B\n\n## 8. Building\n\ntext\n\n## 9. Foundations & Terms\n\n- **Term:** x\n\n## 11. Sources\n\n- a\n"
        out = gb.splice_spotlights(md, [("Taiwan Strait", "body " * 100)])
        self.assertIn("## 9. Foundations & Terms\n\n### Concept Spotlight: Taiwan Strait", out)
        # The pre-existing term entries must survive.
        self.assertIn("- **Term:** x", out)
        self.assertLess(out.index("Concept Spotlight"), out.index("## 11. Sources"))

    def test_falls_back_to_before_sources_when_the_section_is_missing(self):
        md = "# B\n\n## 8. Building\n\ntext\n\n## 11. Sources\n\n- a\n"
        out = gb.splice_spotlights(md, [("OPEC and OPEC+", "body " * 100)])
        self.assertIn("Concept Spotlight: OPEC and OPEC+", out)
        self.assertLess(out.index("Concept Spotlight"), out.index("## 11. Sources"))

    def test_appends_when_there_is_no_section_and_no_sources(self):
        out = gb.splice_spotlights("# B\n\njust text\n", [("Sanctions", "body " * 100)])
        self.assertIn("Concept Spotlight: Sanctions", out)

    def test_no_spotlights_leaves_the_brief_untouched(self):
        md = "# B\n\n## 9. Foundations & Terms\n\n- **Term:** x\n"
        self.assertEqual(gb.splice_spotlights(md, []), md)

    def test_spliced_heading_retires_the_concept_from_the_syllabus(self):
        """The invariant the whole queue depends on."""
        md = "# B\n\n## 9. Foundations & Terms\n\n- **Term:** x\n"
        self.write_brief(gb.splice_spotlights(md, [("Taiwan Strait", "body " * 100)]))
        self.assertIn("taiwan strait", gb.spotlight_dates())
        # And therefore the world track moves past it.
        delivered, briefs = gb.spotlight_dates(), gb.brief_dates()
        picked = gb.track_pick(gb.FOUNDATIONS_WORLD, delivered, briefs)
        self.assertNotEqual(picked[0], "Taiwan Strait")

    def test_every_syllabus_name_survives_the_round_trip(self):
        """A name that does not read back would silently stall its track."""
        for name, _ in gb.FOUNDATIONS_MARKETS + gb.FOUNDATIONS_WORLD:
            with self.subTest(concept=name):
                for f in self.tmp.glob("*.md"):
                    f.unlink()
                self.write_brief(gb.splice_spotlights("# B\n", [(name, "body " * 100)]))
                self.assertIn(gb._concept_key(name), gb.spotlight_dates())


class FoundationsBlock(unittest.TestCase):
    def test_external_mode_tells_the_model_to_skip_the_spotlights(self):
        block = gb.build_foundations_block([("Taiwan Strait", "x")], external=True)
        self.assertIn("do NOT write", block)
        self.assertIn("Concept Spotlight", block)
        # The concept must NOT be named, or the model may write it anyway.
        self.assertNotIn("Taiwan Strait", block)

    def test_inline_mode_still_names_the_concepts(self):
        block = gb.build_foundations_block([("Taiwan Strait", "why a 180km channel")], external=False)
        self.assertIn("Taiwan Strait", block)
        self.assertIn("why a 180km channel", block)

    def test_exhausted_syllabus_asks_the_model_to_choose(self):
        self.assertIn("exhausted", gb.build_foundations_block([], external=False))


class GenerateSpotlights(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(gb, "FOUNDATIONS_MODEL", "test-spotlight-model")
        p.start()
        self.addCleanup(p.stop)

    def run_gen(self, picks, script):
        client = FakeClient(script)
        with mock.patch("openai.OpenAI", return_value=client):
            return client, gb.generate_spotlights(picks, "key")

    def test_returns_the_body_for_each_concept(self):
        body = "A real explanation. " * 40
        _, out = self.run_gen(
            [("Alpha", "a"), ("Bravo", "b")], [completion(body), completion(body)]
        )
        self.assertEqual([n for n, _ in out], ["Alpha", "Bravo"])

    def test_uses_the_dedicated_model(self):
        body = "A real explanation. " * 40
        client, _ = self.run_gen([("Alpha", "a")], [completion(body)])
        self.assertEqual(client.calls[0]["model"], "test-spotlight-model")

    def test_a_refusal_is_dropped_not_published(self):
        """Same lesson as the brief gate: non-empty is not usable."""
        _, out = self.run_gen(
            [("Alpha", "a")],
            [completion("你好，我无法给到相关内容。", finish="content_filter")],
        )
        self.assertEqual(out, [])

    def test_one_failure_does_not_lose_the_other_spotlight(self):
        body = "A real explanation. " * 40
        _, out = self.run_gen(
            [("Alpha", "a"), ("Bravo", "b")], [completion("nope."), completion(body)]
        )
        self.assertEqual([n for n, _ in out], ["Bravo"])

    def test_an_api_error_is_swallowed_so_the_brief_still_ships(self):
        _, out = self.run_gen([("Alpha", "a")], [RuntimeError("endpoint down")])
        self.assertEqual(out, [])

    def test_a_model_supplied_heading_is_stripped(self):
        """We add our own heading; a second one would break the round trip."""
        body = "### Concept Spotlight: Alpha\n\n" + ("Real text. " * 60)
        _, out = self.run_gen([("Alpha", "a")], [completion(body)])
        self.assertFalse(out[0][1].startswith("#"))
        self.assertEqual(gb.splice_spotlights("# B\n", out).count("Concept Spotlight"), 1)

    def test_disabled_when_no_model_is_configured(self):
        with mock.patch.object(gb, "FOUNDATIONS_MODEL", ""):
            self.assertEqual(gb.generate_spotlights([("Alpha", "a")], "key"), [])


if __name__ == "__main__":
    unittest.main()
