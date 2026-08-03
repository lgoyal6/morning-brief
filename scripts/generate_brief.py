#!/usr/bin/env python3
"""Generate Laksh's daily AI / tech-infra / markets / geopolitics morning brief.

Pipeline (stage 1 of 3):

1.  Decide today's date (America/Los_Angeles) and where files go.
2.  Duplicate prevention: a scheduled run claims the day's morning-delivery slot
    via a committed marker (``briefs/.delivery.json``). The day's second DST cron
    sees the marker and exits cleanly; the first cron regenerates and stamps it.
    The marker is set only by scheduled runs, so a manual dispatch or a local run
    can never suppress the morning send. ``FORCE_REGENERATE=1`` overrides the skip.
3.  Fetch current articles: optional live web search (Tavily/Brave, if a
    SEARCH_API_KEY is set) merged with credible RSS feeds + topic-targeted
    Google News searches. Optionally pull a watchlist quote snapshot via yfinance.
4.  Ask an OpenAI-compatible LLM (GMI Cloud by default) to synthesize the brief
    from ONLY those sources -- no browsing, no hallucinated news.
5.  Write ``briefs/YYYY-MM-DD-ai-tech-market-brief.md`` and a sources JSON.

The LLM provider is fully configurable so you can point it at GMI Cloud (an
open-weight model, effectively free with your GMI key), OpenAI, or anything else
that speaks the OpenAI Chat Completions API.
"""

from __future__ import annotations

import collections
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import common
import web_search

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
# Output ceiling. The old 7000 default routinely truncated the brief mid-section
# (some days ended mid-sentence, ~3 pages). The four-pillar newspaper below is
# long, so default high; 16000 lets the full brief + Sources finish. Lower via
# LLM_MAX_TOKENS if your endpoint caps completion length. (The Bottom Line is
# ordered before Sources so the synthesis survives even if the tail is clipped.)
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "16000"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.4"))
# Reasoning models (DeepSeek V4 Flash/Pro, etc.) spend part of max_tokens on
# hidden thinking. When the thinking trace eats the whole budget the API returns
# a 200 with an EMPTY content field — no exception — and the day's brief is lost
# (this is what killed 2026-08-03). So: retry, and on each retry give the visible
# answer more room while asking for less thinking.
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))
LLM_MAX_TOKENS_CEILING = int(os.environ.get("LLM_MAX_TOKENS_CEILING", "32000"))
# Sent as reasoning_effort when non-empty. Blank = let the endpoint decide on the
# first attempt; retries step it down explicitly.
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "").strip()
# Ordered high -> low; a retry after an empty completion drops one rung.
REASONING_EFFORT_LADDER = ("max", "high", "medium", "low")

# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
LOOKBACK_HOURS = int(os.environ.get("BRIEF_LOOKBACK_HOURS", "48"))
MAX_ITEMS = int(os.environ.get("BRIEF_MAX_ITEMS", "95"))
PER_FEED_CAP = int(os.environ.get("BRIEF_PER_FEED_CAP", "8"))

# Every source is tagged with a category so the final bundle can be interleaved
# to GUARANTEE each pillar is represented -- otherwise the high-volume tech feeds
# crowd out world news, which is exactly what happened before (an AI-only brief).
#   world    -> wars, great-power relations, elections, major deals, milestones
#   markets  -> rates, earnings, macro, company moves
#   ai       -> AI labs, hyperscalers, data centers, GPUs, semis supply chain
#   research -> new model launches, open-weights releases, papers/findings
CATEGORIES = ("world", "markets", "ai", "research")

# Direct RSS feeds from credible publishers (clean article URLs).
DIRECT_FEEDS = [
    # World / international (the pillar that was missing before).
    ("world", "BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("world", "Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("world", "NPR World", "https://feeds.npr.org/1004/rss.xml"),
    ("world", "Guardian World", "https://www.theguardian.com/world/rss"),
    ("world", "France 24", "https://www.france24.com/en/rss"),
    # Markets / macro.
    ("markets", "CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("markets", "CNBC Business", "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("markets", "CNBC Investing", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("markets", "CNBC Earnings", "https://www.cnbc.com/id/15839135/device/rss/rss.html"),
    ("markets", "CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("markets", "MarketWatch Top Stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("markets", "Federal Reserve Press", "https://www.federalreserve.gov/feeds/press_all.xml"),
    # AI / tech / semis.
    ("ai", "CNBC Technology", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("ai", "TechCrunch", "https://techcrunch.com/feed/"),
    ("ai", "TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("ai", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("ai", "The Verge", "https://www.theverge.com/rss/index.xml"),
    ("ai", "Tom's Hardware", "https://www.tomshardware.com/feeds/all"),
    ("ai", "Hacker News Front Page", "https://hnrss.org/frontpage?points=100"),
    # Research / model launches.
    ("research", "arXiv cs.AI", "https://rss.arxiv.org/rss/cs.AI"),
    ("research", "arXiv cs.LG", "https://rss.arxiv.org/rss/cs.LG"),
    ("research", "Hugging Face Blog", "https://huggingface.co/blog/feed.xml"),
]

# Topic-targeted Google News searches. Google News links resolve to the original
# publisher (Reuters, Bloomberg, FT, WSJ, AP, ...), giving broad credible coverage
# without any API key. ``when:2d`` limits to the last two days.
GOOGLE_NEWS_TOPICS = [
    # World / geopolitics -- the specific relationships and beats Laksh asked for.
    ("world", "Russia-Ukraine War", "Russia Ukraine war ceasefire negotiations frontline"),
    ("world", "Middle East / Israel / Iran", "Israel Gaza Iran Middle East ceasefire strike hostage"),
    ("world", "US-China Relations", "US China relations Taiwan military trade tension"),
    ("world", "India & South Asia", "India geopolitics Pakistan China border trade relations"),
    ("world", "World Leaders & Statements", "world leader remarks controversy summit sanctions election"),
    # Markets / deals / milestones (e.g. \"Apple overtakes Nvidia\").
    ("markets", "Markets & Rates", "stock market OR Federal Reserve interest rates OR earnings guidance"),
    ("markets", "Major Deals & M&A", "merger OR acquisition OR takeover OR IPO billion deal"),
    # AI infrastructure / semiconductors.
    ("ai", "AI Infrastructure", "AI data center OR GPU OR Nvidia OR hyperscaler capex"),
    ("ai", "Semiconductors", "TSMC OR semiconductor OR chip export controls OR HBM OR foundry"),
    ("ai", "Cloud & Enterprise AI", "OpenAI OR Anthropic OR Microsoft AI OR Google DeepMind OR cloud AI"),
    ("ai", "Data-Center Power", "data center power OR electricity grid OR nuclear energy AI demand"),
    # New models / research findings.
    ("research", "New Model Launches", "new AI model release open weights benchmark parameters context window"),
    ("research", "AI Research Findings", "AI research paper breakthrough results reasoning agents findings"),
    # Secondary beats (lighter touch; ride inside the pillar they fit best).
    ("world", "US Politics & Policy", "US Congress Senate election policy regulation White House"),
    ("world", "Defense & Military Tech", "defense drones Pentagon military technology NATO budget"),
    ("markets", "Crypto & Fintech", "bitcoin OR ethereum crypto regulation stablecoin fintech ETF"),
    ("research", "Science & Space", "NASA space astronomy physics science breakthrough discovery"),
]

# Full watchlist shown to the model (verbatim from the brief spec).
WATCHLIST_DISPLAY = (
    "TSM (TSMC), NEE (NextEra Energy), CRDO (Credo), RGTI (Rigetti Computing), "
    "SOFI (SoFi), SK Hynix (ADR), FOTO, NVDA (Nvidia), PLTR (Palantir), SpaceX (SPCX), "
    "MRVL (Marvell), COHR (Coherent), AAOI (Applied Optoelectronics), LITE (Lumentum), "
    "FLNC (Fluence Energy), RDW (Redwire), IBM, INFQ (Infleqtion), MU (Micron), "
    "SNDK (SanDisk), CEG (Constellation Energy), AMC (AMC Entertainment), TSLA (Tesla), "
    "GOOGL (Alphabet), META (Meta), MSFT (Microsoft), AMZN (Amazon), AVGO (Broadcom), "
    "NET (Cloudflare), ALAB (Astera Labs), VST (Vistra), HOOD (Robinhood), COIN (Coinbase), "
    "ARM (Arm Holdings), IMAX, CNK (Cinemark), CMCSA (Comcast), NFLX (Netflix), INTC (Intel), "
    "AMD, QCOM (Qualcomm), KLAC (KLA Corp), DLR (Digital Realty), EQIX (Equinix), "
    "SBUX (Starbucks), NKE (Nike), FXAIX (Fidelity 500 Index Fund)"
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


def parse_feed(category: str, label: str, url: str, cutoff: datetime) -> list[dict]:
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
                "category": category,
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

    collected: list[dict] = []
    seen_links: set[str] = set()
    seen_titles: set[str] = set()

    def add(items: list[dict]) -> int:
        added = 0
        for it in items:
            link_key = it["link"].split("?")[0]
            title_key = it["title"].lower().strip()
            if not link_key or link_key in seen_links or title_key in seen_titles:
                continue
            seen_links.add(link_key)
            seen_titles.add(title_key)
            collected.append(it)
            added += 1
        return added

    def within_window(items: list[dict]) -> list[dict]:
        """Drop anything published before ``cutoff``.

        RSS is already filtered inside parse_feed, but search results arrive
        unfiltered and providers treat recency as a hint at best -- Tavily's
        coarse time_range silently widened a 48h ask to a full week, so two
        thirds of search items were 2-7 days old. Gate them on the same cutoff
        the feeds use. Undated items are kept: we cannot prove them stale, and
        they sort last regardless.
        """
        fresh = []
        for it in items:
            stamp = it.get("published") or ""
            if not stamp:
                fresh.append(it)
                continue
            try:
                dt = datetime.fromisoformat(stamp)
            except ValueError:
                fresh.append(it)
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                fresh.append(it)
        return fresh

    # 1) Live web search (optional). Runs first so its on-topic results win
    #    dedupe ties and are never dropped by the MAX_ITEMS cap below.
    if web_search.search_enabled():
        try:
            found = web_search.fetch_web_search(LOOKBACK_HOURS)
            recent = within_window(found)
            stale = len(found) - len(recent)
            web_kept = add(recent)
            print(
                f"  web search: {web_kept} new items "
                f"({stale} dropped as older than {LOOKBACK_HOURS}h)"
            )
        except Exception as exc:  # defensive: never let search break the brief
            print(f"Web search failed (non-fatal): {exc}", file=sys.stderr)
    else:
        print("Web search off (no SEARCH_API_KEY / SEARCH_PROVIDER=none) -- RSS feeds only.")

    # 2) Fixed RSS + Google News feeds (always on; the reliable baseline).
    feeds = list(DIRECT_FEEDS)
    feeds += [(cat, f"Google News: {label}", google_news_url(q)) for cat, label, q in GOOGLE_NEWS_TOPICS]
    print(f"Fetching {len(feeds)} feeds (lookback {LOOKBACK_HOURS}h)...")
    for cat, label, url in feeds:
        print(f"  - [{cat}] {label}: {add(parse_feed(cat, label, url, cutoff))} new items")

    # Interleave by category so the bundle stays balanced across pillars and the
    # MAX_ITEMS cap trims each category's tail evenly -- instead of the high-volume
    # tech/markets feeds burying world news (the old failure mode). Within each
    # category it is strictly newest-first: ranking web search above RSS used to
    # put week-old search hits ahead of six-hour-old wire copy.
    buckets: dict[str, list[dict]] = {}
    for it in collected:
        buckets.setdefault(it.get("category", "ai"), []).append(it)
    for cat in buckets:
        buckets[cat].sort(key=lambda x: x["published"] or "", reverse=True)

    # Round-robin across a stable category order (known categories first).
    order = [c for c in CATEGORIES if c in buckets] + [c for c in buckets if c not in CATEGORIES]
    ordered: list[dict] = []
    depth = 0
    while any(depth < len(buckets[c]) for c in order):
        for c in order:
            if depth < len(buckets[c]):
                ordered.append(buckets[c][depth])
        depth += 1

    kept = ordered[:MAX_ITEMS]
    by_cat = {c: sum(1 for it in kept if it.get("category", "ai") == c) for c in order}
    print(f"  kept {len(kept)}/{len(collected)} items by category: {by_cat}")
    return kept


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
    # Prefer the previous "Bottom Line" / "Overall Conclusion" section if present.
    lower = text.lower()
    idx = max(lower.rfind("bottom line"), lower.rfind("overall conclusion"))
    excerpt = text[idx:] if idx != -1 else text[-1800:]
    return excerpt[:1800].strip()


# ---------------------------------------------------------------------------
# Term memory: spaced-repetition teaching across briefs
# ---------------------------------------------------------------------------
# Laksh reads this daily, so a term should be TAUGHT IN FULL (definition +
# example + use case) the first few times it appears, then only REFERENCED once
# it's been taught enough. We reconstruct each term's exposure count from the
# "Terms & Concepts" sections of prior briefs.
MASTERED_AFTER = int(os.environ.get("BRIEF_TERM_MASTERED_AFTER", "4"))
_TERM_STOPWORDS = {
    "example", "caution", "use case", "benchmark", "benchmarks", "why it matters",
    "the chain", "what happened", "concept spotlight", "deep dive", "bottom line",
    "note", "the mechanism", "the tension", "deeper term", "rough benchmark",
    "rough benchmarks", "who benefits", "who is hurt", "uncertainty", "background",
    "second-order effect", "second-order effects", "takeaway", "the takeaway",
}


def _iter_term_names(text: str):
    """Yield the bolded term names inside a brief's Terms/Concepts section."""
    in_terms = False
    for line in text.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if h:
            level, title = len(h.group(1)), h.group(2)
            if level <= 2:  # only H1/H2 open or close the section
                in_terms = "term" in title.lower() or "concept" in title.lower()
            elif in_terms:  # an H3 inside the section is itself likely a term
                t = re.sub(r"(?i)\b(deep dive|concept spotlight|spotlight)\b[:\-\s]*", "", title).strip()
                if t:
                    yield t
            continue
        if in_terms:
            for m in re.finditer(r"\*\*(.+?)\*\*", line):
                yield m.group(1)


def _term_key(raw: str) -> str:
    # Normalize "HBM (High-Bandwidth Memory):" -> "hbm" for dedupe/counting.
    key = re.split(r"[(:—]|--", raw)[0]
    return key.strip().strip("*").strip().lower().rstrip(" .")


def taught_terms() -> tuple[list[str], list[str]]:
    """Return (reinforce, mastered) display names from prior briefs.

    reinforce -> seen 1..MASTERED_AFTER-1 times: re-teach in full.
    mastered  -> seen >= MASTERED_AFTER times: reference only, don't re-define.
    """
    counts: collections.Counter[str] = collections.Counter()
    display: dict[str, str] = {}
    for p in sorted(common.BRIEFS_DIR.glob(f"*-{common.BRIEF_SLUG}.md")):
        text = p.read_text(errors="ignore")
        seen_here: set[str] = set()
        for raw in _iter_term_names(text):
            key = _term_key(raw)
            if not (2 <= len(key) <= 50) or key in _TERM_STOPWORDS or key in seen_here:
                continue
            seen_here.add(key)
            counts[key] += 1
            display.setdefault(key, re.split(r":|—|--", raw.strip().strip("*"))[0].strip())
    reinforce = sorted(display[k] for k, c in counts.items() if 1 <= c < MASTERED_AFTER)
    mastered = sorted(display[k] for k, c in counts.items() if c >= MASTERED_AFTER)
    return reinforce, mastered


def build_term_memory_block(reinforce: list[str], mastered: list[str]) -> str:
    if not reinforce and not mastered:
        return (
            "TERM MEMORY: This is an early brief — no terms taught yet. In '## 8. Terms & "
            "Concepts', fully explain every term you use (definition + concrete example + real "
            "use case + rough numbers), because Laksh is seeing them for the first time."
        )
    reinforce_s = ", ".join(reinforce) if reinforce else "(none yet)"
    mastered_s = ", ".join(mastered) if mastered else "(none yet)"
    return (
        "TERM MEMORY (spaced repetition — Laksh reads daily, so teach cumulatively):\n"
        f"- STILL LEARNING (explain these IN FULL again whenever they come up — definition + a "
        f"concrete example + a real use case + rough numbers/benchmarks): {reinforce_s}.\n"
        f"- MASTERED (Laksh already knows these from ~{MASTERED_AFTER}+ prior briefs — just USE or "
        f"briefly reference them, do NOT re-define): {mastered_s}.\n"
        "- Any term NOT listed above is NEW: introduce it in full the first time, and keep "
        "re-teaching it in full for its next few appearances before it graduates to 'mastered'.\n"
        "Always prefer teaching properly (examples + use cases) over terse definitions."
    )


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
BRIEF_SPEC = """Produce Laksh's daily morning newspaper: a personal front-page that makes him smarter about the WORLD and about MARKETS/AI to invest well. It is NOT an AI-infra-only newsletter -- world news and geopolitics lead.

Audience:
Laksh is a student building durable knowledge in world affairs, geopolitics, markets/investing, AI infrastructure, and the semiconductor supply chain. He reads this EVERY morning, so knowledge compounds day over day. Start from modest prior knowledge but assume he remembers what earlier briefs taught (see the TERM MEMORY block).

Voice & style:
Write like a sharp, plain-spoken newspaper -- confident, causal, never sensational or hype-y. Short paragraphs and clean bullets. For every story go beyond the headline: what happened, the background a newcomer needs, why it matters, who wins/loses, second-order effects, and what's still uncertain. This should be a substantive read (roughly 10-15 minutes); be comprehensive, but every sentence must earn its place.

Ticker clarity: the FIRST time you name a company by its stock ticker, give the company name too, unless it is a household name (Apple, Tesla, Nvidia, Microsoft, Amazon, Google/Alphabet, Meta, Netflix, Intel, AMD). Write it as "Company (TICKER)" -- e.g. "Credo (CRDO)", "Vistra (VST)", "Constellation Energy (CEG)", "Astera Labs (ALAB)". Never leave a non-obvious ticker unexplained.

Coverage -- give these FOUR pillars roughly equal weight every day:
A. World & Geopolitics (LEAD PILLAR): wars and conflicts, great-power relations, diplomacy, elections, major international deals, and notable/outrageous statements by leaders. PRIORITIZE these relationships when there's news: US-China (tech war, Taiwan, trade), the Middle East (Israel, Iran, Gulf, oil), and India & South Asia. Cover Russia-Ukraine/Europe and others when genuinely major. Weave in the secondary beats lightly when there's real news: US politics & policy, and defense & military tech.
B. Markets, Money & Deals: indices, rates, macro, big earnings, mergers/acquisitions, and market milestones (e.g. one company's market cap overtaking another's). Weave in crypto & fintech lightly.
C. AI & Infrastructure (investing lens): AI labs, hyperscalers, cloud, data centers, GPUs/accelerators, memory (HBM), networking/optics, power/cooling, semiconductor supply chain (TSMC, SK Hynix, Micron, Nvidia, AMD, Broadcom, Marvell), enterprise AI adoption -- always framed so Laksh learns to invest.
D. Model & Research Watch: NEW model launches (give parameter counts, context length, benchmark scores, price, open- vs closed-weights when known), notable research findings/papers and why they matter, and a short "Science & Frontier Tech" note for major non-AI science/space breakthroughs.

Watchlist:
{watchlist}

Required sections (use these H2 titles, in this order):

## 1. World & Geopolitics
- 6-9 stories. Lead with the single most important thing happening in the world today. Prioritize the relationships above.

## 2. Markets, Money & Deals
- 5-8 stories: the market tape (what moved and why), notable earnings, big deals/M&A, milestones, and a light crypto touch.

## 3. AI & Infrastructure
- 4-7 stories through an investing lens, with the causal "why it matters for the buildout" spelled out.

## 4. Model & Research Watch
- New model launches with concrete specs (params, context, benchmarks, price), key papers/findings, and a brief Science & Frontier Tech note.

## 5. Watchlist: Earnings, Guidance & Movers
- Watchlist companies with earnings/news in the last 24h: key numbers vs expectations, guidance, stock reaction. Flag roughly +/-3% moves. Treat the quote snapshot as approximate/delayed. Say plainly when the sources have nothing on a name rather than inventing. Refer to each company by name with its ticker in parentheses (e.g. "Credo (CRDO)"), especially for non-obvious tickers -- do not use bare tickers.

## 6. How It Connects (Infrastructure & Supply-Chain Logic)
- 2-3 deep causal chains linking the day's stories. Example: AI demand -> cloud capex -> GPUs -> HBM -> networking/optics -> power/cooling.

## 7. Building Your Knowledge
- 4-6 bullets teaching durable patterns (how markets, geopolitics, or the AI buildout actually work), pitched slightly higher each day as Laksh's knowledge grows.

## 8. Terms & Concepts
- Follow the TERM MEMORY rules below EXACTLY. Lead with ONE "Concept Spotlight": a single concept explained in depth (what it is, a concrete example, a real use case, rough benchmarks/numbers, and a caution/common misconception). Then 4-8 shorter term entries.

## 9. Bottom Line
- 1-3 short paragraphs synthesizing the big picture and explicitly noting what CHANGED since the previous brief. (Write this BEFORE the Sources list so it is never the thing that gets cut.)

## 10. Sources
- List ONLY the stories you actually cited above (aim for ~15-25 bullets, not every source provided), grouped by pillar. Keep it compact. Every bullet is itself a clickable Markdown link `[Publisher -- headline](https://...)`.

Retention:
{retention}

Visuals:
The PDF layout adds its own charts. In the Markdown, include a GitHub-flavored Markdown table only when it genuinely aids understanding (e.g. the watchlist movers, or a model-spec comparison). Do not add decorative filler."""

SYSTEM_ROLE = (
    "You are the editor of a sharp personal daily newspaper. You cover world affairs and "
    "geopolitics FIRST, then markets/investing, AI infrastructure, the semiconductor supply "
    "chain, and new AI models/research. You write for a curious student who reads you every "
    "morning, so you teach cumulatively and never condescend. You are precise, causal, "
    "plain-spoken, and never sensational."
)

GUARDRAILS = """CRITICAL SOURCING RULES:
- Use ONLY the sources in the "SOURCE BUNDLE" below. Do NOT invent facts, numbers, quotes, dates, events, or URLs. You are NOT browsing the web.
- LINKS ARE MANDATORY AND MUST BE INLINE. Whenever you use a source, hyperlink it inline as a Markdown link with that source's EXACT url. Example of the required style:
      Tesla fell ~18% after a weak quarter ([CNBC](https://www.cnbc.com/example)).
  Use a short descriptive label (publisher and/or topic). Every news story in sections 1-5 must contain at least one such inline [label](url) link.
- ABSOLUTELY NO bare numeric citations. Never write "[3]", "[15][42]", "(source 7)", superscripts, or any references-by-number scheme. The SOURCE numbers below are for your private lookup only — never print them. The only square-bracket syntax allowed in your output is a real Markdown link immediately followed by "(https://...)".
- The "Sources" section: every bullet MUST itself be a clickable Markdown link formatted as `[Publisher — headline](https://...)`, grouped by pillar. No naked numbers, and no titles without their URL.
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


def build_messages(sources, quotes, today, prior_excerpt, include_quick_check, term_memory):
    retention = (
        "This run is a retention checkpoint: append a final '## Quick Check' section with "
        "3 short logic questions (numbered) drawn from today's brief, followed by their answers."
        if include_quick_check
        else "Do NOT add a 'Quick Check' section this run."
    )
    spec = BRIEF_SPEC.format(watchlist=WATCHLIST_DISPLAY, retention=retention)
    system = f"{SYSTEM_ROLE}\n\n{spec}\n\n{GUARDRAILS}"

    prior = (
        f"\n\nFOR CONTEXT — excerpt from the previous brief (use it for the "
        f"'what changed since the previous brief' comparison; do not copy it):\n\"\"\"\n{prior_excerpt}\n\"\"\""
        if prior_excerpt
        else ""
    )
    user = (
        f"Today's date is {today} (America/Los_Angeles). Write today's brief now.\n\n"
        f"{term_memory}\n\n"
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


def describe_completion(resp) -> str:
    """finish_reason + token counts, so an empty completion is diagnosable.

    Without this an empty response is indistinguishable from a content filter,
    a truncation, or a provider hiccup — you only see "empty" in the log.
    """
    choice = resp.choices[0] if getattr(resp, "choices", None) else None
    bits = [f"finish_reason={getattr(choice, 'finish_reason', None)}"]
    usage = getattr(resp, "usage", None)
    if usage is not None:
        bits.append(f"prompt_tokens={getattr(usage, 'prompt_tokens', None)}")
        bits.append(f"completion_tokens={getattr(usage, 'completion_tokens', None)}")
        details = getattr(usage, "completion_tokens_details", None)
        reasoning = getattr(details, "reasoning_tokens", None) if details else None
        if reasoning is not None:
            bits.append(f"reasoning_tokens={reasoning}")
    return " ".join(str(b) for b in bits)


def reasoning_text(choice) -> str:
    """The hidden thinking trace, if the provider exposed one.

    DeepSeek-style endpoints return it as ``reasoning_content``; OpenAI-compatible
    gateways often park it in the model's extra fields instead.
    """
    msg = getattr(choice, "message", None)
    if msg is None:
        return ""
    text = getattr(msg, "reasoning_content", None)
    if not text:
        extra = getattr(msg, "model_extra", None) or {}
        text = extra.get("reasoning_content") or extra.get("reasoning") or ""
    return (text or "").strip()


def looks_like_brief(text: str) -> bool:
    """True if ``text`` is the finished brief rather than a thinking trace.

    Used to salvage a run where the whole answer landed in ``reasoning_content``.
    Deliberately strict: publishing a model's scratchpad as the morning brief is
    worse than failing, so require real length and most of the section headers.
    """
    if len(text) < 4000:
        return False
    headers = len(re.findall(r"^##\s+\d+\.", text, flags=re.MULTILINE))
    return headers >= 6


def step_down_effort(current: str) -> str:
    """Next rung down the reasoning ladder — less thinking, more visible output."""
    if current in REASONING_EFFORT_LADDER:
        idx = REASONING_EFFORT_LADDER.index(current)
        return REASONING_EFFORT_LADDER[min(idx + 1, len(REASONING_EFFORT_LADDER) - 1)]
    # Unset/unknown: the first retry asks explicitly for the low end.
    return "low"


def call_llm(messages, api_key) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=LLM_BASE_URL, timeout=180.0)
    max_tokens = LLM_MAX_TOKENS
    effort = LLM_REASONING_EFFORT
    send_effort = bool(effort)

    for attempt in range(1, LLM_MAX_RETRIES + 1):
        knobs = f"max_tokens={max_tokens}"
        if send_effort:
            knobs += f", reasoning_effort={effort}"
        print(
            f"Calling {LLM_MODEL} at {LLM_BASE_URL} "
            f"(attempt {attempt}/{LLM_MAX_RETRIES}, {knobs}) ...",
            flush=True,
        )

        kwargs = {
            "model": LLM_MODEL,
            "messages": messages,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": max_tokens,
        }
        if send_effort:
            kwargs["extra_body"] = {"reasoning_effort": effort}

        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as exc:
            msg = str(exc)
            print(f"\nLLM call failed: {msg}", file=sys.stderr)
            low = msg.lower()
            # A bad model name never fixes itself — report and stop.
            if any(w in low for w in ("model", "not found", "404", "does not exist")):
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
            # An endpoint that rejects reasoning_effort should not cost us the run.
            if send_effort and "reasoning_effort" in low:
                print(
                    "  endpoint rejected reasoning_effort — retrying without it.",
                    file=sys.stderr,
                )
                send_effort = False
                continue
            if attempt == LLM_MAX_RETRIES:
                raise
            backoff = 5 * attempt
            print(f"  retrying in {backoff}s ...", file=sys.stderr, flush=True)
            time.sleep(backoff)
            continue

        choice = resp.choices[0]
        content = (choice.message.content or "").strip()
        print(f"  {describe_completion(resp)} content_chars={len(content)}")
        if content:
            return content

        # 200 OK, empty content. Say why, loudly, then try to recover.
        thinking = reasoning_text(choice)
        print(
            f"  WARNING: empty content on attempt {attempt} "
            f"(reasoning_content chars={len(thinking)}).",
            file=sys.stderr,
        )
        if looks_like_brief(thinking):
            print(
                "  reasoning_content contains the finished brief — using it.",
                file=sys.stderr,
            )
            return thinking

        if attempt == LLM_MAX_RETRIES:
            break

        # Most likely the thinking trace consumed the whole budget: raise the
        # ceiling and ask for less thinking so the answer has somewhere to go.
        max_tokens = min(int(max_tokens * 1.5), LLM_MAX_TOKENS_CEILING)
        effort = step_down_effort(effort)
        send_effort = True
        print(
            f"  retrying with max_tokens={max_tokens}, reasoning_effort={effort} ...",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(5 * attempt)

    return ""


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
    is_scheduled = event == "schedule"
    force = common.env_flag("FORCE_REGENERATE") or "--force" in sys.argv
    dry_run = common.env_flag("BRIEF_DRY_RUN") or "--dry-run" in sys.argv

    # Duplicate prevention across the two daily DST crons. A scheduled run claims
    # the day's morning-delivery slot via a COMMITTED marker (see
    # common.mark_scheduled_delivery, set below once generation succeeds), so only
    # the *second* scheduled cron short-circuits. We deliberately key off that
    # marker rather than "do today's files already exist": a manual dispatch or a
    # late-night local run may have committed today's .md/.pdf, and that must NOT
    # suppress the real morning send.
    if is_scheduled and common.scheduled_delivered_date() == today and not force:
        print(f"Scheduled brief for {today} already delivered — skipping.")
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
        # Retention cadence between "balanced" (weekly) and "heavy" (daily):
        # a Quick Check every 2nd brief. Override with BRIEF_QUIZ_EVERY.
        existing = list(common.BRIEFS_DIR.glob(f"*-{common.BRIEF_SLUG}.md"))
        quiz_every = max(1, int(os.environ.get("BRIEF_QUIZ_EVERY", "2")))
        include_quick_check = (len(existing) + 1) % quiz_every == 0
        reinforce, mastered = taught_terms()
        term_memory = build_term_memory_block(reinforce, mastered)
        print(f"Term memory: {len(reinforce)} still-learning, {len(mastered)} mastered.")
        prior_excerpt = previous_brief_excerpt(today)
        messages = build_messages(
            sources, quotes, today, prior_excerpt, include_quick_check, term_memory
        )
        markdown = call_llm(messages, api_key)
        if not markdown:
            print(
                f"ERROR: LLM returned empty content on all {LLM_MAX_RETRIES} attempts.\n"
                "See the finish_reason / token counts above. If reasoning_tokens is at "
                "the max_tokens ceiling, the thinking trace is eating the whole budget: "
                "raise LLM_MAX_TOKENS_CEILING or pin LLM_REASONING_EFFORT=low.",
                file=sys.stderr,
            )
            return 1

    # Footer for provenance.
    stamp = common.la_now().strftime("%Y-%m-%d %H:%M %Z")
    model_note = "dry-run" if dry_run else f"{LLM_MODEL} via {LLM_BASE_URL}"
    markdown = markdown.rstrip() + (
        f"\n\n---\n_Generated {stamp} · {len(sources)} sources · model: {model_note}_\n"
    )

    md_file.write_text(markdown)
    print(f"Wrote {md_file} ({len(markdown)} chars)")

    # Claim today's morning-delivery slot so the day's second DST cron skips.
    # Only scheduled runs do this — manual/local runs never consume the slot, so
    # iterating on the pipeline can't block the next scheduled send. The marker is
    # committed by the workflow's commit step (it is not gitignored).
    if is_scheduled:
        common.mark_scheduled_delivery(today)
        print(f"Claimed scheduled morning-delivery slot for {today}.")

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
