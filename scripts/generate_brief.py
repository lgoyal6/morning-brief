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
MAX_ITEMS = int(os.environ.get("BRIEF_MAX_ITEMS", "130"))
# Minimum slots reserved per feed within a category before the remainder is
# ranked purely on recency -- keeps narrow beats alive against high-volume wires.
FEED_FLOOR = int(os.environ.get("BRIEF_FEED_FLOOR", "2"))
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
    # Practitioner chatter: what builders actually run, swap and complain about.
    # This is the layer the institutional press misses -- it is where "just use
    # this open-weight model instead" circulates. X and LinkedIn have no usable
    # feed, but the same conversation lands here within a day.
    ("ai", "r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=day"),
    ("ai", "Hacker News: AI Show/Ask", "https://hnrss.org/newest?q=LLM+OR+model+OR+inference&points=40"),
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
    ("ai", "Practical AI & Model Routing", "open source model alternative OR model routing OR inference cost per token OR run LLM locally OR self-host"),
    # Secondary beats (lighter touch; ride inside the pillar they fit best).
    ("world", "US Politics & Policy", "US Congress Senate election policy regulation White House"),
    ("world", "Defense & Military Tech", "defense drones Pentagon military technology NATO budget"),
    ("markets", "Crypto & Fintech", "bitcoin OR ethereum crypto regulation stablecoin fintech ETF"),
    ("research", "Science & Space", "NASA space astronomy physics science breakthrough discovery"),
]

# The standing world beats above are all about how countries deal with EACH
# OTHER, so what happens INSIDE a country -- protests, courts, parliaments,
# domestic economy -- never surfaced. (Asked "what's happening with the protests
# in India", the brief had nothing: its India query was
# "India geopolitics Pakistan China border trade relations".) Rotate through the
# majors so each gets real coverage every few days without crowding the front.
DOMESTIC_ROTATION = [
    ("India", "India protest OR farmers OR parliament OR Supreme Court OR state election OR economy"),
    ("United States", "US domestic protest OR Congress OR Supreme Court ruling OR state politics OR economy"),
    ("China", "China domestic economy OR property crisis OR youth unemployment OR local protest OR policy"),
    ("European Union", "Europe domestic election OR protest OR strike OR energy prices OR immigration policy"),
    ("Global South", "Brazil OR Indonesia OR Nigeria OR South Africa domestic politics election economy protest"),
]
DOMESTIC_SLOTS = int(os.environ.get("BRIEF_DOMESTIC_SLOTS", "2"))


def rotating_domestic_topics(today: str) -> list[tuple[str, str, str]]:
    """Pick today's domestic beats, walking the rotation by day-of-year.

    Two slots out of five countries means any one of them comes round roughly
    every two-and-a-half days -- frequent enough to follow a running story.
    """
    try:
        doy = datetime.strptime(today, "%Y-%m-%d").timetuple().tm_yday
    except ValueError:
        doy = 0
    n = len(DOMESTIC_ROTATION)
    picks = [DOMESTIC_ROTATION[(doy + i) % n] for i in range(min(DOMESTIC_SLOTS, n))]
    return [("world", f"Domestic: {name}", query) for name, query in picks]


# ---------------------------------------------------------------------------
# Foundations curriculum
# ---------------------------------------------------------------------------
# The term-memory system only ever taught vocabulary the day's news happened to
# raise, which skews to whatever is in the headlines: one recent brief taught
# Forward Guidance, Free Cash Flow, Sovereign AI, open-weights, agent benchmarks
# and token efficiency -- and no geopolitics, no market mechanics at all. So
# "what is shorting a stock" could never come up: no wire story stops to explain
# it. This is the proactive half -- a fixed syllabus delivered regardless of the
# news, ordered so each entry builds on the ones above it.
#
# Entries are taught in the same markdown shape as news terms, so taught_terms()
# picks them up automatically and they graduate to "mastered" like anything else.
FOUNDATIONS_MARKETS = [
    ("Share (Stock)", "what owning a fraction of a company actually entitles you to"),
    ("Stock Exchange and Ticker Symbol", "where shares trade and how they're named"),
    ("Broker and Brokerage Account", "how an ordinary person actually buys a share, and what a broker earns"),
    ("Bid, Ask and Spread", "the two prices always quoted, and who pockets the difference"),
    ("Market Order vs Limit Order", "the two basic ways to place a trade and how each can burn you"),
    ("Market Capitalisation", "price times shares outstanding; why it, not share price, measures size"),
    ("Stock Index", "S&P 500, Nasdaq, Dow, Nifty 50 -- what an index actually measures"),
    ("Going Long vs Going Short", "betting a stock rises vs falls; how shorting works mechanically and why losses are unlimited"),
    ("Short Squeeze", "why crowded short positions can detonate upward"),
    ("Margin and Leverage", "borrowing to trade, margin calls, and how leverage magnifies both directions"),
    ("Penny Stocks and Pump-and-Dump", "why cheap shares are usually cheap for a reason, and the classic scam"),
    ("Dividend", "companies paying shareholders directly; yield and payout ratio"),
    ("Earnings, EPS and Earnings Season", "the quarterly report card and why the stock moves on it"),
    ("Beating or Missing Estimates", "why a profitable company's stock can fall on good results"),
    ("Price-to-Earnings (P/E) Ratio", "the most common valuation shorthand and its limits"),
    ("Guidance", "why a company's forecast often moves the stock more than the actual results"),
    ("ETF and Index Fund", "buying the whole market in one instrument; fees compound"),
    ("Bond, Coupon and Yield", "lending to governments and companies; why price and yield move opposite"),
    ("Interest Rates and the Central Bank", "the single lever behind most market moves"),
    ("Inflation and CPI", "what the number measures and why it drives rate decisions"),
    ("Bull Market, Bear Market, Correction", "the vocabulary for market direction, with the standard thresholds"),
    ("Volatility and the VIX", "measuring fear; why volatility itself is tradeable"),
    ("Options: Calls and Puts", "the right but not the obligation; how options differ from shares"),
    ("IPO and Lock-Up", "how a private company goes public and what happens months later"),
    ("Liquidity and Market Makers", "why you can always sell a big stock and not a small one"),
    ("Compounding and Time in the Market", "the arithmetic that makes long horizons decisive"),
]

FOUNDATIONS_WORLD = [
    ("Strait of Hormuz", "the Gulf chokepoint: geography, the ~20% of seaborne oil it carries, who can close it and what happens if they try"),
    ("Chokepoint", "the general concept: why a few narrow waterways govern world trade"),
    ("Strait of Malacca", "Asia's oil artery and China's 'Malacca dilemma'"),
    ("Suez Canal and Bab el-Mandeb", "the Europe-Asia shortcut and the Red Sea approach to it"),
    ("Taiwan Strait", "why a 180km channel is the biggest single risk to the technology industry"),
    ("OPEC and OPEC+", "the oil cartel, production quotas, and how it sets prices"),
    ("Brent vs WTI Crude", "the two benchmark oil prices and what the gap between them says"),
    ("Sanctions", "the main instrument short of war: types, who they hurt, why they leak"),
    ("UN Security Council and the Veto", "why the UN is paralysed on exactly the biggest conflicts"),
    ("NATO and Article 5", "what a mutual defence guarantee actually commits members to"),
    ("Proxy War", "fighting through third parties; why great powers prefer it"),
    ("Ceasefire, Armistice, Peace Treaty", "three different things routinely conflated in headlines"),
    ("Sphere of Influence and Buffer States", "the logic behind a lot of great-power behaviour"),
    ("Tariffs and Trade Wars", "who actually pays a tariff, and the retaliation cycle"),
    ("Export Controls", "restricting technology rather than goods; the chip-war weapon of choice"),
    ("Reserve Currency", "why the dollar's role gives the US power others resent"),
    ("Strategic Petroleum Reserve", "national oil stockpiles as a price and security tool"),
    ("Freedom of Navigation", "the legal principle behind naval patrols in contested water"),
    ("BRICS", "the bloc, what it wants, and how much of it is real"),
    ("Soft Power vs Hard Power", "influence by attraction vs coercion"),
    ("Sovereignty and Recognition", "why who counts as a country is a live political question"),
    ("Central Bank Independence", "why governments hand rate-setting to unelected officials"),
    ("Coalition Government", "how most of the world's democracies actually form governments"),
    ("Remittances and Diaspora", "migration as an economic force for whole countries"),
    ("Food and Fertiliser Security", "why wheat and fertiliser flows cause political crises"),
]


def spotlighted_concepts() -> set[str]:
    """Concepts that have had a FULL Concept Spotlight in some prior brief.

    Deliberately stricter than taught_terms(). The Strait of Hormuz was defined
    twice as a two-line bullet ("a narrow waterway between Iran and Oman...")
    and its reader still had to ask what it was -- a passing gloss is enough to
    parse the sentence and not enough to learn the thing. So the syllabus counts
    only the in-depth treatment as delivered, and a term entry does not retire a
    foundational concept.
    """
    done: set[str] = set()
    for p in sorted(common.BRIEFS_DIR.glob(f"*-{common.BRIEF_SLUG}.md")):
        text = p.read_text(errors="ignore")
        for m in re.finditer(
            r"^#{2,4}\s*(?:concept\s+spotlight|deep\s+dive|spotlight)\s*[:\-]\s*(.+?)\s*$",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        ):
            done.add(_term_key(m.group(1)))
    return done


def pick_foundations() -> list[tuple[str, str]]:
    """Next undelivered concept from each track -- one markets, one world affairs.

    Balanced deliberately: the reactive term memory already over-supplies finance
    and AI vocabulary, so the syllabus guarantees world-affairs literacy gets
    equal billing rather than competing with it for space.
    """
    spotlighted = spotlighted_concepts()
    picks: list[tuple[str, str]] = []
    for track in (FOUNDATIONS_MARKETS, FOUNDATIONS_WORLD):
        for name, cover in track:
            if _term_key(name) not in spotlighted:
                picks.append((name, cover))
                break
    return picks


def build_foundations_block(picks: list[tuple[str, str]]) -> str:
    if not picks:
        return (
            "FOUNDATIONS: the syllabus is exhausted -- every foundational concept has been "
            "taught. Choose two genuinely useful concepts of your own instead, one from "
            "markets/investing and one from world affairs."
        )
    lines = [
        "FOUNDATIONS: teach EXACTLY these two concepts in full today, as the two Concept "
        "Spotlights in the Foundations & Terms section. They are scheduled by a syllabus, "
        "NOT by today's news -- teach them even if the news does not mention them. Assume "
        "the reader has never encountered the concept before.",
        "",
    ]
    for name, cover in picks:
        lines.append(f"  - {name} -- cover: {cover}")
    return "\n".join(lines)

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

    # Some hosts reject feedparser's own fetcher while serving the byte-identical
    # request from requests -- Reddit answers feedparser with 429 and requests
    # with 200. Retry once through requests before giving up on the feed.
    if not parsed.entries and parsed.get("status") in (401, 403, 429, 503):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=int(os.environ.get("BRIEF_FEED_TIMEOUT", "20")),
            )
            if resp.ok:
                parsed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"  ! {label} (requests retry): {exc}", file=sys.stderr)

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
    topics = list(GOOGLE_NEWS_TOPICS) + rotating_domestic_topics(common.today_str())
    feeds += [(cat, f"Google News: {label}", google_news_url(q)) for cat, label, q in topics]
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
        # Give every feed a floor of FEED_FLOOR items before ranking the rest on
        # recency alone. Pure recency lets a few high-volume wires fill the
        # category and silently starve the narrow beats: the rotating domestic
        # feeds returned 8 India stories and not one survived the cap, which is
        # precisely the coverage that was asked for.
        floor: list[dict] = []
        rest: list[dict] = []
        per_feed: collections.Counter[str] = collections.Counter()
        for it in buckets[cat]:
            feed = it.get("feed", "")
            if per_feed[feed] < FEED_FLOOR:
                per_feed[feed] += 1
                floor.append(it)
            else:
                rest.append(it)
        buckets[cat] = floor + rest

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
Write like a sharp, plain-spoken newspaper -- confident, causal, never sensational or hype-y. Short paragraphs and clean bullets. For every story go beyond the headline: what happened, the background a newcomer needs, why it matters, who wins/loses, second-order effects, and what's still uncertain. This should be a substantive read (roughly 20-25 minutes); be comprehensive and generous with explanation, but every sentence must earn its place.

Plain language (IMPORTANT -- Laksh has never read a newspaper regularly and is new to finance and geopolitics):
- Write for a smart beginner, not for someone who already follows markets. Never assume a term is "obvious".
- The FIRST time any specialist term appears in a brief, gloss it in plain English in parentheses immediately, then continue. This applies to finance (basis points, yield, short, capex, guidance, go-shop period, market cap), geopolitics (chokepoint, sanctions, proxy, sovereignty), and AI (inference, weights, MoE, context window). A term listed as mastered in TERM MEMORY is the only exception -- use those freely.
- Prefer the short common word: "buying back its own shares" over "executing a buyback", "cost of borrowing" over "cost of capital". Where the jargon is worth learning, give the plain phrase first and the technical term after it in parentheses -- that way he learns the word without needing it to read the sentence.
- Keep sentences short. Avoid stacking three unexplained proper nouns in a row.
- Never use a number without saying what it means. "$44.9B in capex" alone is useless; "$44.9B on data centres and other long-lived assets (capex) -- roughly double last year" teaches something.

Ticker clarity: the FIRST time you name a company by its stock ticker, give the company name too, unless it is a household name (Apple, Tesla, Nvidia, Microsoft, Amazon, Google/Alphabet, Meta, Netflix, Intel, AMD). Write it as "Company (TICKER)" -- e.g. "Credo (CRDO)", "Vistra (VST)", "Constellation Energy (CEG)", "Astera Labs (ALAB)". Never leave a non-obvious ticker unexplained.

Coverage -- give these FOUR pillars roughly equal weight every day:
A. World & Geopolitics (LEAD PILLAR): wars and conflicts, great-power relations, diplomacy, elections, major international deals, and notable/outrageous statements by leaders. PRIORITIZE these relationships when there's news: US-China (tech war, Taiwan, trade), the Middle East (Israel, Iran, Gulf, oil), and India & South Asia. Cover Russia-Ukraine/Europe and others when genuinely major. Weave in the secondary beats lightly when there's real news: US politics & policy, and defense & military tech. ALSO cover what is happening INSIDE countries, not only between them -- protests, strikes, court rulings, parliaments, elections and the domestic economy. The source bundle carries "Domestic: <country>" feeds that rotate day to day; when they contain real news, give them genuine space rather than a passing line.
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

## 5. What Builders Are Actually Using
- 3-5 bullets on the PRACTICAL layer the institutional press misses: which open-weight model is a credible swap for which closed one and at what quality cost; model routing (sending easy queries to a small cheap model and hard ones to a big one) and what it saves; real price-per-million-tokens comparisons; tools, libraries or repos that developers are actually adopting; running models locally. Draw on the r/LocalLLaMA, Hacker News and practical-AI sources in the bundle -- this is the "you could just use this instead" layer that circulates among engineers.
- Be concrete and current: name the model, the rough benchmark gap, and the price difference. If the sources genuinely have nothing new today, say so in one line rather than padding with generic advice.

## 6. Watchlist: Earnings, Guidance & Movers
- Watchlist companies with earnings/news in the last 24h: key numbers vs expectations, guidance, stock reaction. Flag roughly +/-3% moves. Treat the quote snapshot as approximate/delayed. Say plainly when the sources have nothing on a name rather than inventing. Refer to each company by name with its ticker in parentheses (e.g. "Credo (CRDO)"), especially for non-obvious tickers -- do not use bare tickers.

## 7. Analysis: Second-Order Effects
This is the section where you REASON rather than report, and it is the one Laksh most wants -- the wires tell him a tanker was hit; they will not walk him from that to his portfolio. Give 3-4 chains.

- Each chain starts from a real story above and follows it two, three, four steps out to consequences no single article states. Write the chain explicitly with arrows, then explain each link in prose. Example shape: attack in the Strait of Hormuz -> war-risk insurance premiums on tankers jump -> shipping costs rise -> diesel and petrol prices follow -> headline inflation ticks up -> the Fed has less room to cut rates -> higher borrowing costs press on exactly the debt-funded data-centre buildout in section 3.
- Cover BOTH kinds: geopolitics/macro chains AND infrastructure/supply-chain chains (AI demand -> cloud capex -> GPUs -> HBM -> optics -> power). Do not make this an AI-only section.
- CRITICAL -- label your epistemics, because Laksh is learning to tell reported fact from inference. Every chain must carry: the FACTS it starts from (sourced, from the stories above), the INFERENCE (your reasoning, clearly marked as such with hedged language -- "this would likely", "the usual pattern is"), the WEAKEST LINK (the step most likely to break, named explicitly), and WHAT WOULD FALSIFY IT (a concrete observable in the next days or weeks that would show the chain wrong).
- Never present inference as reported fact. If a step is a guess, say it is a guess. Teaching the method matters more than being right.
- Where a chain has a historical precedent, name it in one line (e.g. "the 2019 Abqaiq strike moved Brent ~15% in a day, then fully retraced within weeks") -- precedent is how he will learn to calibrate.

## 8. Building Your Knowledge
- 4-6 bullets teaching durable patterns (how markets, geopolitics, or the AI buildout actually work), pitched slightly higher each day as Laksh's knowledge grows.

## 9. Foundations & Terms
- Lead with TWO "Concept Spotlights" -- exactly the two concepts named in the FOUNDATIONS block below, one from markets/investing and one from world affairs. These are set by a syllabus, not by the news, so teach them even if nothing today mentions them. This is the section that fixes the gap where the brief only ever defined whatever the headlines happened to raise.
- Each Spotlight, written for someone encountering the idea for the first time: what it is in plain words; WHY it exists / what problem it solves; a concrete worked example with real numbers; how it shows up in the news (tie to today's stories if you honestly can, otherwise a typical case); rough benchmarks or figures worth remembering; and a caution or common misconception. Use the heading form "### Concept Spotlight: <Name>".
- Then 4-8 shorter term entries drawn from today's news, following the TERM MEMORY rules below EXACTLY. Use the form "- **Term:** explanation".

## 10. Bottom Line
- 1-3 short paragraphs synthesizing the big picture and explicitly noting what CHANGED since the previous brief. (Write this BEFORE the Sources list so it is never the thing that gets cut.)

## 11. Sources
- List ONLY the stories you actually cited above (aim for ~15-25 bullets, not every source provided), grouped by pillar. Keep it compact. Every bullet is itself a clickable Markdown link `[Publisher -- headline](https://...)`.

Foundations:
{foundations}

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


def build_messages(
    sources, quotes, today, prior_excerpt, include_quick_check, term_memory, foundations
):
    retention = (
        "This run is a retention checkpoint: append a final '## Quick Check' section with "
        "3 short logic questions (numbered) drawn from today's brief, followed by their answers."
        if include_quick_check
        else "Do NOT add a 'Quick Check' section this run."
    )
    spec = BRIEF_SPEC.format(
        watchlist=WATCHLIST_DISPLAY, retention=retention, foundations=foundations
    )
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
        picks = pick_foundations()
        foundations = build_foundations_block(picks)
        print(
            "Foundations today: "
            + (", ".join(name for name, _ in picks) if picks else "syllabus exhausted")
        )
        prior_excerpt = previous_brief_excerpt(today)
        messages = build_messages(
            sources, quotes, today, prior_excerpt, include_quick_check, term_memory, foundations
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
