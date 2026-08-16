#!/usr/bin/env python3
"""Render the generated Markdown brief to a two-column magazine PDF (stage 2 of 3).

This replaces the old plain single-column ReportLab layout with an HTML + CSS
newspaper look rendered by WeasyPrint:

  * a styled masthead (title, date, section tagline),
  * a genuine two-column article flow with full-width section headers,
  * a matplotlib chart of the day's biggest watchlist moves ("graphs"), and
  * best-effort photos pulled from the top stories' Open Graph images ("pics").

Data for the chart/photos comes from ``briefs/YYYY-MM-DD-sources.json`` (written
by generate_brief.py). Everything visual degrades gracefully: no quotes -> no
chart; no reachable images -> no photo strip; the text brief always renders.

Reads the target path from the state file written by generate_brief.py, falling
back to today's date. Writes the dated PDF and copies it to
``briefs/latest-ai-tech-market-brief.pdf``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import markdown as md_lib
import requests
from bs4 import BeautifulSoup

import common

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Palette (kept in sync with the four coverage pillars).
INK = "#141821"
ACCENT = "#b3202c"       # masthead red
LINK = "#1a56db"
GAIN = "#15803d"
LOSS = "#b91c1c"

TAGLINE = "World · Markets & Money · AI & Infrastructure · Models & Research"

# Fraction of the brief's visible text that must survive into the PDF. WeasyPrint
# silently drops content out of the two-column flow on some documents: 2026-08-07
# shipped without its Bottom Line and Sources sections (0.87 of the text), and
# 2026-08-12 lost sections 7-11 on CI (0.55): 5 pages where the Markdown was
# worth 10. Nothing downstream noticed either one. Across the 24 briefs in the
# archive every healthy render scores 1.03-1.06 (extraction picks up the running
# header/footer and list bullets on top of the body text), so 0.95 sits far below
# the good runs and far above both bad ones.
MIN_RENDERED_TEXT_RATIO = float(os.environ.get("BRIEF_MIN_TEXT_RATIO", "0.95"))


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
def load_provenance(date: str) -> tuple[list[dict], list[dict]]:
    """Return (quotes, sources) from the day's sources JSON, if available."""
    path = common.sources_path(date)
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text())
        return data.get("quotes", []) or [], data.get("sources", []) or []
    except (ValueError, OSError):
        return [], []


def pretty_date(date: str) -> str:
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return date
    # %-d is non-portable; strip a leading zero by hand.
    return dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")


# ---------------------------------------------------------------------------
# Chart: biggest watchlist moves (matplotlib -> data URI)
# ---------------------------------------------------------------------------
def movers_chart_uri(quotes: list[dict], top: int = 12) -> str | None:
    movers = [q for q in quotes if isinstance(q.get("pct_1d"), (int, float))]
    if len(movers) < 3:
        return None
    movers.sort(key=lambda q: abs(q["pct_1d"]), reverse=True)
    movers = movers[:top][::-1]  # smallest at top so largest ends up on top

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # matplotlib missing -> just skip the chart
        print(f"  chart skipped (matplotlib unavailable): {exc}", file=sys.stderr)
        return None

    labels = [q["ticker"] for q in movers]
    vals = [q["pct_1d"] for q in movers]
    colors = [GAIN if v >= 0 else LOSS for v in vals]

    fig, ax = plt.subplots(figsize=(7.4, 0.34 * len(movers) + 0.7), dpi=150)
    bars = ax.barh(labels, vals, color=colors, height=0.68)
    ax.axvline(0, color="#94a3b8", lw=0.8)
    ax.set_title("Watchlist — biggest 1-day moves (%)", fontsize=12,
                 fontweight="bold", color=INK, loc="left", pad=8)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#cbd5e1")
    ax.tick_params(length=0, labelsize=9, colors=INK)
    ax.set_xlabel("")
    pad = max(abs(min(vals)), abs(max(vals))) * 0.14 + 0.3
    ax.set_xlim(min(vals) - pad * 2.2, max(vals) + pad * 2.2)
    for bar, v in zip(bars, vals):
        ax.text(bar.get_width() + (pad * 0.25 if v >= 0 else -pad * 0.25),
                bar.get_y() + bar.get_height() / 2, f"{v:+.1f}%",
                va="center", ha="left" if v >= 0 else "right",
                fontsize=8.5, color=INK, fontweight="bold")
    ax.margins(y=0.02)
    fig.tight_layout(pad=0.6)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Photos: best-effort Open Graph images from top stories
# ---------------------------------------------------------------------------
# Google News / aggregator / paper hosts don't yield clean article images.
_SKIP_PHOTO_HOSTS = ("news.google.com", "arxiv.org", "rss.arxiv.org",
                     "huggingface.co", "hnrss.org", "news.ycombinator.com",
                     "facebook.com", "reddit.com")


def _og_image(url: str, timeout: int) -> str | None:
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for sel in (("meta", {"property": "og:image"}), ("meta", {"name": "twitter:image"})):
        tag = soup.find(*sel)
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _download_data_uri(img_url: str, timeout: int) -> str | None:
    r = requests.get(img_url, headers={"User-Agent": USER_AGENT}, timeout=timeout, stream=True)
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    if not ctype.startswith("image/"):
        return None
    data = r.content
    if len(data) > 4_000_000 or len(data) < 2_000:  # skip oversized / tracking pixels
        return None
    return f"data:{ctype};base64," + base64.b64encode(data).decode()


def fetch_photos(sources: list[dict], limit: int) -> list[dict]:
    if limit <= 0 or not common.env_flag("BRIEF_PHOTOS", default=True):
        return []
    timeout = int(os.environ.get("BRIEF_PHOTO_TIMEOUT", "6"))
    # Prefer world/markets stories (they carry real photos), from direct publishers.
    order = {"world": 0, "markets": 1, "ai": 2, "research": 3}
    cand = [s for s in sources
            if s.get("link") and not any(h in s["link"] for h in _SKIP_PHOTO_HOSTS)]
    cand.sort(key=lambda s: order.get(s.get("category", "ai"), 9))

    photos: list[dict] = []
    seen: set[str] = set()
    for s in cand:
        if len(photos) >= limit:
            break
        try:
            img = _og_image(s["link"], timeout)
            if not img or img in seen:
                continue
            uri = _download_data_uri(img, timeout)
            if not uri:
                continue
            seen.add(img)
            photos.append({"uri": uri, "caption": s.get("title", ""),
                           "publisher": s.get("publisher", "")})
            print(f"  photo: {s.get('publisher','?')} — {s.get('title','')[:60]}")
        except Exception:
            continue  # any failure -> just try the next story
    return photos


# ---------------------------------------------------------------------------
# Markdown -> HTML
# ---------------------------------------------------------------------------
def md_to_html(md_text: str) -> tuple[str, str, str]:
    """Return (title, footer_html, body_html). The H1 becomes the masthead and
    the trailing '_Generated ..._' provenance line becomes the footer."""
    html = md_lib.markdown(
        md_text,
        extensions=["extra", "sane_lists"],
        output_format="html5",
    )
    soup = BeautifulSoup(html, "html.parser")

    title = "Morning Brief"
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
        h1.decompose()

    # Pull the provenance footer (last <hr> onward) out of the column flow.
    footer_html = ""
    hrs = soup.find_all("hr")
    if hrs:
        last_hr = hrs[-1]
        tail = []
        node = last_hr.next_sibling
        while node is not None:
            nxt = node.next_sibling
            tail.append(str(node))
            node.extract()
            node = nxt
        last_hr.decompose()
        footer_html = "".join(tail).strip()

    # Make every external link open its real URL and carry the link colour.
    for a in soup.find_all("a"):
        a["class"] = a.get("class", []) + ["src"]

    # Compact the Sources section — it's a long link list, so shrink its font
    # and spacing. Tag the Sources <h2> and everything under it up to the next
    # <h2> (so a later Quick Check section is unaffected).
    in_sources = False
    for el in soup.find_all(recursive=False):
        if el.name == "h2":
            in_sources = "source" in el.get_text().lower()
        if in_sources and el.name:
            el["class"] = el.get("class", []) + ["srccompact"]

    # WeasyPrint only honours `column-span: all` on a block box, not on a raw
    # <table>. Wrap each table so it spans both columns instead of fragmenting
    # across the column break.
    for table in soup.find_all("table"):
        wrapper = soup.new_tag("div")
        wrapper["class"] = ["fullspan"]
        table.insert_before(wrapper)
        wrapper.append(table.extract())

    return title, footer_html, str(soup)


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------
def photo_strip_html(photos: list[dict]) -> str:
    if not photos:
        return ""
    cells = []
    for p in photos:
        cap = BeautifulSoup(p["caption"], "html.parser").get_text()[:90]
        pub = BeautifulSoup(p.get("publisher", ""), "html.parser").get_text()[:40]
        cells.append(
            f'<figure class="photo"><img src="{p["uri"]}" alt="">'
            f'<figcaption><b>{pub}</b> — {cap}</figcaption></figure>'
        )
    return f'<div class="photostrip">{"".join(cells)}</div>'


def build_html(title, date, body_html, footer_html, chart_uri, photos, columns=2) -> str:
    date_label = pretty_date(date)
    chart_block = (
        f'<figure class="chart"><img src="{chart_uri}" alt="Watchlist movers chart">'
        f'<figcaption>Approximate 1-day moves from the watchlist snapshot.</figcaption></figure>'
        if chart_uri else ""
    )
    strip = photo_strip_html(photos)
    footer_block = f'<div class="provenance">{footer_html}</div>' if footer_html else ""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
@page {{
  size: Letter; margin: 1.5cm 1.4cm 1.5cm 1.4cm;
  @top-left {{ content: "Morning Brief"; font: 8pt Georgia, serif; color: #94a3b8; }}
  @top-right {{ content: "{date_label}"; font: 8pt Georgia, serif; color: #94a3b8; }}
  @bottom-left {{ content: "{TAGLINE}"; font: 7.5pt 'Helvetica Neue', Arial, sans-serif; color: #94a3b8; }}
  @bottom-right {{ content: "Page " counter(page) " / " counter(pages); font: 8pt 'Helvetica Neue', Arial, sans-serif; color: #94a3b8; }}
}}
* {{ box-sizing: border-box; }}
html {{ font-size: 10.3pt; }}
body {{ margin: 0; color: {INK};
  font-family: 'Helvetica Neue', Arial, 'DejaVu Sans', sans-serif;
  line-height: 1.42; }}

/* Masthead */
.masthead {{ border-bottom: 3px double {INK}; padding-bottom: 8px; margin-bottom: 12px; }}
.masthead .kicker {{ font: 700 8pt 'Helvetica Neue', Arial, sans-serif;
  letter-spacing: 3px; text-transform: uppercase; color: {ACCENT}; }}
.masthead h1 {{ font-family: Georgia, 'DejaVu Serif', serif; font-weight: 700;
  font-size: 30pt; line-height: 1.03; margin: 3px 0 6px; color: {INK}; letter-spacing: -0.5px; }}
.masthead .dateline {{ display: flex; justify-content: space-between;
  border-top: 1px solid #cbd5e1; padding-top: 5px;
  font: 8.5pt Georgia, serif; color: #475569; }}
.masthead .tagline {{ font-style: italic; }}

/* Lead visuals span full width above the columns */
.lead {{ margin: 0 0 12px; }}
figure {{ margin: 0 0 10px; }}
.chart img {{ width: 100%; border: 1px solid #e2e8f0; border-radius: 4px; }}
figure figcaption {{ font-size: 7.6pt; color: #64748b; margin-top: 3px; font-style: italic; }}
.photostrip {{ display: flex; gap: 8px; margin-bottom: 12px; }}
.photostrip .photo {{ flex: 1; margin: 0; }}
.photostrip .photo img {{ width: 100%; height: 96px; object-fit: cover;
  border-radius: 4px; border: 1px solid #e2e8f0; }}
.photostrip figcaption {{ font-size: 7pt; line-height: 1.2; }}

/* Two-column article body */
.article {{ column-count: {columns}; column-gap: 20px; text-align: left; }}
.article h2 {{ column-span: all; font-family: Georgia, 'DejaVu Serif', serif;
  font-size: 14.5pt; color: {INK}; margin: 14px 0 7px;
  padding: 4px 0 4px 9px; border-left: 4px solid {ACCENT};
  background: #f8fafc; break-after: avoid; }}
.article h2:first-of-type {{ margin-top: 0; }}
.article h3 {{ font-size: 10.6pt; font-weight: 700; color: {ACCENT};
  margin: 9px 0 2px; break-after: avoid; }}
.article h4 {{ font-size: 10pt; font-weight: 700; margin: 7px 0 2px; break-after: avoid; }}
.article p {{ margin: 0 0 7px; }}
.article ul, .article ol {{ margin: 0 0 8px; padding-left: 16px; }}
.article li {{ margin: 0 0 3px; }}
.article strong {{ color: {INK}; }}
a.src {{ color: {LINK}; text-decoration: none; border-bottom: 0.6px solid #bcccf5;
  word-break: break-word; }}
blockquote {{ margin: 6px 0; padding: 2px 0 2px 10px; border-left: 3px solid #cbd5e1;
  color: #475569; font-style: italic; }}
code {{ font-family: 'DejaVu Sans Mono', monospace; font-size: 8.6pt;
  background: #f1f5f9; padding: 0 2px; border-radius: 2px; }}

/* Tables span both columns so they stay readable */
.fullspan {{ column-span: all; margin: 4px 0 12px; }}
table {{ width: 100%; border-collapse: collapse; margin: 0; font-size: 8.6pt;
  break-inside: avoid; }}
th, td {{ border: 0.6px solid #cbd5e1; padding: 4px 6px; text-align: left; vertical-align: top; }}
thead th {{ background: #eef2f7; font-weight: 700; }}
tbody tr:nth-child(even) {{ background: #f8fafc; }}

/* Sources section: compact — smaller font, tighter spacing */
h2.srccompact {{ font-size: 12.5pt; margin: 11px 0 4px; }}
.srccompact {{ font-size: 7.9pt; line-height: 1.22; }}
.srccompact ul, .srccompact ol {{ margin: 0 0 3px; padding-left: 12px; }}
.srccompact li {{ margin: 0 0 1px; }}
.srccompact p {{ margin: 0 0 3px; }}
.srccompact h3 {{ font-size: 9pt; margin: 5px 0 1px; }}
.srccompact a.src {{ border-bottom: 0; }}

.provenance {{ column-span: all; margin-top: 14px; padding-top: 6px;
  border-top: 1px solid #e2e8f0; font-size: 7.8pt; color: #94a3b8; }}
.provenance a {{ color: #94a3b8; }}
</style></head>
<body>
  <header class="masthead">
    <div class="kicker">Laksh's Daily Brief</div>
    <h1>{title}</h1>
    <div class="dateline"><span>{date_label}</span><span class="tagline">{TAGLINE}</span></div>
  </header>
  <div class="lead">{chart_block}{strip}</div>
  <main class="article">
    {body_html}
    {footer_block}
  </main>
</body></html>"""


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def rendered_text_len(pdf_file: Path) -> int:
    """Characters of extractable text in a rendered PDF, or -1 if unreadable.

    -1 means "could not check", not "empty": a reader that chokes must not be
    read as content loss and cost us the day's brief.
    """
    try:
        from pypdf import PdfReader

        return sum(len(page.extract_text() or "") for page in PdfReader(str(pdf_file)).pages)
    except Exception as exc:
        print(f"  could not read back {pdf_file.name} to verify: {exc}", file=sys.stderr)
        return -1


def render(md_file: Path, pdf_file: Path, date: str) -> None:
    from weasyprint import HTML

    md_text = md_file.read_text(errors="ignore")
    quotes, sources = load_provenance(date)

    title, footer_html, body_html = md_to_html(md_text)
    # The auto watchlist chart is off by default — big graphs only when they add
    # real signal. Re-enable with BRIEF_MOVERS_CHART=1 if you want it back.
    chart_uri = movers_chart_uri(quotes) if common.env_flag("BRIEF_MOVERS_CHART") else None
    photo_limit = int(os.environ.get("BRIEF_PHOTO_LIMIT", "3"))
    photos = fetch_photos(sources, photo_limit)

    expected = len(BeautifulSoup(body_html + footer_html, "html.parser").get_text())

    def attempt(columns: int) -> int:
        html = build_html(title, date, body_html, footer_html, chart_uri, photos, columns)
        # Keep the assembled HTML next to the PDF for debugging (gitignored).
        try:
            pdf_file.with_suffix(".debug.html").write_text(html)
        except OSError:
            pass
        HTML(string=html, base_url=str(md_file.parent)).write_pdf(str(pdf_file))
        return rendered_text_len(pdf_file)

    got = attempt(2)

    # WeasyPrint drops content out of the multi-column flow on some documents,
    # silently: the PDF is short a section or five and nothing errors. One column
    # is not subject to it; re-rendering 2026-08-07 that way restores the Bottom
    # Line and Sources it shipped without. Trade the magazine layout for a whole
    # brief on the days it happens, and say so in the log.
    if 0 <= got < expected * MIN_RENDERED_TEXT_RATIO:
        print(
            f"::warning::Two-column render kept only {got}/{expected} chars "
            f"({got / expected:.0%}) of the brief; WeasyPrint dropped content. "
            "Re-rendering in one column.",
            flush=True,
        )
        single = attempt(1)
        if single < got:
            # Worse, somehow. Keep the better of the two rather than shipping the
            # regression: re-render restores the file the earlier attempt wrote.
            print(
                f"  one-column render was shorter ({single} chars), keeping the "
                "two-column PDF.",
                file=sys.stderr,
            )
            attempt(2)
        elif single < expected * MIN_RENDERED_TEXT_RATIO:
            print(
                f"::warning::One column still kept only {single}/{expected} chars "
                f"({single / expected:.0%}). Shipping it (a short brief beats none), "
                "but the PDF is incomplete.",
                flush=True,
            )
        else:
            print(f"  one column recovered the full brief ({single} chars).", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    state = common.load_state()

    if state.get("action") == "skipped":
        # Nothing regenerated this run. Make sure `latest` points at today's PDF.
        date = state.get("date") or common.today_str()
        pdf_file = common.dated_pdf_path(date)
        if pdf_file.exists():
            shutil.copyfile(pdf_file, common.latest_pdf_path())
        print("No regeneration this run — skipping PDF render.")
        return 0

    date = state.get("date") or common.today_str()
    md_file = Path(state.get("md") or common.md_path(date))
    pdf_file = Path(state.get("pdf") or common.dated_pdf_path(date))

    if not md_file.exists():
        print(f"ERROR: Markdown not found: {md_file}", file=sys.stderr)
        return 1

    render(md_file, pdf_file, date)
    latest = common.latest_pdf_path()
    shutil.copyfile(pdf_file, latest)

    size_kb = pdf_file.stat().st_size / 1024
    print(f"Wrote {pdf_file} ({size_kb:.0f} KB) and {latest}")

    common.save_state(pdf=str(pdf_file), latest_pdf=str(latest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
