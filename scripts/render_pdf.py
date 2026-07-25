#!/usr/bin/env python3
"""Render the generated Markdown brief to a phone-readable PDF (stage 2 of 3).

Uses ReportLab with a small, self-contained Markdown -> flowables converter that
handles headings, ordered/unordered lists (with nesting), GitHub-flavored tables,
blockquotes, horizontal rules, fenced code, local images, and inline formatting
(**bold**, *italic*, `code`, [links](url), and bare URLs). Links stay clickable
AND visible.

Reads the target path from the state file written by generate_brief.py, falling
back to today's date. Writes the dated PDF and copies it to
``briefs/latest-ai-tech-market-brief.pdf``.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import common

# ---------------------------------------------------------------------------
# Page geometry & styles
# ---------------------------------------------------------------------------
PAGE_SIZE = letter
LEFT = RIGHT = 0.75 * inch
TOP = 0.9 * inch
BOTTOM = 0.7 * inch
USABLE_WIDTH = PAGE_SIZE[0] - LEFT - RIGHT

INK = colors.HexColor("#1a1a1a")
LINK = colors.HexColor("#1a56db")
MUTED = colors.HexColor("#64748b")

_base = getSampleStyleSheet()

BODY = ParagraphStyle(
    "Body", parent=_base["BodyText"], fontName="Helvetica",
    fontSize=10.5, leading=15, spaceAfter=6, textColor=INK,
)
H1 = ParagraphStyle(
    "H1", fontName="Helvetica-Bold", fontSize=19, leading=23,
    spaceBefore=4, spaceAfter=10, textColor=colors.HexColor("#0f172a"),
)
H2 = ParagraphStyle(
    "H2", fontName="Helvetica-Bold", fontSize=14, leading=18,
    spaceBefore=14, spaceAfter=5, textColor=colors.HexColor("#1e3a8a"),
)
H3 = ParagraphStyle(
    "H3", fontName="Helvetica-Bold", fontSize=11.5, leading=15,
    spaceBefore=9, spaceAfter=3, textColor=colors.HexColor("#334155"),
)
QUOTE = ParagraphStyle(
    "Quote", parent=BODY, leftIndent=12, textColor=MUTED,
    borderPadding=(0, 0, 0, 6), fontName="Helvetica-Oblique",
)
CODE = ParagraphStyle(
    "Code", fontName="Courier", fontSize=8.5, leading=11,
    backColor=colors.HexColor("#f1f5f9"), borderPadding=6, textColor=INK,
)
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=9, leading=12, spaceAfter=0)
CELL_HEAD = ParagraphStyle("CellHead", parent=CELL, fontName="Helvetica-Bold")

HEADING_STYLES = {1: H1, 2: H2, 3: H3, 4: H3, 5: H3, 6: H3}

# Private-use placeholders used to protect rendered links from later regexes.
_TOK_L, _TOK_R = "", ""


# ---------------------------------------------------------------------------
# Inline Markdown -> ReportLab mini-markup
# ---------------------------------------------------------------------------
def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link_markup(text: str, url: str) -> str:
    return f'<a href="{url}"><font color="#1a56db"><u>{text}</u></font></a>'


def inline(text: str) -> str:
    """Convert inline Markdown to ReportLab paragraph markup (already XML-safe)."""
    text = _xml_escape(text)

    # 1) Markdown links -> tokens (so bare-URL autolinking can't touch them).
    stash: list[str] = []

    def _stash_md_link(m: re.Match) -> str:
        label = m.group(1)
        url = m.group(2).strip()
        stash.append(_link_markup(label, url))
        return f"{_TOK_L}{len(stash) - 1}{_TOK_R}"

    text = re.sub(r"\[([^\]]+)\]\((\S+?)\)", _stash_md_link, text)

    # 2) Autolink bare URLs (trailing sentence punctuation kept outside the link).
    def _autolink(m: re.Match) -> str:
        url = m.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?)":
            trail = url[-1] + trail
            url = url[:-1]
        stash.append(_link_markup(url, url))
        return f"{_TOK_L}{len(stash) - 1}{_TOK_R}{trail}"

    text = re.sub(r"https?://[^\s<]+", _autolink, text)

    # 3) Emphasis + inline code (bold before italic so ** wins over *).
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', text)

    # 4) Restore protected links.
    def _restore(m: re.Match) -> str:
        return stash[int(m.group(1))]

    text = re.sub(f"{_TOK_L}(\\d+){_TOK_R}", _restore, text)
    return text


# ---------------------------------------------------------------------------
# Block-level helpers
# ---------------------------------------------------------------------------
def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and "|" in s and "-" in s and set(s) <= set("|:- ")


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _build_table(header: list[str], rows: list[list[str]]) -> Table:
    ncols = max(len(header), max((len(r) for r in rows), default=0))
    header = (header + [""] * ncols)[:ncols]
    norm_rows = [(r + [""] * ncols)[:ncols] for r in rows]

    head_style = CELL_HEAD if ncols <= 5 else ParagraphStyle("CH2", parent=CELL_HEAD, fontSize=8)
    body_style = CELL if ncols <= 5 else ParagraphStyle("CB2", parent=CELL, fontSize=8)

    data = [[Paragraph(inline(c), head_style) for c in header]]
    data += [[Paragraph(inline(c), body_style) for c in r] for r in norm_rows]

    col_w = USABLE_WIDTH / ncols
    table = Table(data, colWidths=[col_w] * ncols, hAlign="LEFT", repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]
        )
    )
    return table


def _image_flowable(path: str, base_dir: Path):
    img_path = (base_dir / path).resolve() if not Path(path).is_absolute() else Path(path)
    if not img_path.exists():
        return None
    try:
        img = Image(str(img_path))
    except Exception:
        return None
    if img.imageWidth > USABLE_WIDTH:
        ratio = USABLE_WIDTH / img.imageWidth
        img.drawWidth = USABLE_WIDTH
        img.drawHeight = img.imageHeight * ratio
    img.hAlign = "CENTER"
    return img


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
ORDERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
IMAGE_RE = re.compile(r"^!\[(.*?)\]\((.*?)\)\s*$")


def markdown_to_flowables(md_text: str, base_dir: Path) -> list:
    lines = md_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    flow: list = []
    para: list[str] = []
    i, n = 0, len(lines)

    def flush_para():
        if para:
            txt = " ".join(l.strip() for l in para).strip()
            if txt:
                flow.append(Paragraph(inline(txt), BODY))
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```"):
            flush_para()
            i += 1
            code: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence
            flow.append(Preformatted("\n".join(code) or " ", CODE))
            flow.append(Spacer(1, 6))
            continue

        # Table (header row followed by a separator row)
        if "|" in line and i + 1 < n and _is_table_sep(lines[i + 1]):
            flush_para()
            header = _split_row(line)
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            flow.append(_build_table(header, rows))
            flow.append(Spacer(1, 8))
            continue

        # Blank line -> paragraph break
        if not stripped:
            flush_para()
            i += 1
            continue

        # Horizontal rule
        if stripped in ("---", "***", "___") or re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            flush_para()
            flow.append(Spacer(1, 4))
            flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#cbd5e1")))
            flow.append(Spacer(1, 4))
            i += 1
            continue

        # Heading
        m = HEADING_RE.match(stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            flow.append(Paragraph(inline(m.group(2).strip()), HEADING_STYLES[level]))
            i += 1
            continue

        # Image (standalone)
        m = IMAGE_RE.match(stripped)
        if m:
            flush_para()
            img = _image_flowable(m.group(2).strip(), base_dir)
            if img is not None:
                flow.append(Spacer(1, 4))
                flow.append(img)
                if m.group(1).strip():
                    flow.append(Paragraph(inline(m.group(1).strip()),
                                          ParagraphStyle("Cap", parent=BODY, fontSize=8.5,
                                                         textColor=MUTED, alignment=1)))
                flow.append(Spacer(1, 6))
            i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            flush_para()
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            flow.append(Paragraph(inline(" ".join(quote_lines)), QUOTE))
            continue

        # Unordered list item
        m = BULLET_RE.match(line)
        if m:
            flush_para()
            level = len(m.group(1)) // 2
            style = ParagraphStyle(
                f"UL{level}", parent=BODY, leftIndent=14 + level * 16,
                bulletIndent=4 + level * 16, spaceAfter=3,
            )
            flow.append(Paragraph(inline(m.group(2).strip()), style, bulletText="•"))
            i += 1
            continue

        # Ordered list item
        m = ORDERED_RE.match(line)
        if m:
            flush_para()
            level = len(m.group(1)) // 2
            style = ParagraphStyle(
                f"OL{level}", parent=BODY, leftIndent=18 + level * 16,
                bulletIndent=4 + level * 16, spaceAfter=3,
            )
            flow.append(Paragraph(inline(m.group(3).strip()), style, bulletText=f"{m.group(2)}."))
            i += 1
            continue

        # Default: accumulate into current paragraph
        para.append(line)
        i += 1

    flush_para()
    return flow


# ---------------------------------------------------------------------------
# Page furniture (running header / footer)
# ---------------------------------------------------------------------------
def _make_page_decorator(date_label: str):
    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        # Header
        canvas.drawRightString(
            PAGE_SIZE[0] - RIGHT, PAGE_SIZE[1] - 0.55 * inch, f"Morning Brief · {date_label}"
        )
        canvas.setStrokeColor(colors.HexColor("#e2e8f0"))
        canvas.setLineWidth(0.5)
        canvas.line(LEFT, PAGE_SIZE[1] - 0.62 * inch, PAGE_SIZE[0] - RIGHT, PAGE_SIZE[1] - 0.62 * inch)
        # Footer
        canvas.drawString(LEFT, 0.45 * inch, "AI · Tech Infra · Markets · Geopolitics")
        canvas.drawRightString(PAGE_SIZE[0] - RIGHT, 0.45 * inch, f"Page {doc.page}")
        canvas.restoreState()

    return decorate


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def render(md_file: Path, pdf_file: Path, date_label: str) -> None:
    md_text = md_file.read_text(errors="ignore")
    flowables = markdown_to_flowables(md_text, base_dir=md_file.parent)

    doc = SimpleDocTemplate(
        str(pdf_file),
        pagesize=PAGE_SIZE,
        leftMargin=LEFT,
        rightMargin=RIGHT,
        topMargin=TOP,
        bottomMargin=BOTTOM,
        title=f"Morning Brief — {date_label}",
        author="morning-brief-bot",
        subject="AI · Tech Infrastructure · Markets · Geopolitics",
    )
    decorate = _make_page_decorator(date_label)
    doc.build(flowables, onFirstPage=decorate, onLaterPages=decorate)


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
