"""Indian-market data sources: fundamentals, prices, and news.

Every fetch here is read-only and returns plain dataclasses. Nothing in this
module touches the database — that keeps the network-flaky part separable and
testable, and lets the ingestion pipeline own all the transactional logic.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import settings

log = logging.getLogger(__name__)

SCREENER_BASE = "https://www.screener.in/company"

# Indian financial media RSS. Kept broad on purpose: overlapping coverage of the
# same story across outlets is exactly what the deduplication step is for.
NEWS_FEEDS: list[tuple[str, str]] = [
    ("Moneycontrol", "https://www.moneycontrol.com/rss/business.xml"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Economic Times", "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms"),
    ("LiveMint", "https://www.livemint.com/rss/markets"),
    ("LiveMint", "https://www.livemint.com/rss/companies"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Business Standard", "https://www.business-standard.com/rss/companies-101.rss"),
]

# Politeness: screener.in is a free community resource, so requests are spaced
# out and identify themselves. One in-process lock serialises all scraping.
_SCRAPE_LOCK = asyncio.Lock()
_SCRAPE_DELAY_SECONDS = 1.5


@dataclass
class Fundamentals:
    ticker: str
    name: str
    bse_id: str | None = None
    nse_id: str | None = None
    sector: str | None = None
    industry: str | None = None
    current_price: float | None = None
    market_cap_cr: float | None = None
    pe: float | None = None
    pb: float | None = None
    roe: float | None = None
    roce: float | None = None
    debt_to_equity: float | None = None
    dividend_yield: float | None = None
    eps: float | None = None
    book_value: float | None = None
    face_value: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    promoter_holding: float | None = None
    sales_growth_3y: float | None = None
    profit_growth_3y: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    source_url: str = ""
    source_name: str = "screener.in"

    def content_hash(self) -> str:
        """Stable hash of the figures that matter.

        Ingestion compares this against the stored hash to skip re-embedding
        fundamentals that have not actually moved since the last run.
        """
        parts = [
            f"{k}={getattr(self, k)}"
            for k in (
                "current_price",
                "market_cap_cr",
                "pe",
                "pb",
                "roe",
                "roce",
                "debt_to_equity",
                "dividend_yield",
                "eps",
                "book_value",
                "high_52w",
                "low_52w",
                "promoter_holding",
            )
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()


@dataclass
class PriceMetrics:
    last_close: float | None = None
    return_1m: float | None = None
    return_3m: float | None = None
    return_6m: float | None = None
    return_1y: float | None = None
    volatility_1y: float | None = None
    drawdown_1y: float | None = None


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published_at: dt.datetime | None
    summary: str = ""
    body: str = ""

    @property
    def canonical_url(self) -> str:
        return canonicalise_url(self.url)

    def content_hash(self) -> str:
        """The idempotency key for an article.

        Built from the normalised title plus canonical URL so that the same
        story re-appearing in a later feed poll — or fetched concurrently by two
        jobs — collapses onto one row via the UNIQUE constraint.
        """
        basis = f"{normalise_title(self.title)}|{self.canonical_url}"
        return hashlib.sha256(basis.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "ref", "referrer")


def canonicalise_url(url: str) -> str:
    """Strip tracking params and fragments so syndicated copies match."""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    kept = [
        kv
        for kv in parts.query.split("&")
        if kv and not any(kv.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    return urlunparse(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            "",
            "&".join(sorted(kept)),
            "",
        )
    )


_TITLE_NOISE = re.compile(r"[^a-z0-9 ]+")
_APOSTROPHE = re.compile(r"['‘’ʼ]")
_WS = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    """Lowercase, strip punctuation and outlet suffixes, collapse whitespace."""
    text = title.lower()
    # Apostrophes are deleted rather than replaced with a space, so that
    # "Reliance's Q1" and "Reliances Q1" normalise to the same string instead
    # of splitting into "reliance s". Straight and curly quotes both count.
    text = _APOSTROPHE.sub("", text)
    # Outlets append their own name after a pipe or dash on the same story.
    text = re.split(r"\s+[\|\-–—]\s+(moneycontrol|economic times|mint|business standard)", text)[0]
    text = _TITLE_NOISE.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _to_float(value: Any) -> float | None:
    """Parse screener.in's rendered numbers: '₹ 1,234', '12.3 %', '1,23,456'."""
    if value is None:
        return None
    text = str(value)
    text = text.replace(",", "").replace("₹", "").replace("%", "")
    text = text.replace("Cr.", "").replace("Cr", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


# --------------------------------------------------------------------------- #
# Fundamentals (screener.in)
# --------------------------------------------------------------------------- #
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10), reraise=True)
async def fetch_fundamentals(ticker: str) -> Fundamentals | None:
    """Scrape one company's ratio block from screener.in.

    Returns None when the ticker has no page (rather than raising) so a bad
    follow request degrades to "no fundamentals" instead of a 500.
    """
    ticker = ticker.upper().strip()
    url = f"{SCREENER_BASE}/{ticker}/consolidated/"

    async with (
        _SCRAPE_LOCK,
        httpx.AsyncClient(
            timeout=settings.ingest_http_timeout,
            headers={"User-Agent": settings.ingest_user_agent},
            follow_redirects=True,
        ) as client,
    ):
        resp = await client.get(url)
        if resp.status_code == 404:
            # Some companies only have a standalone (non-consolidated) page.
            resp = await client.get(f"{SCREENER_BASE}/{ticker}/")
        await asyncio.sleep(_SCRAPE_DELAY_SECONDS)

    if resp.status_code != 200:
        log.warning("screener.in returned %s for %s", resp.status_code, ticker)
        return None

    return _parse_screener(ticker, resp.text, str(resp.url))


def _parse_screener(ticker: str, html: str, url: str) -> Fundamentals:
    soup = BeautifulSoup(html, "lxml")

    name_el = soup.select_one("h1")
    name = name_el.get_text(strip=True) if name_el else ticker

    # The ratio strip renders as <li><span class="name">…</span>
    # <span class="value">…</span></li>; keys are read by label so a layout
    # reshuffle degrades to missing fields rather than wrong ones.
    ratios: dict[str, str] = {}
    for li in soup.select("#top-ratios li"):
        label_el = li.select_one(".name")
        value_el = li.select_one(".value")
        if label_el and value_el:
            label = _WS.sub(" ", label_el.get_text(strip=True)).lower()
            ratios[label] = _WS.sub(" ", value_el.get_text(strip=True))

    def ratio(*names: str) -> float | None:
        for n in names:
            for key, value in ratios.items():
                if key.startswith(n):
                    return _to_float(value)
        return None

    high_low = ratios.get("high / low", "")
    high = low = None
    if "/" in high_low:
        high, low = (_to_float(p) for p in high_low.split("/", 1))

    nse_id = bse_id = None
    for link in soup.select("a[href*='nseindia'], a[href*='bseindia']"):
        text = link.get_text(strip=True)
        href = link.get("href", "")
        if "nseindia" in href:
            nse_id = text or ticker
        elif "bseindia" in href:
            digits = re.search(r"\d{6}", href + " " + text)
            bse_id = digits.group() if digits else text

    sector = industry = None
    for link in soup.select("p.sub a"):
        label = link.get_text(strip=True)
        if sector is None:
            sector = label
        elif industry is None:
            industry = label

    return Fundamentals(
        ticker=ticker,
        name=name,
        nse_id=nse_id or ticker,
        bse_id=bse_id,
        sector=sector,
        industry=industry,
        current_price=ratio("current price"),
        market_cap_cr=ratio("market cap"),
        pe=ratio("stock p/e", "p/e"),
        pb=ratio("price to book", "book value"),
        roe=ratio("roe", "return on equity"),
        roce=ratio("roce", "return on capital"),
        debt_to_equity=ratio("debt to equity"),
        dividend_yield=ratio("dividend yield"),
        eps=ratio("eps"),
        book_value=ratio("book value"),
        face_value=ratio("face value"),
        high_52w=high,
        low_52w=low,
        promoter_holding=ratio("promoter holding"),
        raw=ratios,
        source_url=url,
    )


# --------------------------------------------------------------------------- #
# Prices (yfinance)
# --------------------------------------------------------------------------- #
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

# Yahoo rate-limits by client, not by network. yfinance's own user-agent is
# throttled to HTTP 429 from a server, which looked like an IP block and was
# nearly written off as "Yahoo blocks AWS". The same request with an ordinary
# browser header returns a full year of closes from the same host, so the
# endpoint is called directly — which also drops yfinance and pandas from the
# image, a meaningful saving on a 1 GiB instance.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


async def fetch_price_metrics(ticker: str) -> PriceMetrics | None:
    """Derive momentum and volatility from one year of NSE closes.

    Returns None on any failure — price history is a scoring input, not a
    prerequisite, so a bad response degrades momentum to neutral rather than
    failing the whole ingest.
    """
    url = f"{YAHOO_CHART_URL}/{ticker.upper()}.NS"
    try:
        async with httpx.AsyncClient(
            timeout=settings.ingest_http_timeout,
            headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, params={"range": "1y", "interval": "1d"})
        if resp.status_code != 200:
            log.warning("yahoo returned %s for %s", resp.status_code, ticker)
            return None
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - network and JSON surface is broad
        log.warning("price fetch failed for %s: %s", ticker, exc)
        return None

    try:
        result = payload["chart"]["result"][0]
        raw = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        log.warning("unexpected yahoo payload shape for %s", ticker)
        return None

    # Holidays and halts come back as nulls; drop them rather than
    # interpolating, since a gap is not a price.
    closes = [float(c) for c in raw if c is not None]
    if len(closes) < 20:
        return None

    return _metrics_from_closes(closes)


def _metrics_from_closes(closes: list[float]) -> PriceMetrics:
    """Compute momentum, volatility and drawdown from a close series.

    Split out from the fetch so it is testable without a network call, and
    written in plain Python because pulling pandas in for four aggregates over
    250 points is not a trade worth making.
    """
    latest = closes[-1]

    def trailing_return(sessions: int) -> float | None:
        if len(closes) <= sessions:
            return None
        past = closes[-sessions - 1]
        return (latest / past - 1.0) if past else None

    daily = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1]]
    volatility = None
    if len(daily) > 1:
        mean = sum(daily) / len(daily)
        variance = sum((d - mean) ** 2 for d in daily) / (len(daily) - 1)
        volatility = (variance**0.5) * (252**0.5)  # annualised from daily

    peak = closes[0]
    drawdown = 0.0
    for price in closes:
        peak = max(peak, price)
        if peak:
            drawdown = min(drawdown, price / peak - 1.0)

    return PriceMetrics(
        last_close=latest,
        # Trading sessions, not calendar days. Each window returns None when
        # the series is too short rather than measuring over whatever history
        # happens to exist — a 29-session move reported as a "1-year return"
        # is a wrong number, and a wrong number is worse than a blank one in a
        # panel a person makes decisions from. A year of NSE trading is ~245
        # sessions; 240 allows for holidays without stretching the label.
        return_1m=trailing_return(21),
        return_3m=trailing_return(63),
        return_6m=trailing_return(126),
        return_1y=trailing_return(240),
        volatility_1y=volatility,
        drawdown_1y=drawdown,
    )


# --------------------------------------------------------------------------- #
# News (RSS)
# --------------------------------------------------------------------------- #
def _company_query_feeds(company_name: str, ticker: str) -> list[tuple[str, str]]:
    """Google News RSS searches scoped to a single company.

    The broad outlet feeds carry whatever is on the front page, so for any
    individual ticker they almost never hit: a sample of 189 headlines across
    all eight contained exactly one Reliance story, and that one was a
    market-wrap false positive. Filtering a firehose is the wrong shape for
    per-company coverage.

    These searches are company-scoped at the source instead. Google News is an
    aggregator, so what comes back is still Indian financial media — Economic
    Times, Moneycontrol, Mint, Business Standard — and each item carries its
    originating publisher, which is what gets cited.
    """
    from urllib.parse import quote_plus

    # Trim the legal suffix: "Reliance Industries Ltd" as an exact phrase
    # misses headlines that only say "Reliance Industries".
    short_name = re.sub(
        r"\s+(ltd|limited|corporation|corp)\.?$", "", company_name, flags=re.I
    ).strip()

    queries = [
        f'"{short_name}" (stock OR shares OR results OR NSE)',
        f'"{ticker}" NSE',
    ]
    return [
        (
            "Google News",
            "https://news.google.com/rss/search?"
            f"q={quote_plus(q)}+when:30d&hl=en-IN&gl=IN&ceid=IN:en",
        )
        for q in queries
    ]


async def fetch_news(company_name: str, ticker: str, limit: int | None = None) -> list[NewsItem]:
    """Pull recent Indian financial news about one company.

    Two sources combined:
      * company-scoped searches, which supply most of the coverage
      * the broad outlet feeds, filtered by mention, which catch sector and
        market-wide stories a company query would miss

    Feeds are fetched concurrently and per-feed failures are swallowed, so one
    dead outlet cannot fail the whole ingest.
    """
    limit = limit or settings.ingest_news_per_ticker

    company_feeds = _company_query_feeds(company_name, ticker)
    results = await asyncio.gather(
        *(_fetch_feed(source, url) for source, url in company_feeds + NEWS_FEEDS),
        return_exceptions=True,
    )

    targeted: list[NewsItem] = []
    broad: list[NewsItem] = []
    for index, outcome in enumerate(results):
        if isinstance(outcome, BaseException):
            log.warning("feed fetch failed: %s", outcome)
            continue
        # The company-scoped feeds come first in the gather order.
        (targeted if index < len(company_feeds) else broad).extend(outcome)

    # Company-scoped results are already about this company; only the broad
    # feeds need the mention filter.
    items = targeted + [i for i in broad if _mentions(i, company_name, ticker)]

    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in items:
        key = item.content_hash()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    unique.sort(
        key=lambda i: i.published_at or dt.datetime.min.replace(tzinfo=dt.UTC), reverse=True
    )
    log.info(
        "news for %s: %d targeted, %d matched from broad feeds, %d unique",
        ticker,
        len(targeted),
        len(items) - len(targeted),
        len(unique),
    )
    return unique[:limit]


async def _fetch_feed(source: str, url: str) -> list[NewsItem]:
    async with httpx.AsyncClient(
        timeout=settings.ingest_http_timeout,
        headers={"User-Agent": settings.ingest_user_agent},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        return []

    # feedparser is CPU-bound XML parsing; keep it off the loop.
    parsed = await asyncio.to_thread(feedparser.parse, resp.content)
    items: list[NewsItem] = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue
        items.append(
            NewsItem(
                title=_clean_title(title),
                url=link.strip(),
                source=_resolve_source(entry, source),
                published_at=_entry_datetime(entry),
                summary=_strip_html(entry.get("summary", ""))[:2000],
            )
        )
    return items


def _resolve_source(entry: Any, fallback: str) -> str:
    """Report the outlet that actually published the story.

    Aggregated feeds name themselves as the feed source but carry the real
    publisher in a `source` element. Citing "Google News" would be both less
    useful to the reader and less honest about where a claim came from.
    """
    origin = entry.get("source")
    if isinstance(origin, dict):
        title = (origin.get("title") or "").strip()
        if title:
            return title[:80]
    return fallback


_AGGREGATOR_SUFFIX = re.compile(
    r"\s+-\s+[^-]{2,40}$"  # trailing " - Publisher Name"
)


def _clean_title(title: str) -> str:
    """Drop the publisher suffix aggregators append to headlines.

    Left in, the same story from two aggregators would hash differently and
    escape exact deduplication.
    """
    return _AGGREGATOR_SUFFIX.sub("", title.strip()).strip()


def _entry_datetime(entry: Any) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        struct_time = entry.get(key)
        if struct_time:
            return dt.datetime(*struct_time[:6], tzinfo=dt.UTC)
    return None


def _strip_html(text: str) -> str:
    return _WS.sub(" ", BeautifulSoup(text or "", "lxml").get_text(" ")).strip()


def _mentions(item: NewsItem, company_name: str, ticker: str) -> bool:
    """Cheap lexical gate before anything expensive happens.

    Matching on the distinctive leading words of the company name avoids the
    false positives you get from generic tokens like 'India' or 'Limited'.
    """
    haystack = f"{item.title} {item.summary}".lower()
    if ticker.lower() in haystack:
        return True
    return any(token in haystack for token in _name_tokens(company_name))


_NAME_STOPWORDS = {
    "ltd",
    "limited",
    "india",
    "indian",
    "company",
    "corporation",
    "corp",
    "industries",
    "enterprises",
    "group",
    "the",
    "and",
    "of",
    "co",
}


def _name_tokens(company_name: str) -> list[str]:
    tokens = [
        t
        for t in _TITLE_NOISE.sub(" ", company_name.lower()).split()
        if t not in _NAME_STOPWORDS and len(t) > 3
    ]
    return tokens[:2]


async def fetch_article_body(url: str) -> str:
    """Fetch and flatten an article's main text.

    Best-effort: paywalls and bot walls are common, and a missing body just
    means retrieval falls back to the RSS summary.
    """
    try:
        async with httpx.AsyncClient(
            timeout=settings.ingest_http_timeout,
            headers={"User-Agent": settings.ingest_user_agent},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        container = (
            soup.select_one("article")
            or soup.select_one("div.content_wrapper")
            or soup.select_one("div.storyContent")
            or soup.body
        )
        if container is None:
            return ""
        paragraphs = [p.get_text(" ", strip=True) for p in container.select("p")]
        return _WS.sub(" ", " ".join(paragraphs))[:20000]
    except Exception as exc:  # noqa: BLE001
        log.debug("body fetch failed for %s: %s", url, exc)
        return ""
