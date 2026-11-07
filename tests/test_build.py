"""Offline tests for scripts/build_data.py.

Fake Yahoo, Wikipedia and FRED responses stand in for the internet, so these run anywhere:
    python -m unittest discover -s tests -v
Every number below is test data, made up to exercise the code.
"""
import datetime as dt
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_data as B  # noqa: E402

TODAY = dt.date(2026, 11, 4)
START = pd.Timestamp("1999-11-01")
INCEPTION = {"VTI": "2001-06", "ITOT": "2004-01", "SCHB": "2009-11", "VXUS": "2011-02", "IXUS": "2012-11",
             "IEF": "2002-08", "TLT": "2002-08", "SCHP": "2010-08", "TIP": "2004-01", "IAUM": "2021-07",
             "GLDM": "2018-07", "IAU": "2005-02", "GLD": "2004-12", "PDBC": "2014-12", "BCI": "2017-04",
             "DJP": "2006-07", "DBC": "2006-03", "IBIT": "2024-02", "FBTC": "2024-02", "BTC-USD": "2014-10"}
FEES = {"VTI": 0.03, "ITOT": 0.03, "SCHB": 0.03, "VXUS": 0.05, "IXUS": 0.07, "IEF": 0.15, "TLT": 0.15,
        "SCHP": 0.03, "TIP": 0.18, "IAUM": 0.09, "GLDM": 0.10, "IAU": 0.25, "PDBC": 0.59, "BCI": 0.25,
        "DBC": 0.85, "IBIT": 0.25, "FBTC": 0.25}
ASSETS = {"VTI": 500e9, "ITOT": 70e9, "SCHB": 30e9, "IBIT": 70e9, "FBTC": 20e9}
SECTORS = ["Information Technology", "Communication Services", "Consumer Discretionary", "Financials", "Health Care",
           "Energy", "Consumer Staples", "Industrials", "Materials", "Utilities", "Real Estate"]
STOCKS = [(f"S{i:03d}", f"Test company {i}", SECTORS[i % 11]) for i in range(460)]


def seed(t):
    return int(hashlib.sha256(t.encode()).hexdigest()[:8], 16)


class FakeTicker:
    def __init__(self, yf, t):
        self.yf, self.t = yf, t

    @property
    def info(self):
        inc = INCEPTION.get(self.t, "2000-01") + "-01"
        return {"netExpenseRatio": FEES.get(self.t), "yield": 0.02, "totalAssets": ASSETS.get(self.t, 10e9),
                "fundInceptionDate": int(pd.Timestamp(inc, tz="UTC").timestamp())}

    @property
    def fast_info(self):
        return {"marketCap": 1e9 * (5 + seed(self.t) % 3000)}


class FakeYF:
    __version__ = "test"

    def __init__(self, split=None, jump=None):
        self.split, self.jump, self.cache = split or {}, jump or {}, {}

    def daily(self, t):
        if t not in self.cache:
            days = pd.bdate_range(START, pd.Timestamp(TODAY) - pd.Timedelta(days=1))
            rng = np.random.default_rng(seed(t))
            vol = 0.035 if t in ("BTC-USD", "IBIT", "FBTC") else 0.01
            px = 50 * np.exp(np.cumsum(rng.normal(0.0003, vol, len(days))))
            s = pd.Series(px, index=days)
            first = INCEPTION.get(t)
            if first:
                s = s[s.index >= pd.Timestamp(first + "-01")]
            self.cache[t] = s
        s = self.cache[t].copy()
        if t in self.split:
            s[s.index < pd.Timestamp(TODAY) - pd.Timedelta(days=200)] *= self.split[t]
        if t in self.jump:
            s.iloc[-1] = s.iloc[-2] * (1 + self.jump[t])
        return s

    def download(self, tickers, start=None, end=None, period=None, interval="1d", actions=False, **kw):
        frames = {}
        for t in tickers:
            if t.startswith("S") and t[1:].isdigit() and int(t[1:]) >= 460:
                continue
            s = self.daily(t)
            if start:
                s = s[s.index >= pd.Timestamp(start)]
            if end:
                s = s[s.index < pd.Timestamp(end)]
            if period == "2mo":
                s = s[s.index >= pd.Timestamp(TODAY) - pd.Timedelta(days=62)]
            if interval == "1mo":
                m = s.groupby(s.index.to_period("M")).last()
                m.index = m.index.to_timestamp()
                df = pd.DataFrame({"Close": m.values}, index=m.index)
                df["Dividends"] = [(0.004 * c if (d.month % 3 == 0 and t.startswith("S")) else 0.0) for d, c in zip(df.index, df["Close"])]
            else:
                df = pd.DataFrame({"Close": s.values}, index=s.index)
            if len(df):
                frames[t] = df
        return pd.concat(frames, axis=1, sort=True) if frames else pd.DataFrame()

    def Ticker(self, t):
        return FakeTicker(self, t)


class FakeNet:
    def text(self, url, params=None, plain=False, limit=45):
        if "wikipedia" in url:
            rows = "".join(f"<tr><td>{t}</td><td>{n}</td><td>{s}</td></tr>" for t, n, s in STOCKS)
            return f"<table><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>{rows}</table>"
        sid = url.split("id=")[1]
        if sid == "CPIAUCNS":
            idx = pd.date_range("1990-01-01", "2026-09-01", freq="MS")
            vals = 130 * (1.0022 ** np.arange(len(idx)))
        else:
            first = {"DGS10": "1990-01-02", "DFII10": "2003-01-02", "T5YIE": "2003-01-02"}[sid]
            idx = pd.bdate_range(first, "2026-11-03")
            base = {"DGS10": 4.0, "DFII10": 1.5, "T5YIE": 2.3}[sid]
            vals = base + 0.5 * np.sin(np.arange(len(idx)) / 400)
        df = pd.DataFrame({"observation_date": idx.strftime("%Y-%m-%d"), sid: np.round(vals, 2)})
        return df.to_csv(index=False)

    def json(self, url, params=None):
        raise AssertionError("no Tiingo token is set in tests")


def make_project():
    tmp = Path(tempfile.mkdtemp())
    for d in ("config", "scripts", "site"):
        shutil.copytree(ROOT / d, tmp / d)
    (tmp / "state").mkdir()
    (tmp / "site" / "data.json").unlink(missing_ok=True)
    settings = json.loads((tmp / "config" / "settings.json").read_text())
    settings["pauseSeconds"] = 0
    (tmp / "config" / "settings.json").write_text(json.dumps(settings))
    funds = json.loads((tmp / "config" / "funds.json").read_text())
    funds["checked"] = "2026-11-01"
    for f in funds["slots"]["tips"]:
        f["duration"], f["convexity"] = 6.5, 0.5   # test values; real ones come from the fund pages
    (tmp / "config" / "funds.json").write_text(json.dumps(funds))
    return tmp


class NightlyTests(unittest.TestCase):
    def test_full_run_then_split_then_jump(self):
        root = make_project()
        code = B.main(root=root, yf=FakeYF(), net=FakeNet(), today=TODAY)
        self.assertEqual(code, 0)
        data = json.loads((root / "site" / "data.json").read_text())
        self.assertEqual(data["months"][0], "2000-01")
        self.assertEqual(data["months"][-1], "2026-10")
        self.assertEqual(sum(1 for h in data["holdings"] if h.get("role") == "market"), 1)
        by = {h["ticker"]: h for h in data["holdings"]}
        i2019 = data["months"].index("2019-01")
        self.assertIsNotNone(by["IAUM"]["returns"][i2019], "IAUM should use GLD before it existed")
        i2015 = data["months"].index("2015-01")
        self.assertIsNotNone(by["IBIT"]["returns"][i2015], "IBIT should use bitcoin's own history")
        self.assertAlmostEqual(by["VTI"]["expenseRatio"], 0.0003)
        self.assertIn("VTI expense ratio 0.03%", data["build"]["unverified"])
        sc = {s["id"]: s for s in data["scenarios"]}
        self.assertIsNone(sc["dotcom"]["realRate"], "real yields start in 2003")
        self.assertIsNotNone(sc["covid"]["stocks"])
        self.assertEqual(sc["crash"]["realRate"], 0.0)
        stocks = [h for h in data["holdings"] if h["kind"] == "Stock"]
        self.assertEqual(len(stocks), 22)
        self.assertTrue(all(abs(v) < 1 for h in data["holdings"] for v in h["returns"] if v is not None))

        # a split re-bases IEF's history: the job must fetch it again rather than show a fake drop
        code = B.main(root=root, yf=FakeYF(split={"IEF": 0.5}), net=FakeNet(), today=TODAY)
        self.assertEqual(code, 0)
        data2 = json.loads((root / "site" / "data.json").read_text())
        ief = {h["ticker"]: h for h in data2["holdings"]}["IEF"]["returns"]
        self.assertTrue(all(abs(v) < 0.2 for v in ief if v is not None), "no fake split drop")

        # a 40% daily move stops the run and leaves the published file untouched
        before = (root / "site" / "data.json").read_text()
        code = B.main(root=root, yf=FakeYF(jump={"VTI": 0.40}), net=FakeNet(), today=TODAY)
        self.assertEqual(code, 1)
        self.assertEqual((root / "site" / "data.json").read_text(), before)
        shutil.rmtree(root)

    def test_splice_and_returns(self):
        own = [None, None, 0.01, 0.02]
        proxy = [0.05, 0.06, 0.07, 0.08]
        out, used = B.splice(own, proxy, fee_own=0.0012, fee_proxy=0.0024)
        self.assertEqual(used, 2)
        self.assertAlmostEqual(out[0], 0.05 + 0.0001)
        self.assertEqual(out[2:], [0.01, 0.02])
        close = pd.DataFrame({"X": [100.0, 110.0, 99.0]}, index=["1999-12", "2000-01", "2000-02"])
        r = B.monthly_returns(close, ["2000-01", "2000-02"])["X"]
        self.assertAlmostEqual(r[0], 0.1)
        self.assertAlmostEqual(r[1], -0.1)

    def test_yahoo_fact_units(self):
        f = B.facts_from_yahoo({"netExpenseRatio": 0.59, "yield": 0.031, "totalAssets": 5.1e9})
        self.assertAlmostEqual(f["expenseRatio"], 0.0059)
        self.assertAlmostEqual(f["yield"], 0.031)
        self.assertAlmostEqual(f["assets"], 5.1)


if __name__ == "__main__":
    unittest.main()
