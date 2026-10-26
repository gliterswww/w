"""News monitor, economic calendar and earnings analyst.

* Economic calendar (config/economic_calendar.csv): scheduled releases such as FOMC, CPI and NFP.
  High-impact events create no-new-entry blackout windows that the risk manager enforces.
* News monitor: market headlines from free RSS feeds, tagged by topic. It's informational only.
  Headlines never trigger trades.
* Earnings analyst: report dates for the largest Nasdaq-100 companies. A mega-cap reporting after
  the close makes the *next* session an "earnings reaction day" for NQ (bigger gap, higher vol).
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .data import ET

# Approximate index weights (%). Only used to rank how much a report matters to NQ.
MEGA_CAPS = {
    "NVDA": 9.0, "MSFT": 8.5, "AAPL": 8.0, "AMZN": 5.5, "AVGO": 5.0, "META": 4.0,
    "GOOGL": 3.5, "GOOG": 3.3, "TSLA": 3.0, "NFLX": 1.6, "COST": 1.5, "AMD": 1.2,
}


@dataclass
class EconEvent:
    at: datetime
    event: str
    impact: str
    source: str = ""


def load_calendar(path: str | Path) -> list[EconEvent]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open() as f:
        for row in csv.DictReader(f):
            try:
                at = datetime.strptime(row["datetime_et"].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=ET)
            except (KeyError, ValueError):
                continue
            out.append(EconEvent(at, row.get("event", "").strip(), row.get("impact", "medium").strip().lower(),
                                 row.get("source", "").strip()))
    return sorted(out, key=lambda e: e.at)


def blackout_reason(events: list[EconEvent], now: datetime, before_min: int, after_min: int) -> str | None:
    for e in events:
        if e.impact != "high":
            continue
        if e.at - timedelta(minutes=before_min) <= now <= e.at + timedelta(minutes=after_min):
            return f"{e.event} at {e.at:%H:%M} ET"
    return None


def upcoming(events: list[EconEvent], now: datetime, days: int = 14) -> list[dict]:
    horizon = now + timedelta(days=days)
    return [{"at": e.at.isoformat(), "event": e.event, "impact": e.impact, "source": e.source}
            for e in events if now - timedelta(hours=2) <= e.at <= horizon]


# ----------------------------------------------------------------------------- news

FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^NDX&region=US&lang=en-US",
    "https://www.marketwatch.com/rss/topstories",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
]
TOPICS = {
    "Fed & rates": r"\b(fed|fomc|powell|rate cut|rate hike|interest rate|treasury|yield)s?\b",
    "Inflation": r"\b(cpi|ppi|inflation|pce)\b",
    "Jobs": r"\b(jobs report|payrolls|nonfarm|unemployment|jobless)\b",
    "Earnings": r"\b(earnings|revenue|guidance|quarter|eps)\b",
    "Mega-cap tech": r"\b(nvidia|apple|microsoft|amazon|meta|alphabet|google|tesla|broadcom|netflix)\b",
    "Chips & AI": r"\b(semiconductor|chip|chips|ai|artificial intelligence)\b",
    "Index": r"\b(nasdaq|s&p|stocks|wall street|futures)\b",
}


def tag_headline(title: str) -> list[str]:
    t = title.lower()
    return [name for name, pat in TOPICS.items() if re.search(pat, t)]


def fetch_news(max_items: int = 25) -> tuple[list[dict], str | None]:
    """Returns (items, error). Never raises; a dead feed just yields fewer items."""
    try:
        import feedparser
    except ImportError:
        return [], "feedparser not installed"
    items, seen, errors = [], set(), 0
    for url in FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception:
            errors += 1
            continue
        if not parsed.get("entries"):
            errors += 1
        for e in parsed.get("entries", []):
            title = (e.get("title") or "").strip()
            if not title or title in seen:
                continue
            tags = tag_headline(title)
            if not tags:
                continue
            seen.add(title)
            items.append({"title": title, "link": e.get("link", ""), "published": e.get("published", ""),
                          "source": parsed.get("feed", {}).get("title", ""), "tags": tags,
                          "high_impact": any(t in ("Fed & rates", "Inflation", "Jobs") for t in tags)})
    items = items[:max_items]
    err = "all news feeds unreachable" if errors == len(FEEDS) else None
    return items, err


# ----------------------------------------------------------------------------- earnings


def load_earnings_csv(path: str | Path) -> list[dict]:
    """config/earnings.csv: symbol,date,timing (timing = bmo | amc). Manual override / fallback."""
    p = Path(path)
    if not p.exists():
        return []
    with p.open() as f:
        return [{"symbol": r["symbol"].strip().upper(), "date": r["date"].strip(),
                 "timing": r.get("timing", "amc").strip().lower(), "source": "config"} for r in csv.DictReader(f)]


def fetch_earnings_yahoo(symbols=tuple(MEGA_CAPS)) -> tuple[list[dict], str | None]:
    try:
        import yfinance as yf
    except ImportError:
        return [], "yfinance not installed"
    out, failures = [], 0
    for sym in symbols:
        try:
            df = yf.Ticker(sym).get_earnings_dates(limit=8)
            if df is None or df.empty:
                failures += 1
                continue
            for ts in df.index:
                ts = ts.tz_convert(ET)
                out.append({"symbol": sym, "date": ts.date().isoformat(),
                            "timing": "bmo" if ts.hour < 12 else "amc", "source": "yahoo"})
        except Exception:
            failures += 1
    return out, ("earnings data unavailable from Yahoo" if failures == len(symbols) else None)


def reaction_day(report_date: date, timing: str) -> date:
    """The NQ session that first trades on the news."""
    if timing == "bmo":
        return report_date
    d = report_date + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def earnings_outlook(reports: list[dict], today: date, days: int = 21) -> dict:
    rows = []
    for r in reports:
        try:
            d = date.fromisoformat(r["date"])
        except ValueError:
            continue
        rd = reaction_day(d, r["timing"])
        if today - timedelta(days=1) <= rd <= today + timedelta(days=days):
            rows.append({**r, "reaction_day": rd.isoformat(), "weight": MEGA_CAPS.get(r["symbol"], 0.0)})
    # de-duplicate (config overrides yahoo)
    best = {}
    for r in sorted(rows, key=lambda r: r["source"] != "config"):
        best.setdefault((r["symbol"], r["reaction_day"]), r)
    rows = sorted(best.values(), key=lambda r: (r["reaction_day"], -r["weight"]))
    today_weight = sum(r["weight"] for r in rows if r["reaction_day"] == today.isoformat())
    return {"reports": rows, "today_weight": round(today_weight, 1),
            "today_flag": "elevated" if today_weight >= 5 else "normal"}


def reaction_days(reports: list[dict]) -> dict[date, float]:
    """Map of reaction day -> combined index weight reporting. Used by research to split results."""
    out: dict[date, float] = {}
    for r in reports:
        try:
            rd = reaction_day(date.fromisoformat(r["date"]), r["timing"])
        except ValueError:
            continue
        out[rd] = out.get(rd, 0.0) + MEGA_CAPS.get(r["symbol"], 0.0)
    return out
