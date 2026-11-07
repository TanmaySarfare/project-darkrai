#!/usr/bin/env python3
"""
Nightly data build for Three Choices, rules v1.0.

Steps, in order:
  1. Once a month, refresh the S&P 500 list and market values (state/universe.json).
  2. Check the latest daily prices of every candidate: stale, missing, or a daily move over the limit.
  3. Update the stored monthly price history, fetching only recent months (state/monthly_close.csv).
     If Yahoo has re-based a ticker's history (a stock split), fetch that ticker's full history again.
  4. Build monthly returns. Before a young fund existed, use its proxy minus the fee difference.
  5. Fill scenario values marked "compute" once, then keep them fixed (state/scenario_values.json).
  6. Write site/data.new.json, check it with the same engine the page uses, and only then replace
     site/data.json. If anything fails, yesterday's data stays and the run fails so you get an email.

Sources: Yahoo Finance through yfinance (personal use only) and FRED (yields, inflation, CPI).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
TIINGO_PRICES = "https://api.tiingo.com/tiingo/daily/{}/prices"
MARKET_INDEX = "^GSPC"

SLOT_CLASS = {
    "us-stock": ("stock", "Stock ETF"), "intl-stock": ("stock", "Stock ETF"),
    "tsy-7-10": ("bond", "Bond ETF"), "tsy-20": ("bond", "Bond ETF"), "tips": ("bond", "Bond ETF"),
    "gold": ("real", "Gold ETF"), "commodities": ("real", "Commodity ETF"), "bitcoin": ("bitcoin", "Bitcoin ETF"),
}
FACT_LABELS = {"expenseRatio": "expense ratio", "yield": "yield", "assets": "assets", "inception": "inception date"}


class Fatal(Exception):
    """Stops the run. Yesterday's data stays published."""


class Report:
    SECTIONS = ["Blocking", "Warnings", "Unverified facts", "Assumptions", "Excluded", "Info"]

    def __init__(self):
        self.items = {s: [] for s in self.SECTIONS}
        self.check_output = None

    def add(self, section, message):
        if message not in self.items[section]:
            self.items[section].append(message)

    def markdown(self, title):
        out = ["## " + title, ""]
        for s in self.SECTIONS:
            if self.items[s]:
                out.append("**" + s + "**")
                out.extend("- " + m for m in self.items[s])
                out.append("")
        if self.check_output:
            out.append("**Engine check**")
            out.append("```")
            out.append(self.check_output.strip())
            out.append("```")
        return "\n".join(out)


# ---------------------------------------------------------------- helpers
def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=1, sort_keys=False) + "\n")


def month_label(d):
    return f"{d.year:04d}-{d.month:02d}"


def add_months(label, n):
    y, m = map(int, label.split("-"))
    m += n
    while m > 12:
        y, m = y + 1, m - 12
    while m < 1:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


def month_range(first, last):
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur = add_months(cur, 1)
    return out


def retry(fn, what, tries=3, wait=10):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # network errors, Yahoo rate limits
            last = e
            if i < tries - 1:
                time.sleep(wait * (2 ** i))
    raise RuntimeError(f"{what} failed after {tries} tries: {last}")


def naive_index(index):
    idx = pd.to_datetime(index)
    return idx.tz_localize(None) if getattr(idx, "tz", None) is not None else idx


def clean(x, digits=6):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, digits)


# ---------------------------------------------------------------- network
class Net:
    """Web requests. FRED ignores bare-name User-Agents, so FRED uses the library's standard one.
    Every request has a hard time limit."""

    def __init__(self, user_agent):
        import requests
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.plain = requests.Session()

    def text(self, url, params=None, plain=False, limit=45):
        session = self.plain if plain else self.session
        start = time.monotonic()
        with session.get(url, params=params, timeout=(10, 20), stream=True) as r:
            r.raise_for_status()
            chunks = []
            for chunk in r.iter_content(chunk_size=65536):
                chunks.append(chunk)
                if time.monotonic() - start > limit:
                    raise TimeoutError(f"no complete answer within {limit} seconds")
            return b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")

    def json(self, url, params=None):
        r = self.session.get(url, params=params, timeout=(10, 20))
        r.raise_for_status()
        return r.json()


def say(message):
    print(message, flush=True)


def split_frame(df, tickers):
    """yfinance returns one wide table; split it into one small table per ticker."""
    out = {}
    if df is None or len(df) == 0:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        lvl = 0 if set(tickers) & set(df.columns.get_level_values(0)) else 1
        names = set(df.columns.get_level_values(lvl))
        for t in tickers:
            if t in names:
                sub = df.xs(t, axis=1, level=lvl)
                if "Close" in sub.columns and sub["Close"].notna().any():
                    out[t] = sub[sub["Close"].notna()]
    elif len(tickers) == 1 and "Close" in df.columns:
        out[tickers[0]] = df[df["Close"].notna()]
    return out


class Yahoo:
    """yfinance with batching, pauses and retries."""

    def __init__(self, yf, settings):
        self.yf = yf
        self.batch = int(settings["batchSize"])
        self.pause = float(settings["pauseSeconds"])

    def history(self, tickers, **kw):
        out = {}
        for i in range(0, len(tickers), self.batch):
            chunk = tickers[i:i + self.batch]
            df = retry(lambda: self.yf.download(chunk, progress=False, auto_adjust=False, repair=True,
                                                group_by="ticker", threads=False, **kw), "Yahoo price download")
            out.update(split_frame(df, chunk))
            time.sleep(self.pause)
        return out

    def info(self, ticker):
        return retry(lambda: self.yf.Ticker(ticker).info, f"Yahoo facts for {ticker}") or {}

    def market_cap(self, ticker):
        def get():
            fi = self.yf.Ticker(ticker).fast_info
            for key in ("market_cap", "marketCap"):
                try:
                    v = getattr(fi, key) if hasattr(fi, key) else fi[key]
                except (KeyError, TypeError, AttributeError):
                    continue
                if v:
                    return float(v)
            raise RuntimeError("no market value")
        return retry(get, f"market value for {ticker}", tries=2, wait=5)


# ---------------------------------------------------------------- step 1: universe
def refresh_universe(ctx):
    path = ctx.state / "universe.json"
    old = load_json(path)
    label = month_label(ctx.today)
    if old and old.get("month") == label:
        return old
    try:
        html = ctx.net.text(WIKI_SP500)
        tables = pd.read_html(io.StringIO(html))
        table = next(t for t in tables if "Symbol" in t.columns and "GICS Sector" in t.columns)
        rows = [{"ticker": str(r["Symbol"]).strip().replace(".", "-"), "name": str(r["Security"]).strip(),
                 "sector": str(r["GICS Sector"]).strip()} for _, r in table.iterrows()]
        if len(rows) < 450:
            raise RuntimeError(f"only {len(rows)} rows in the S&P 500 table")
        failed = []
        for i, row in enumerate(rows):
            try:
                row["marketValue"] = round(ctx.yahoo.market_cap(row["ticker"]) / 1e9, 3)
            except Exception:
                failed.append(row["ticker"])
            if i % 25 == 24:
                time.sleep(ctx.settings["pauseSeconds"])
        if len(failed) > 0.05 * len(rows):
            raise RuntimeError(f"{len(failed)} market values missing")
        if failed:
            ctx.report.add("Warnings", "No market value for " + ", ".join(failed[:10]) + ("…" if len(failed) > 10 else "") + "; left out of ranking.")
        uni = {"month": label, "source": WIKI_SP500, "stocks": [r for r in rows if "marketValue" in r]}
        save_json(path, uni)
        ctx.report.add("Info", f"S&P 500 list refreshed for {label}: {len(uni['stocks'])} stocks ranked.")
        return uni
    except Exception as e:
        if old:
            ctx.report.add("Warnings", f"S&P 500 list not refreshed ({e}); using the {old['month']} list.")
            return old
        raise Fatal(f"No S&P 500 list yet and the refresh failed: {e}")


def stock_candidates(universe, per_sector):
    by_sector = {}
    for s in universe["stocks"]:
        by_sector.setdefault(s["sector"], []).append(s)
    picks = []
    for sector in sorted(by_sector):
        ranked = sorted(by_sector[sector], key=lambda s: (-s["marketValue"], s["ticker"]))
        picks.extend(ranked[:per_sector])
    return picks


# ---------------------------------------------------------------- step 2: daily check
def daily_check(ctx, tickers):
    frames = ctx.yahoo.history(sorted(set(tickers) | {MARKET_INDEX}), period="2mo", interval="1d")
    cal = frames.get(MARKET_INDEX)
    if cal is None or cal.empty:
        raise Fatal("No daily S&P 500 prices came back. Yahoo may be rate limiting; the next run will try again.")
    dates = sorted({d.normalize() for d in naive_index(cal.index)})
    confirmed = {(e["ticker"], e["date"]) for e in ctx.settings.get("confirmedEvents", [])}
    stale, jumps = [], []
    for t in tickers:
        f = frames.get(t)
        if f is None or f["Close"].dropna().empty:
            stale.append(t)
            continue
        closes = f["Close"].dropna()
        closes.index = naive_index(closes.index)
        last = closes.index.max().normalize()
        if sum(1 for d in dates if d > last) > ctx.settings["staleTradingDays"]:
            stale.append(t)
        moves = closes.pct_change().tail(5)
        for d, v in moves.items():
            day = pd.to_datetime(d).date().isoformat()
            if v is not None and not pd.isna(v) and abs(v) > ctx.settings["dailyMoveLimit"] and (t, day) not in confirmed:
                jumps.append((t, day, float(v)))
    return dates, stale, jumps, frames


# ---------------------------------------------------------------- step 3: monthly history
def load_store(path):
    if Path(path).exists():
        df = pd.read_csv(path, index_col=0)
        df.index = df.index.astype(str)
        return df.astype(float)
    return pd.DataFrame(dtype=float)


def update_history(ctx, tickers):
    close = load_store(ctx.state / "monthly_close.csv")
    divs = load_store(ctx.state / "monthly_dividends.csv")
    first = add_months(ctx.settings["historyStart"], -1)
    start_full = first + "-01"
    start_recent = add_months(month_label(ctx.today), -4) + "-01"
    have = [t for t in tickers if t in close.columns and close[t].notna().any()]
    new = [t for t in tickers if t not in have]
    recent = ctx.yahoo.history(have, start=start_recent, interval="1mo", actions=True) if have else {}
    # a re-based history (stock split) shows up as old stored months no longer matching Yahoo
    rebased = []
    for t, f in recent.items():
        for d, price in zip(naive_index(f.index), f["Close"].astype(float).values):
            lab = month_label(d)
            if lab < month_label(ctx.today) and lab in close.index and not pd.isna(close.at[lab, t]):
                stored = close.at[lab, t]
                if stored > 0 and abs(price / stored - 1) > 0.01:
                    rebased.append(t)
                    break
    full = ctx.yahoo.history(new + rebased, start=start_full, interval="1mo", actions=True) if (new or rebased) else {}
    if rebased:
        ctx.report.add("Info", "Full history fetched again after Yahoo re-based it (usually a split): " + ", ".join(rebased) + ".")
        close = close.drop(columns=[t for t in rebased if t in close.columns])
        divs = divs.drop(columns=[t for t in rebased if t in divs.columns])
    new_close, new_divs = {}, {}
    for source in (recent, full):
        for t, f in source.items():
            labels = [month_label(d) for d in naive_index(f.index)]
            new_close[t] = pd.Series(f["Close"].astype(float).values, index=labels)
            d = f["Dividends"].astype(float).fillna(0.0).values if "Dividends" in f.columns else [0.0] * len(labels)
            new_divs[t] = pd.Series(d, index=labels)
    if new_close:
        nc = pd.DataFrame(new_close)
        nd = pd.DataFrame(new_divs)
        nc = nc[~nc.index.duplicated(keep="last")]
        nd = nd[~nd.index.duplicated(keep="last")]
        close = nc.combine_first(close) if len(close) else nc
        divs = nd.combine_first(divs) if len(divs) else nd
    close = close.sort_index()
    divs = divs.reindex(index=close.index, columns=close.columns).fillna(0.0)
    missing = [t for t in tickers if t not in close.columns or close[t].dropna().empty]
    return close, divs, missing


def monthly_returns(close, months):
    labels = [add_months(months[0], -1)] + months
    sub = close.reindex(labels)
    out = {}
    for t in sub.columns:
        s = sub[t]
        r = (s / s.shift(1) - 1).loc[months]
        out[t] = [clean(v) for v in r.tolist()]
    return out


def splice(own, proxy, fee_own, fee_proxy):
    """Before the fund's first month, use the proxy's return adjusted for the fee difference."""
    first = next((i for i, v in enumerate(own) if v is not None), len(own))
    out, used = list(own), 0
    for i in range(first):
        if proxy[i] is not None:
            out[i] = round(proxy[i] + (fee_proxy - fee_own) / 12, 6)
            used += 1
    return out, used


def ttm_yield(divs, close, ticker, last_month):
    if ticker not in close.columns or last_month not in close.index or pd.isna(close.at[last_month, ticker]):
        return None
    months = month_range(add_months(last_month, -11), last_month)
    total = sum(0.0 if (m not in divs.index or pd.isna(divs.at[m, ticker])) else float(divs.at[m, ticker]) for m in months)
    return clean(total / close.at[last_month, ticker])


# ---------------------------------------------------------------- funds
def facts_from_yahoo(info):
    out = {}
    er = info.get("netExpenseRatio") or info.get("annualReportExpenseRatio")
    if er is not None:
        er = float(er)
        out["expenseRatio"] = er / 100 if er > 0.02 else er  # Yahoo usually gives 0.03 meaning 0.03%
    y = info.get("yield")
    if y is not None:
        y = float(y)
        out["yield"] = y / 100 if y > 0.5 else y
    if info.get("totalAssets"):
        out["assets"] = round(float(info["totalAssets"]) / 1e9, 3)
    if info.get("fundInceptionDate"):
        out["inception"] = dt.datetime.fromtimestamp(int(info["fundInceptionDate"]), dt.timezone.utc).date().isoformat()
    return out


def resolve_fund(ctx, slot, f):
    t = f["ticker"]
    facts = dict(f)
    blanks = [k for k in FACT_LABELS if facts.get(k) is None]
    if blanks:
        try:
            yahoo = facts_from_yahoo(ctx.yahoo.info(t))
        except Exception as e:
            yahoo = {}
            ctx.report.add("Warnings", f"No Yahoo facts for {t}: {e}")
        for k in blanks:
            if yahoo.get(k) is not None:
                facts[k] = yahoo[k]
                shown = f"{yahoo[k] * 100:.2f}%" if k in ("expenseRatio", "yield") else (f"${yahoo[k]:,.1f} billion" if k == "assets" else yahoo[k])
                ctx.unverified.append(f"{t} {FACT_LABELS[k]} {shown}")
    if facts.get("spread") is None:
        facts["spread"] = ctx.settings["fundSpreadAssumed"]
        ctx.assumptions.append(f"{t}: bid-ask spread assumed at {ctx.settings['fundSpreadAssumed'] * 100:.2f}% until its published median spread is entered")
    cls, kind = SLOT_CLASS[slot]
    reasons = []
    for k in ("expenseRatio", "yield"):
        if facts.get(k) is None:
            reasons.append(f"no {FACT_LABELS[k]}")
    if cls == "bond":
        if facts.get("duration") is None:
            reasons.append("no duration (enter it from the fund page)")
        if facts.get("convexity") is None:
            facts["convexity"] = 0.0
            ctx.assumptions.append(f"{t}: convexity not entered, treated as 0")
    if facts.get("assets") is None or facts["assets"] < ctx.settings["minFundAssetsBillions"]:
        reasons.append("assets under $1 billion or unknown")
    if slot != "bitcoin":
        inc = facts.get("inception")
        cutoff = ctx.today.replace(year=ctx.today.year - int(ctx.settings["minFundYears"]))
        if not inc or dt.date.fromisoformat(inc[:10]) > cutoff:
            reasons.append("less than 5 years of history or unknown inception")
    as_of = facts.get("factsAsOf")
    if as_of and (ctx.today - dt.date.fromisoformat(as_of)).days > ctx.settings["factsMaxAgeDays"]:
        ctx.report.add("Warnings", f"{t} duration, convexity and yield are from {as_of}; update them in funds.json.")
    return facts, cls, kind, reasons


# ---------------------------------------------------------------- scenarios
class SeriesCache:
    def __init__(self, ctx):
        self.ctx, self.fred, self.daily = ctx, {}, {}

    def fred_series(self, sid):
        if sid not in self.fred:
            txt = retry(lambda: self.ctx.net.text(FRED_CSV.format(sid), plain=True), f"FRED {sid}", tries=2, wait=5)
            df = pd.read_csv(io.StringIO(txt))
            df.columns = ["date", "value"]
            df["date"] = pd.to_datetime(df["date"])
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            self.fred[sid] = df.dropna().set_index("date")["value"].sort_index()
        return self.fred[sid]

    def yahoo_daily(self, ticker, start, end):
        key = (ticker, start, end)
        if key not in self.daily:
            s = (pd.Timestamp(start) - pd.Timedelta(days=12)).date().isoformat()
            e = (pd.Timestamp(end) + pd.Timedelta(days=4)).date().isoformat()
            frames = self.ctx.yahoo.history([ticker], start=s, end=e, interval="1d")
            f = frames.get(ticker)
            self.daily[key] = None if f is None else f["Close"].dropna()
        return self.daily[key]


def value_on(series, date, max_gap_days=10):
    """Last value on or before a date, if it is no more than max_gap_days old."""
    if series is None or len(series) == 0:
        return None
    ts = pd.Timestamp(date)
    s = pd.Series(series.values, index=naive_index(series.index)).sort_index()
    s = s[s.index <= ts]
    if len(s) == 0 or (ts - s.index[-1]).days > max_gap_days:
        return None
    return float(s.iloc[-1])


def compute_field(cache, field, start, end):
    if field in ("stocks", "gold", "commodities"):
        ticker = {"stocks": MARKET_INDEX, "gold": "GLD", "commodities": "DBC"}[field]
        px = cache.yahoo_daily(ticker, start, end)
        a, b = value_on(px, start), value_on(px, end)
        return clean(b / a - 1) if a and b else None
    if field in ("rate", "realRate"):
        s = cache.fred_series("DGS10" if field == "rate" else "DFII10")
        a, b = value_on(s, start), value_on(s, end)
        return clean(b - a, 4) if a is not None and b is not None else None
    if field == "cpi":
        s = cache.fred_series("CPIAUCNS")
        a, b = value_on(s, pd.Timestamp(start).replace(day=1), 40), value_on(s, pd.Timestamp(end).replace(day=1), 40)
        return clean(b / a - 1) if a and b else None
    return None


def build_scenarios(ctx, cfg):
    path = ctx.state / "scenario_values.json"
    key = hashlib.sha256((ctx.settings["rulesVersion"] + json.dumps(cfg["scenarios"], sort_keys=True)).encode()).hexdigest()[:16]
    store = load_json(path, {})
    if store.get("key") != key:
        store = {"key": key, "values": {}}
    cache = SeriesCache(ctx)
    out = []
    for s in cfg["scenarios"]:
        row = {"id": s["id"], "name": s["name"], "years": s["years"]}
        missing = []
        for field in ("stocks", "rate", "realRate", "gold", "commodities", "cpi"):
            v = s.get(field)
            if v == "compute":
                saved = store["values"].get(s["id"], {})
                if field in saved:
                    v = saved[field]
                else:
                    try:
                        v = compute_field(cache, field, s["start"], s["end"])
                    except Exception as e:
                        ctx.report.add("Warnings", f"{s['name']}: could not compute {field} ({e}); will retry next run.")
                        v = None
                    else:
                        store["values"].setdefault(s["id"], {})[field] = v
            elif v == "assume0":
                v = 0.0
                ctx.assumptions.append(f"{s['name']}: {field} has no data and is assumed to be 0")
            if v is None and field != "cpi":
                missing.append(field)
            row[field] = v
        if missing:
            ctx.report.add("Info", f"{s['name']} is skipped by the engine until these have data: {', '.join(missing)}.")
        out.append(row)
    ctx.pending_state[path] = store
    return out


# ---------------------------------------------------------------- optional cross-check
def cross_check(ctx, close, month, tickers):
    token = os.environ.get("TIINGO_TOKEN")
    if not token:
        return
    pool = sorted(t for t in tickers if "-" not in t and "^" not in t)
    if not pool:
        return
    seed = int(hashlib.sha256(month.encode()).hexdigest(), 16)
    sample = [pool[(seed + i * 7919) % len(pool)] for i in range(min(ctx.settings["crossCheckSample"], len(pool)))]
    first = month + "-01"
    last = (pd.Timestamp(first) + pd.offsets.MonthEnd(0)).date().isoformat()
    bad = []
    for t in sorted(set(sample)):
        try:
            rows = ctx.net.json(TIINGO_PRICES.format(t), params={"startDate": first, "endDate": last, "token": token})
            theirs = float(rows[-1]["close"])
            ours = float(close.at[month, t])
            if abs(ours / theirs - 1) > ctx.settings["crossCheckTolerance"]:
                bad.append(f"{t}: Yahoo {ours:.2f} vs Tiingo {theirs:.2f}")
        except Exception as e:
            ctx.report.add("Warnings", f"Cross-check of {t} failed: {e}")
    if bad:
        ctx.report.add("Warnings", f"Cross-check for {month} found differences over {ctx.settings['crossCheckTolerance'] * 100:.1f}%: " + "; ".join(bad))
    else:
        ctx.report.add("Info", f"Cross-check for {month}: {len(set(sample))} prices matched Tiingo.")


# ---------------------------------------------------------------- main
class Context:
    def __init__(self, root, yf, net, today):
        self.root = Path(root)
        self.config = self.root / "config"
        self.state = self.root / "state"
        self.site = self.root / "site"
        self.settings = load_json(self.config / "settings.json")
        self.today = today
        self.report = Report()
        self.yf = yf
        self.yahoo = Yahoo(yf, self.settings)
        self.net = net
        self.unverified, self.assumptions, self.excluded = [], [], []
        self.pending_state = {}


def build(ctx):
    funds_cfg = load_json(ctx.config / "funds.json")
    scen_cfg = load_json(ctx.config / "scenarios.json")
    checked = funds_cfg.get("checked")
    if not checked:
        ctx.report.add("Warnings", "Fund facts have never been checked by hand. Fill in config/funds.json from the fund pages and set \"checked\".")
    elif (ctx.today - dt.date.fromisoformat(checked)).days > ctx.settings["factsMaxAgeDays"]:
        ctx.report.add("Warnings", f"Fund facts were last checked on {checked}. Time for the monthly update of config/funds.json.")

    # step 1
    say("1/6 S&P 500 list and market values")
    universe = refresh_universe(ctx)
    stocks = stock_candidates(universe, int(ctx.settings["stocksPerSector"]))
    funds = [(slot, f) for slot, lst in funds_cfg["slots"].items() for f in lst]
    proxies = sorted({f["proxy"]["ticker"] for _, f in funds if f.get("proxy")})
    fund_tickers = [f["ticker"] for _, f in funds]
    stock_tickers = [s["ticker"] for s in stocks]
    tickers = sorted(set(fund_tickers + stock_tickers + proxies + [MARKET_INDEX]))

    # step 2
    say("2/6 Latest daily prices")
    dates, stale, jumps, daily = daily_check(ctx, fund_tickers + stock_tickers)
    if jumps:
        for t, day, v in jumps:
            ctx.report.add("Blocking", f"{t} moved {v * 100:+.1f}% on {day}, over the {ctx.settings['dailyMoveLimit'] * 100:.0f}% limit. Check it; if it is real, add it to confirmedEvents in config/settings.json.")
        raise Fatal("Daily moves over the limit need review.")
    for t in stale:
        ctx.excluded.append(f"{t}: latest price is missing or more than {ctx.settings['staleTradingDays']} trading days old")

    # step 3
    say("3/6 Monthly price history")
    close, divs, missing = update_history(ctx, tickers)
    last_month = add_months(month_label(ctx.today), -1)
    months = month_range(ctx.settings["historyStart"], last_month)
    returns = monthly_returns(close, months)
    gspc = daily.get(MARKET_INDEX)
    as_of = None
    if gspc is not None:
        in_month = [d for d in naive_index(gspc.index) if month_label(d) == last_month]
        as_of = max(in_month).date().isoformat() if in_month else None
    as_of = as_of or last_month

    # step 4: holdings
    holdings = []
    for slot, f in funds:
        t = f["ticker"]
        if t in missing:
            ctx.excluded.append(f"{t}: no monthly price history from Yahoo")
        if t in stale or t in missing:
            continue
        facts, cls, kind, reasons = resolve_fund(ctx, slot, f)
        own = returns.get(t, [None] * len(months))
        if f.get("proxy"):
            p = f["proxy"]
            pr = returns.get(p["ticker"], [None] * len(months))
            fee_p = p.get("expenseRatio")
            if fee_p is None:
                fee_p = 0.0
                ctx.assumptions.append(f"{t}: proxy {p['ticker']} fee not entered, treated as 0 when splicing")
            own, used = splice(own, pr, facts.get("expenseRatio") or 0.0, fee_p)
            if used:
                ctx.report.add("Info", f"{t}: {used} months before it existed use {p['ticker']} minus the fee difference.")
        if own[-1] is None:
            reasons.append(f"no return for {last_month}")
        if reasons:
            ctx.excluded.append(f"{t}: " + "; ".join(reasons))
            continue
        h = {"ticker": t, "name": facts.get("name", t), "slot": slot, "cls": cls, "kind": kind,
             "expenseRatio": facts["expenseRatio"], "yield": facts["yield"], "spread": facts["spread"],
             "tax": facts["tax"], "assets": facts.get("assets"), "returns": own}
        if f.get("role") == "market":
            h["role"] = "market"
        if cls == "bond":
            h.update({"duration": facts["duration"], "convexity": facts["convexity"], "rateType": facts.get("rateType", "nominal")})
        holdings.append(h)
    if not any(h.get("role") == "market" for h in holdings):
        raise Fatal("The market fund (role \"market\" in funds.json) is not usable this run, so betas can't be measured.")
    for s in stocks:
        t = s["ticker"]
        if t in stale or t in missing:
            continue
        r = returns.get(t, [None] * len(months))
        if r[-1] is None:
            ctx.excluded.append(f"{t}: no return for {last_month}")
            continue
        y = ttm_yield(divs, close, t, last_month)
        holdings.append({"ticker": t, "name": s["name"], "cls": "stock", "kind": "Stock", "sector": s["sector"],
                         "marketValue": s["marketValue"], "expenseRatio": 0.0, "yield": y if y is not None else 0.0,
                         "spread": ctx.settings["stockSpreadAssumed"], "tax": "qualified", "returns": r})
    ctx.assumptions.append(f"S&P 500 stocks: bid-ask spread assumed at {ctx.settings['stockSpreadAssumed'] * 100:.2f}%")
    for h in holdings:
        big = [months[i] for i, v in enumerate(h["returns"]) if v is not None and abs(v) > 0.6 and h["cls"] != "bitcoin"]
        if big:
            ctx.report.add("Warnings", f"{h['ticker']} has monthly moves over 60% in {', '.join(big[:3])}; worth a look.")

    # step 5: scenarios and expected inflation
    say("5/6 Scenario values and FRED data")
    scenarios = build_scenarios(ctx, scen_cfg)
    try:
        t5 = SeriesCache(ctx).fred_series("T5YIE")
        expected = clean(float(t5.iloc[-1]) / 100, 5)
    except Exception as e:
        prev = load_json(ctx.site / "data.json")
        if not prev:
            raise Fatal(f"No expected inflation from FRED: {e}")
        expected = prev["expectedInflation"]
        ctx.report.add("Warnings", f"Expected inflation not refreshed ({e}); kept {expected * 100:.2f}%.")

    # optional cross-check when a month completes
    prev = load_json(ctx.site / "data.json") or {}
    if (prev.get("months") or [None])[-1] != last_month:
        cross_check(ctx, close, last_month, [h["ticker"] for h in holdings])

    for m in ctx.unverified:
        ctx.report.add("Unverified facts", m)
    for m in ctx.assumptions:
        ctx.report.add("Assumptions", m)
    for m in ctx.excluded:
        ctx.report.add("Excluded", m)
    try:
        import importlib.metadata as md
        yf_version = md.version("yfinance")
    except Exception:
        yf_version = getattr(ctx.yf, "__version__", "unknown")
    data = {
        "label": "Nightly data", "asOf": as_of, "expectedInflation": expected,
        "months": months, "holdings": holdings, "scenarios": scenarios,
        "build": {"rulesVersion": ctx.settings["rulesVersion"],
                  "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
                  "latestDailyClose": dates[-1].date().isoformat(), "yfinance": yf_version,
                  "unverified": sorted(set(ctx.unverified)), "assumptions": sorted(set(ctx.assumptions)),
                  "excluded": sorted(set(ctx.excluded))}
    }
    ctx.pending_state[ctx.state / "monthly_close.csv"] = close
    ctx.pending_state[ctx.state / "monthly_dividends.csv"] = divs
    return data


def engine_check(ctx, path):
    try:
        proc = subprocess.run(["node", str(ctx.root / "scripts" / "check_data.js"), str(path)],
                              capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise Fatal("Node.js is needed for the engine check. Install the LTS version from nodejs.org, then run this again.")
    ctx.report.check_output = proc.stdout[-6000:] + (("\n" + proc.stderr[-2000:]) if proc.stderr else "")
    return proc.returncode


def save_state(ctx):
    for path, value in ctx.pending_state.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, pd.DataFrame):
            value.to_csv(path)
        else:
            save_json(path, value)


def write_summary(ctx, title):
    text = ctx.report.markdown(title)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def main(root=None, yf=None, net=None, today=None):
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is needed (you have " + sys.version.split()[0] + "). Install Python 3.12 from python.org.")
        return 2
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    if yf is None:
        import yfinance as yf  # noqa: F811
    settings = load_json(root / "config" / "settings.json")
    if net is None:
        net = Net(settings["userAgent"])
    ctx = Context(root, yf, net, today or dt.datetime.now(dt.timezone.utc).date())
    try:
        data = build(ctx)
        new_path = ctx.site / "data.new.json"
        save_json(new_path, data)
        try:
            say("6/6 Engine check")
            code = engine_check(ctx, new_path)
        except Fatal:
            new_path.unlink(missing_ok=True)
            raise
        if code != 0:
            ctx.report.add("Blocking", "The engine check failed, so yesterday's data stays published. See the engine output below.")
            new_path.unlink(missing_ok=True)
            raise Fatal("engine check failed")
        os.replace(new_path, ctx.site / "data.json")
        save_state(ctx)
        write_summary(ctx, f"Nightly data built: as of {data['asOf']}, {len(data['holdings'])} holdings")
        return 0
    except Fatal as e:
        ctx.report.add("Blocking", str(e))
        write_summary(ctx, "Nightly data NOT updated: yesterday's data stays published")
        return 1


if __name__ == "__main__":
    sys.exit(main())
