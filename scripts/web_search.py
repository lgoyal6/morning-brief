#!/usr/bin/env python3
"""Live web search for the morning brief (optional source enrichment).

The base pipeline grounds the LLM on a fixed list of RSS / Google-News feeds.
This module adds *live* web-search results from a search API so the SOURCE
BUNDLE is broader and fresher than the hard-coded feeds -- while keeping the
same "cite only what we hand you" guardrail: we still pre-fetch here, the model
never browses. Results are normalized into the exact dict shape ``parse_feed``
produces, so everything downstream (dedupe, bundle, prompt) is unchanged.

Providers (choose with SEARCH_PROVIDER):
  * ``tavily`` (default) -- news-optimized; returns clean content snippets.
  * ``brave``            -- Brave News Search API.

Enable by providing a key via SEARCH_API_KEY (generic) or a provider-specific
var (TAVILY_API_KEY / BRAVE_API_KEY). With no key this module is a silent no-op
and the pipeline falls back to RSS only -- nothing breaks if the secret is
absent. Set SEARCH_PROVIDER=none to force it off even when a key is present.
"""

from __future__ import annotations

import os
import sys
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

DEFAULT_PROVIDER = "tavily"
MAX_PER_QUERY = int(os.environ.get("SEARCH_MAX_PER_QUERY", "5"))
SEARCH_TIMEOUT = int(os.environ.get("SEARCH_TIMEOUT", "25"))

# Topic queries mirror the brief's focus areas (see GOOGLE_NEWS_TOPICS in
# generate_brief.py). Override with SEARCH_QUERIES: a "||"-separated list of
# "Label::query terms" pairs.
DEFAULT_QUERIES = [
    ("AI Infrastructure", "AI data center GPU Nvidia hyperscaler capex"),
    ("Semiconductors", "semiconductor TSMC chip export controls HBM foundry"),
    ("Markets & Rates", "stock market Federal Reserve interest rates earnings guidance"),
    ("Geopolitics", "geopolitics sanctions war trade restrictions breaking news"),
    ("Cloud & Enterprise AI", "OpenAI Anthropic Microsoft Google cloud enterprise AI"),
    ("Data-Center Power", "data center power grid electricity nuclear energy AI demand"),
]


def _provider() -> str:
    return (os.environ.get("SEARCH_PROVIDER") or DEFAULT_PROVIDER).strip().lower()


def _api_key(provider: str) -> str | None:
    names = ["SEARCH_API_KEY"]
    if provider == "tavily":
        names.append("TAVILY_API_KEY")
    elif provider == "brave":
        names.append("BRAVE_API_KEY")
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return None


def search_enabled() -> bool:
    """True only if a real provider is selected and a key is available."""
    if _provider() in ("none", "off", "0", ""):
        return False
    return _api_key(_provider()) is not None


def _queries() -> list[tuple[str, str]]:
    raw = os.environ.get("SEARCH_QUERIES", "").strip()
    if not raw:
        return DEFAULT_QUERIES
    out: list[tuple[str, str]] = []
    for pair in raw.split("||"):
        pair = pair.strip()
        if not pair:
            continue
        label, _, query = pair.partition("::")
        query = query or label
        out.append((label.strip() or "Web Search", query.strip()))
    return out or DEFAULT_QUERIES


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _to_iso(value: str) -> str:
    """Best-effort convert an RFC-2822 or ISO date string to ISO 8601.

    Kept lenient: an unparseable/empty value returns "" so newest-first sorting
    downstream simply treats it as undated (sorts last) rather than crashing.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if "T" in value and value[:4].isdigit():  # already ISO-ish
        return value
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, IndexError):
        return ""


def _norm(label: str, title: str, link: str, summary: str, published: str) -> dict:
    return {
        "feed": f"Web Search: {label}",
        "publisher": _domain(link) or label,
        "title": " ".join((title or "").split()),
        "link": link or "",
        "published": _to_iso(published),
        "summary": " ".join((summary or "").split())[:400],
    }


def _tavily_raw(query: str, key: str, days: int, max_results: int) -> list[tuple]:
    # Tavily: key goes in the Authorization header; recency via time_range.
    time_range = "day" if days <= 1 else "week" if days <= 7 else "month"
    resp = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "query": query,
            "topic": "news",
            "time_range": time_range,
            "max_results": min(max_results, 20),
            "search_depth": "basic",
        },
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    # published_date is present for topic=news but not contractually guaranteed;
    # _norm/_to_iso treat a missing value as simply "undated".
    return [
        (r.get("title"), r.get("url"), r.get("content"), r.get("published_date", ""))
        for r in resp.json().get("results", [])
    ]


def _brave_raw(query: str, key: str, days: int, max_results: int) -> list[tuple]:
    freshness = "pd" if days <= 1 else "pw" if days <= 7 else "pm"
    resp = requests.get(
        "https://api.search.brave.com/res/v1/news/search",
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        params={"q": query, "count": max_results, "freshness": freshness, "spellcheck": 0},
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    return [
        (r.get("title"), r.get("url"), r.get("description"), r.get("page_age", ""))
        for r in resp.json().get("results", [])
    ]


def _search_one(label, query, provider, key, days, max_results) -> list[dict]:
    if provider == "tavily":
        raw = _tavily_raw(query, key, days, max_results)
    elif provider == "brave":
        raw = _brave_raw(query, key, days, max_results)
    else:
        raise ValueError(f"Unknown SEARCH_PROVIDER: {provider!r} (use 'tavily' or 'brave').")
    return [_norm(label, t, u, c, p) for (t, u, c, p) in raw]


def fetch_web_search(lookback_hours: int) -> list[dict]:
    """Run every topic query through the configured provider.

    Returns normalized item dicts (possibly empty). Never raises: a failing
    provider/query is logged and skipped so the brief still generates from RSS.
    """
    if not search_enabled():
        return []
    provider = _provider()
    key = _api_key(provider) or ""
    days = max(1, -(-lookback_hours // 24))  # ceil to whole days
    queries = _queries()
    print(f"Web search via {provider}: {len(queries)} queries (last {days}d)...")

    collected: list[dict] = []
    for label, query in queries:
        try:
            items = _search_one(label, query, provider, key, days, MAX_PER_QUERY)
        except Exception as exc:  # network / auth / quota -> skip this query
            print(f"  ! web search [{label}]: {exc}", file=sys.stderr)
            continue
        items = [it for it in items if it["title"] and it["link"]]
        collected.extend(items)
        print(f"  - {label}: {len(items)} results")
    return collected
