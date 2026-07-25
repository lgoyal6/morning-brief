#!/usr/bin/env python3
"""Generate Laksh's daily AI / tech-infra / markets / geopolitics morning brief.

Pipeline (stage 1 of 3):

1.  Decide today's date (America/Los_Angeles) and where files go.
2.  Duplicate prevention: on a *scheduled* run, if today's Markdown + PDF already
    exist, exit cleanly without regenerating or re-sending. Manual dispatch
    (``workflow_dispatch``) or ``FORCE_REGENERATE=1`` always regenerates.
3.  Fetch current articles from credible RSS feeds + topic-targeted Google News
    searches. Optionally pull a watchlist quote snapshot via yfinance.
4.  Ask an OpenAI-compatible LLM (GMI Cloud by default) to synthesize the brief
    from ONLY those sources -- no browsing, no hallucinated news.
5.  Write ``briefs/YYYY-MM-DD-ai-tech-market-brief.md`` and a sources JSON.

The LLM provider is fully configurable so you can point it at GMI Cloud (an
open-weight model, effectively free with your GMI key), OpenAI, or anything else
that speaks the OpenAI Chat Completions API.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import common

load_dotenv()

# ---------------------------------------------------------------------------
# LLM configuration (OpenAI-compatible). Defaults target GMI Cloud.
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://api.gmi-serving.com/v1"
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"


def resolve_api_key() -> str | None:
    """First non-empty of the accepted key env vars.

    Accepts GMI_API_KEY (preferred), then LLM_API_KEY, then OPENAI_API_KEY so the
    same code works whether the repo secret is named for GMI or OpenAI.
    """
    for name in ("GMI_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(name)
        if val and val.strip():
            return val.strip()
    return None


LLM_BASE_URL = os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "7000"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.4"))

# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
LOOKBACK_HOURS = int(os.environ.get("BRIEF_LOOKBACK_HOURS", "48"))
MAX_ITEMS = int(os.environ.get("BRIEF_MAX_ITEMS", "70"))
PER_FEED_CAP = int(os.environ.get("BRIEF_PER_FEED_CAP", "8"))

# Direct RSS feeds from credible publishers (clean article URLs).
DIRECT_FEEDS = [
    ("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Technology", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("CNBC Business", "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("CNBC Investing", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("CNBC Earnings", "https://www.cnbc.com/id/15839135/device/rss/rss.html"),
    ("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Tom's Hardware", "https://www.tomshardware.com/feeds/all"),
    ("MarketWatch Top Stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Federal Reserve Press", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Hacker News Front Page", "https://hnrss.org/frontpage?points=100"),
]

# Topic-targeted Google News searches. Google News links resolve to the original
# publisher (Reuters, Bloomberg, FT, WSJ, ...), giving broad credible coverage
# without any API key. ``when:2d`` limits to the last two days.
GOOGLE_NEWS_TOPICS = [
    ("AI Infrastructure", "AI data center OR GPU OR Nvidia OR hyperscaler capex"),
    ("Semiconductors", "TSMC OR semiconductor OR chip export controls OR HBM OR foundry"),
    ("Markets & Rates", "stock market OR Federal Reserve interest rates OR earnings guidance"),
    ("Geopolitics", "sanctions OR trade restrictions OR war OR export controls chips"),
    ("Cloud & Enterprise AI", "cloud computing OR enterprise AI OR OpenAI OR Anthropic OR Microsoft AI"),
    ("Data-Center Power", "data center power OR electricity grid OR nuclear energy AI demand"),
]

# Full watchlist shown to the model (verbatim from the brief spec).
WATCHLIST_DISPLAY = (
    "TSM, NEE, CRDO, RGTI, SOFI, SK Hynix ADR, FOTO, NVDA, PLTR, SPCX/SpaceX, MRVL, "
    "COHR, AAOI, LITE, FLNC, RDW, IBM, INFQ/Infleqtion, MU, SNDK, CEG, AMC, TSLA, "
    "GOOGL, META, MSFT, AMZN, AVGO, NET, ALAB, VST, HOOD, COIN, ARM, IMAX, "
    "CNK/Cinemark, CMCSA/Comcast, NFLX, INTC, AMD, QCOM, KLAC, DLR, EQIX, SBUX, NKE, FXAIX"
)

# Subset of the watchlist that maps to valid Yahoo Finance tickers (private
# companies like SpaceX/Infleqtion and messy ADRs are dropped from the quote pull
# but remain in the display watchlist above).
YF_TICKERS = [
    "TSM", "NEE", "CRDO", "RGTI", "SOFI", "NVDA", "PLTR", "MRVL", "COHR", "AAOI",
    "LITE", "FLNC", "RDW", "IBM", "MU", "SNDK", "CEG", "AMC", "TSLA", "GOOGL",
    "META", "MSFT", "AMZN", "AVGO", "NET", "ALAB", "VST", "HOOD", "COIN", "ARM",
    "IMAX", "CNK", "CMCSA", "NFLX", "INTC", "AMD", "QCOM", "KLAC", "DLR", "EQIX",
    "SBUX", "NKE", "FXAIX",
]


# ---------------------------------------------------------------------------
# Source fetching
# ---------------------------------------------------------------------------
def clean_text(html: str | None) -> str:
    if not html:
        return ""
    try:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    except Exception:
        text = html
    return " ".join(text.split())


def google_news_url(query: str) -> str:
    from urllib.parse import quote

    q = quote(f"{query} when:2d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
    return None


def parse_feed(label: str, url: str, cutoff: datetime) -> list[dict]:
    """Parse one feed into normalized item dicts, filtered by recency."""
    try:
        parsed = feedparser.parse(url, agent=USER_AGENT)
    except Exception as exc:  # network / parse failure -> skip this feed
        print(f"  ! {label}: {exc}", file=sys.stderr)
        return []

    items: list[dict] = []
    for entry in parsed.entries:
        dt = entry_datetime(entry)
        if dt is not None and dt < cutoff:
            continue  # too old
        link = entry.get("link") or ""
        title = clean_text(entry.get("title"))
        if not title or not link:
            continue
        # Google News entries expose the real publisher under `source`.
        publisher = ""
        src = entry.get("source")
        if isinstance(src, dict):
            publisher = src.get("title", "")
        items.append(
            {
                "feed": label,
                "publisher": publisher or label,
                "title": title,
                "link": link,
                "published": dt.isoformat() if dt else "",
                "summary": clean_text(entry.get("summary"))[:400],
            }
        )
        if len(items) >= PER_FEED_CAP:
            break
    return items


def fetch_sources() -> list[dict]:
    socket.setdefaulttimeout(int(os.environ.get("BRIEF_FEED_TIMEOUT", "20")))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    feeds = list(DIRECT_FEEDS)
    feeds += [(f"Google News: {label}", google_news_url(q)) for label, q in GOOGLE_NEWS_TOPICS]

    print(f"Fetching {len(feeds)} feeds (lookback {LOOKBACK_HOURS}h)...")
    collected: list[dict] = []
    seen_links: set[str] = set()
    seen_titles: set[str] = set()

    for label, url in feeds:
        items = parse_feed(label, url, cutoff)
        kept = 0
        for it in items:
            link_key = it["link"].split("?")[0]
            title_key = it["title"].lower().strip()
            if link_key in seen_links or title_key in seen_titles:
                continue
            seen_links.add(link_key)
            seen_titles.add(title_key)
            collected.append(it)
            kept += 1
        print(f"  - {label}: {kept} new items")

    # Newest first; undated items sort last.
    collected.sort(key=lambda x: x["published"] or "", reverse=True)
    return collected[:MAX_ITEMS]


# ---------------------------------------------------------------------------
# Watchlist quotes (best-effort; never fatal)
# ---------------------------------------------------------------------------
def fetch_quotes() -> list[dict]:
    if not common.env_flag("ENABLE_QUOTES", default=True):
        print("Quotes disabled (ENABLE_QUOTES=0).")
        return []
    try:
        import yfinance as yf
    except Exception as exc:
        print(f"yfinance unavailable, skipping quotes: {exc}", file=sys.stderr)
        return []

    print(f"Fetching quote snapshot for {len(YF_TICKERS)} tickers (best-effort)...")
    quotes: list[dict] = []
    try:
        data = yf.download(
            YF_TICKERS,
            period="5d",
            interval="1d",
            progress=False,
            threads=True,
            auto_adjust=False,
        )
        closes = data["Close"]
        for ticker in YF_TICKERS:
            try:
                series = closes[ticker].dropna()
                if len(series) < 2:
                    continue
                last = float(series.iloc[-1])
                prev = float(series.iloc[-2])
                pct = (last / prev - 1.0) * 100.0 if prev else 0.0
                quotes.append({"ticker": ticker, "last": round(last, 2), "pct_1d": round(pct, 2)})
            except Exception:
                continue
    except Exception as exc:
        print(f"Quote fetch failed (non-fatal): {exc}", file=sys.stderr)
        return []

    print(f"  got {len(quotes)} quotes")
    return quotes


# ---------------------------------------------------------------------------
# Prior brief context ("what changed from prior run")
# ---------------------------------------------------------------------------
def previous_brief_excerpt(today: str) -> str:
    briefs = sorted(common.BRIEFS_DIR.glob(f"*-{common.BRIEF_SLUG}.md"))
    briefs = [p for p in briefs if not p.name.startswith(today)]
    if not briefs:
        return ""
    text = briefs[-1].read_text(errors="ignore")
    # Prefer the previous "Overall Conclusion" section if present.
    lower = text.lower()
    idx = lower.rfind("overall conclusion")
    excerpt = text[idx:] if idx != -1 else text[-1800:]
    return excerpt[:1800].strip()


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
BRIEF_SPEC = """Produce Laksh's daily morning general-knowledge, AI/tech/cloud infrastructure, markets, and investing brief.

Audience:
Laksh is a student building general knowledge in investing, markets, geopolitics, AI infrastructure, cloud infrastructure, and semiconductor supply chains. Assume low prior knowledge.

Style:
Make it compact but substantive, roughly a 4-6 minute read. Use plain prose, short paragraphs, clear bullets, and causal explanations. Explain technical, market, and geopolitical terms inline with short parentheticals. Go beyond headlines: explain what happened, why it matters, who benefits, who is hurt, second-order effects, and uncertainty.

Primary focus:
1. AI industry and infrastructure: AI labs, hyperscalers, cloud providers, data centers, GPUs/accelerators, networking, memory, power, cooling, enterprise AI adoption.
2. Tech and semiconductor supply chain: TSMC, SK Hynix, Micron, Nvidia, AMD, Broadcom, Marvell, optical networking, equipment, packaging, foundries, power/utilities.
3. Investing and markets: rates, earnings, guidance, valuation, sector rotation, company-specific watchlist moves.
4. Global news/geopolitics: wars, sanctions, elections, policy, trade restrictions, energy shocks, supply-chain disruptions.

Watchlist:
{watchlist}

Required sections:
1. Top World, AI, Tech, and Market News
- 8-12 stories max.
- Each story must include what happened, background, why it matters, and source links.

2. Infrastructure and Supply-Chain Logic
- Pick 2-4 stories and explain deeper mechanisms.
- Example: AI demand -> cloud capex -> GPUs -> HBM -> networking/optics -> power/cooling.

3. Watchlist: Earnings, Guidance, and Notable Movers
- Identify watchlist companies with earnings/calls in last 24 hours.
- Include key numbers vs expectations, guidance, stock reaction.
- Flag roughly +/-3% moves or major company-specific news.
- Explain valuation terms when relevant.

4. Beginner Knowledge Lens
- 4-6 bullets teaching market/general-world patterns.

5. Terms Used Today
- 5-10 terms.
- For 1-3 important terms, include deeper explanation, rough benchmarks, example, and caution.

6. Sources Pulled
- Group links by topic.
- Use credible sources only.

7. Overall Conclusion
- 1-3 short paragraphs synthesizing the big picture and what changed from the prior run.

Retention:
{retention}

Visuals:
Include 0-3 useful visuals only if they genuinely improve understanding. No decoration. If you include a table, use GitHub-flavored Markdown table syntax."""

SYSTEM_ROLE = (
    "You are an expert analyst covering AI infrastructure, the semiconductor supply "
    "chain, global markets, and geopolitics. You write a rigorous daily briefing for a "
    "curious student. You are precise, causal, and never sensational."
)

GUARDRAILS = """CRITICAL SOURCING RULES:
- Use ONLY the sources in the "SOURCE BUNDLE" below. Do NOT invent facts, numbers, quotes, dates, events, or URLs. You are NOT browsing the web.
- LINKS ARE MANDATORY AND MUST BE INLINE. Whenever you use a source, hyperlink it inline as a Markdown link with that source's EXACT url. Example of the required style:
      Tesla fell ~18% after a weak quarter ([CNBC](https://www.cnbc.com/example)).
  Use a short descriptive label (publisher and/or topic). Every story in sections 1-3 must contain at least one such inline [label](url) link.
- ABSOLUTELY NO bare numeric citations. Never write "[3]", "[15][42]", "(source 7)", superscripts, or any references-by-number scheme. The SOURCE numbers below are for your private lookup only — never print them. The only square-bracket syntax allowed in your output is a real Markdown link immediately followed by "(https://...)".
- Section 6 "Sources Pulled": every bullet MUST itself be a clickable Markdown link formatted as `[Publisher — headline](https://...)`, grouped by topic. No naked numbers, and no titles without their URL.
- If the sources do not cover a watchlist name or topic, say so plainly (e.g. "No notable news in the sources for X") instead of fabricating.
- Treat the watchlist quote snapshot as approximate and possibly delayed; attribute price moves to it, not to invented figures.
- Output valid GitHub-flavored Markdown ONLY. Begin directly with a single H1 title line. No preamble, no sign-off, and do NOT wrap the whole document in a code fence."""


def build_source_bundle(sources: list[dict]) -> str:
    # Each source is labelled "SOURCE n" for the model's private lookup only.
    # The model must convert any source it uses into an inline [label](url) link
    # (see GUARDRAILS) and must never print the bare number.
    lines = []
    for i, s in enumerate(sources, 1):
        date = s["published"][:10] if s["published"] else "n/a"
        lines.append(f"SOURCE {i}: {s['title']}")
        lines.append(f"    url: {s['link']}")
        lines.append(f"    publisher: {s['publisher']} | date: {date}")
        if s["summary"]:
            lines.append(f"    summary: {s['summary']}")
        lines.append("")
    return "\n".join(lines)


def build_quotes_block(quotes: list[dict]) -> str:
    if not quotes:
        return "(No quote snapshot available this run.)"
    rows = [f"{q['ticker']}: ${q['last']} ({q['pct_1d']:+.2f}% vs prior close)" for q in quotes]
    return "\n".join(rows)


def build_messages(sources, quotes, today, prior_excerpt, include_quick_check):
    retention = (
        "This run is a retention checkpoint: append a final '## Quick Check' section with "
        "3 short logic questions (numbered) followed by their answers."
        if include_quick_check
        else "Only add a 'Quick Check' section on retention checkpoints (not this run)."
    )
    spec = BRIEF_SPEC.format(watchlist=WATCHLIST_DISPLAY, retention=retention)
    system = f"{SYSTEM_ROLE}\n\n{spec}\n\n{GUARDRAILS}"

    prior = (
        f"\n\nFOR CONTEXT — excerpt from the previous brief (use it for the "
        f"'what changed from the prior run' comparison; do not copy it):\n\"\"\"\n{prior_excerpt}\n\"\"\""
        if prior_excerpt
        else ""
    )
    user = (
        f"Today's date is {today} (America/Los_Angeles). Write today's brief now.\n\n"
        f"WATCHLIST QUOTE SNAPSHOT (approximate, possibly delayed):\n{build_quotes_block(quotes)}\n\n"
        f"SOURCE BUNDLE ({len(sources)} items — cite by their URLs):\n{build_source_bundle(sources)}"
        f"{prior}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------
def list_models(base_url: str, api_key: str) -> list[str]:
    try:
        r = requests.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )
        data = r.json()
        return [m.get("id", "") for m in data.get("data", [])]
    except Exception:
        return []


def call_llm(messages, api_key) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=LLM_BASE_URL, timeout=180.0)
    print(f"Calling {LLM_MODEL} at {LLM_BASE_URL} ...")
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
        )
    except Exception as exc:
        msg = str(exc)
        print(f"\nLLM call failed: {msg}", file=sys.stderr)
        if any(w in msg.lower() for w in ("model", "not found", "404", "does not exist")):
            available = list_models(LLM_BASE_URL, api_key)
            if available:
                print(
                    "\nModels available at this endpoint:\n  "
                    + "\n  ".join(sorted(m for m in available if m)),
                    file=sys.stderr,
                )
                print(
                    f"\nSet LLM_MODEL to one of the above (current: {LLM_MODEL}).",
                    file=sys.stderr,
                )
        raise

    content = resp.choices[0].message.content or ""
    return content.strip()


# ---------------------------------------------------------------------------
# Dry-run stub (no API key needed) — lets you exercise render + Discord.
# ---------------------------------------------------------------------------
def build_dry_run_markdown(sources, quotes, today) -> str:
    out = [
        f"# Morning Brief — {today} (DRY RUN)",
        "",
        "> Generated in **dry-run mode** without calling the LLM. This exists only to "
        "test PDF rendering and Discord delivery. Real runs contain full analysis.",
        "",
        "## 1. Top World, AI, Tech, and Market News",
        "",
    ]
    for s in sources[:12]:
        out.append(f"- **{s['title']}** ({s['publisher']}) — [source]({s['link']})")
    out += ["", "## 3. Watchlist: Notable Movers", ""]
    if quotes:
        out += ["| Ticker | Last | % 1d |", "| --- | --- | --- |"]
        movers = sorted(quotes, key=lambda q: abs(q["pct_1d"]), reverse=True)[:12]
        for q in movers:
            out.append(f"| {q['ticker']} | ${q['last']} | {q['pct_1d']:+.2f}% |")
    else:
        out.append("_No quote snapshot available._")
    out += ["", "## 6. Sources Pulled", ""]
    for s in sources[:20]:
        out.append(f"- [{s['title']}]({s['link']}) ({s['publisher']})")
    out += ["", "## 7. Overall Conclusion", "", "_Dry-run stub — no synthesis performed._", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    today = common.today_str()
    md_file = common.md_path(today)
    pdf_file = common.dated_pdf_path(today)

    event = os.environ.get("GITHUB_EVENT_NAME", "")
    force = common.env_flag("FORCE_REGENERATE") or "--force" in sys.argv
    dry_run = common.env_flag("BRIEF_DRY_RUN") or "--dry-run" in sys.argv

    # Duplicate prevention: two DST cron times run daily, so the second one (and
    # any manual re-trigger of a scheduled run) must not double up.
    already = md_file.exists() and pdf_file.exists()
    if already and event == "schedule" and not force:
        print(f"Brief for {today} already exists and this is a scheduled run — skipping.")
        common.save_state(date=today, action="skipped", md=str(md_file), pdf=str(pdf_file))
        return 0

    common.BRIEFS_DIR.mkdir(parents=True, exist_ok=True)

    sources = fetch_sources()
    quotes = fetch_quotes()

    # Persist provenance (uploaded as an artifact; gitignored so it isn't committed).
    common.sources_path(today).write_text(
        json.dumps({"date": today, "sources": sources, "quotes": quotes}, indent=2)
    )

    if not sources and not dry_run:
        print(
            "ERROR: fetched 0 sources — refusing to generate to avoid hallucinated news.\n"
            "Check network access to the RSS/Google News feeds and retry.",
            file=sys.stderr,
        )
        return 1

    if dry_run:
        print("DRY RUN: skipping LLM call.")
        markdown = build_dry_run_markdown(sources, quotes, today)
    else:
        api_key = resolve_api_key()
        if not api_key:
            print(
                "ERROR: no API key found. Set one of GMI_API_KEY / LLM_API_KEY / OPENAI_API_KEY.",
                file=sys.stderr,
            )
            return 1
        # Retention: add a Quick Check roughly every 4th brief.
        existing = list(common.BRIEFS_DIR.glob(f"*-{common.BRIEF_SLUG}.md"))
        include_quick_check = (len(existing) + 1) % 4 == 0
        prior_excerpt = previous_brief_excerpt(today)
        messages = build_messages(sources, quotes, today, prior_excerpt, include_quick_check)
        markdown = call_llm(messages, api_key)
        if not markdown:
            print("ERROR: LLM returned empty content.", file=sys.stderr)
            return 1

    # Footer for provenance.
    stamp = common.la_now().strftime("%Y-%m-%d %H:%M %Z")
    model_note = "dry-run" if dry_run else f"{LLM_MODEL} via {LLM_BASE_URL}"
    markdown = markdown.rstrip() + (
        f"\n\n---\n_Generated {stamp} · {len(sources)} sources · model: {model_note}_\n"
    )

    md_file.write_text(markdown)
    print(f"Wrote {md_file} ({len(markdown)} chars)")

    common.save_state(
        date=today,
        action="generated",
        md=str(md_file),
        pdf=str(pdf_file),
        dry_run=dry_run,
        source_count=len(sources),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
