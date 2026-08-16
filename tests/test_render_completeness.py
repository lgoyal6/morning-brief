#!/usr/bin/env python3
"""Tests for the two ways an incomplete brief used to ship unnoticed.

Both failures were silent: no exception, no non-zero exit, just a PDF in the
inbox with less in it than the Markdown had.

  * WeasyPrint drops content out of the multi-column flow on some documents.
    2026-08-07 went out without its Bottom Line and Sources sections (87% of the
    text survived); 2026-08-12 rendered 5 pages on CI from Markdown worth 10
    (55%). Re-rendering either one in a single column restores the whole brief,
    so render() now reads back what it wrote and falls back when it is short.
  * A backstop cron that fires late used to check out the SHA pinned when its
    event fired rather than the head of main, read a stale delivery marker,
    decided the day was undelivered, and regenerated a brief that had already
    been sent (2026-08-16, run 31954422752). `ref: main` on the checkout is the
    fix, and nothing else in the pipeline notices if it goes away.

WeasyPrint is stubbed here on purpose. The Tests workflow installs
requirements.txt without the Pango/Cairo system libs WeasyPrint needs at render
time, so a test that really rasterised a PDF could not run there. What is under
test is render()'s decision logic, not WeasyPrint's layout.

Run: python -m unittest discover -s tests
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import render_pdf  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "morning-brief.yml"

SAMPLE_MD = (
    "# Test Brief\n\n"
    + "\n".join(
        f"## {i}. Section {i}\n\n" + ("Body text for the section. " * 40) + "\n"
        for i in range(1, 9)
    )
    + "\n---\n\n_Generated 2026-08-16 07:59 PDT · 0 sources_\n"
)


class FakeHTML:
    """Stand-in for weasyprint.HTML that writes a placeholder file."""

    def __init__(self, string="", base_url=None):
        self.string = string

    def write_pdf(self, target):
        Path(target).write_bytes(b"%PDF-1.7\n% placeholder\n")


class RenderFallsBackOnContentLoss(unittest.TestCase):
    """render() must notice a short PDF and re-render it in a single column."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.md = self.tmp / "2026-08-16-ai-tech-market-brief.md"
        self.md.write_text(SAMPLE_MD)
        self.pdf = self.tmp / "2026-08-16-ai-tech-market-brief.pdf"
        self._saved = (render_pdf.fetch_photos, render_pdf.rendered_text_len,
                       sys.modules.get("weasyprint"))
        render_pdf.fetch_photos = lambda sources, limit: []  # no network in tests
        sys.modules["weasyprint"] = types.SimpleNamespace(HTML=FakeHTML)

    def tearDown(self) -> None:
        render_pdf.fetch_photos, render_pdf.rendered_text_len, weasy = self._saved
        if weasy is None:
            sys.modules.pop("weasyprint", None)
        else:
            sys.modules["weasyprint"] = weasy
        shutil.rmtree(self.tmp, ignore_errors=True)

    def columns_used(self) -> int:
        """Column count in the debug HTML render() writes beside the PDF."""
        html = self.pdf.with_suffix(".debug.html").read_text()
        return int(re.search(r"column-count: (\d+);", html).group(1))

    def stub_lengths(self, *lengths):
        """Report each length in turn, recording the layout it was measured on."""
        seen, remaining = [], list(lengths)

        def fake(pdf_file):
            seen.append(self.columns_used())
            return remaining.pop(0)

        render_pdf.rendered_text_len = fake
        return seen

    def test_intact_pdf_keeps_the_two_column_layout(self):
        layouts = self.stub_lengths(100_000)
        render_pdf.render(self.md, self.pdf, "2026-08-16")
        self.assertEqual(layouts, [2], "an intact PDF must not be rendered twice")
        self.assertEqual(self.columns_used(), 2)
        self.assertTrue(self.pdf.exists())

    def test_short_pdf_is_re_rendered_in_one_column(self):
        layouts = self.stub_lengths(100, 100_000)
        render_pdf.render(self.md, self.pdf, "2026-08-16")
        self.assertEqual(layouts, [2, 1], "a short two-column PDF must be retried in one column")
        self.assertEqual(self.columns_used(), 1, "the shipped PDF must be the one-column one")

    def test_worse_fallback_is_discarded(self):
        # If one column somehow keeps even less, ship the better of the two
        # rather than the regression.
        layouts = self.stub_lengths(1_000, 100, 1_000)
        render_pdf.render(self.md, self.pdf, "2026-08-16")
        self.assertEqual(layouts, [2, 1, 2])
        self.assertEqual(self.columns_used(), 2)

    def test_both_layouts_short_still_ships_a_pdf(self):
        # A truncated brief beats no brief: warn, but leave a file behind.
        self.stub_lengths(100, 200)
        render_pdf.render(self.md, self.pdf, "2026-08-16")
        self.assertTrue(self.pdf.exists())

    def test_unreadable_pdf_does_not_trigger_a_rerender(self):
        # -1 means "could not check", not "empty". Reading it as content loss
        # would burn a second render and ship the fallback layout for no reason.
        layouts = self.stub_lengths(-1)
        render_pdf.render(self.md, self.pdf, "2026-08-16")
        self.assertEqual(layouts, [2], "an unreadable PDF must not be re-rendered")
        self.assertEqual(self.columns_used(), 2)


class CheckoutPinsBranchHead(unittest.TestCase):
    """The delivery marker is only as fresh as the tree the run checks out."""

    def test_morning_brief_checkout_uses_ref_main(self):
        found = re.search(
            r"- uses: actions/checkout@v4\s*\n\s*with:\s*\n\s*ref: main",
            WORKFLOW.read_text(),
        )
        self.assertIsNotNone(
            found,
            "morning-brief.yml must check out `ref: main`. Without it a backstop "
            "cron that fires late reads the delivery marker from the SHA pinned "
            "when its event fired, regenerates an already-delivered brief, and "
            "dies on a rebase conflict.",
        )


if __name__ == "__main__":
    unittest.main()
