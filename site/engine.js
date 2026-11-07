/* Three Choices engine, rules v1.0. Pure functions, no page code. */
(function (root) {
  'use strict';

  var RULES_VERSION = 'v1.0';
  var PATHS = 10000;
  var COV_MONTHS = 60;

  // ---------- small helpers ----------
  function fnv1a(str) {
    var h = 0x811c9dc5;
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h >>> 0;
  }
  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function normalGen(rng) {
    var spare = null;
    return function () {
      if (spare !== null) { var s = spare; spare = null; return s; }
      var u = 0;
      while (u === 0) u = rng();
      var v = rng();
      var mag = Math.sqrt(-2 * Math.log(u));
      spare = mag * Math.sin(2 * Math.PI * v);
      return mag * Math.cos(2 * Math.PI * v);
    };
  }
  function sum(a) { var s = 0; for (var i = 0; i < a.length; i++) s += a[i]; return s; }
  function clamp(x, lo, hi) { return Math.min(hi, Math.max(lo, x)); }
  function round2(x) { return Math.round(x * 100) / 100; }
  function byTicker(a, b) { return a.ticker < b.ticker ? -1 : a.ticker > b.ticker ? 1 : 0; }

  function monthList(y0, m0, y1, m1) {
    var out = [], y = y0, m = m0;
    while (y < y1 || (y === y1 && m <= m1)) {
      out.push(y + '-' + (m < 10 ? '0' : '') + m);
      m++; if (m > 12) { m = 1; y++; }
    }
    return out;
  }

  // ---------- rule book constants ----------
  var CHOICES = [
    { id: 'lower', name: 'Lower risk', target: 0.065, swing: [0.05, 0.08], worstLimit: -0.15, nStocks: 3, stockCap: 0.03, sectorCap: 0.25,
      bands: { stock: [0.25, 0.35], bond: [0.45, 0.60], real: [0.10, 0.15], bitcoin: [0, 0] } },
    { id: 'balanced', name: 'Balanced', target: 0.105, swing: [0.09, 0.12], worstLimit: -0.30, nStocks: 6, stockCap: 0.05, sectorCap: 0.25,
      bands: { stock: [0.45, 0.60], bond: [0.25, 0.40], real: [0.10, 0.15], bitcoin: [0, 0.02] } },
    { id: 'higher', name: 'Higher growth', target: 0.16, swing: [0.14, 0.18], worstLimit: -0.45, nStocks: 11, stockCap: 0.08, sectorCap: 0.30,
      bands: { stock: [0.70, 0.85], bond: [0.05, 0.15], real: [0.05, 0.10], bitcoin: [0, 0.05] } }
  ];
  var CLASSES = ['stock', 'bond', 'real', 'bitcoin'];
  var SLOTS = ['us-stock', 'intl-stock', 'tsy-7-10', 'tsy-20', 'tips', 'gold', 'commodities', 'bitcoin'];

  // IRS 2026 tables (Rev. Proc. 2025-32), taxable income.
  var ORDINARY = {
    single: [[12400, 0.10], [50400, 0.12], [105700, 0.22], [201775, 0.24], [256225, 0.32], [640600, 0.35], [Infinity, 0.37]],
    joint: [[24800, 0.10], [100800, 0.12], [211400, 0.22], [403550, 0.24], [512450, 0.32], [768700, 0.35], [Infinity, 0.37]]
  };
  var LONG_TERM = { single: [[49450, 0], [545500, 0.15], [Infinity, 0.20]], joint: [[98900, 0], [613700, 0.15], [Infinity, 0.20]] };
  var NIIT_THRESHOLD = { single: 200000, joint: 250000 };

  var DEFAULT_INPUTS = {
    amount: 10000, years: 1, lossLimit: null, account: 'taxable',
    filing: 'single', income: 100000, stateRate: 0, view: 'nominal'
  };

  function bracketRate(table, income) {
    for (var i = 0; i < table.length; i++) if (income <= table[i][0]) return table[i][1];
    return table[table.length - 1][1];
  }
  function taxRates(inp) {
    if (inp.account !== 'taxable') {
      return { qualified: 0, ordinary: 0, treasury: 0, k1: 0, gold: 0, longTerm: 0, state: 0, taxed: false };
    }
    var ord = bracketRate(ORDINARY[inp.filing], inp.income);
    var lt = bracketRate(LONG_TERM[inp.filing], inp.income);
    var niit = inp.income > NIIT_THRESHOLD[inp.filing] ? 0.038 : 0;
    var st = inp.stateRate;
    return {
      qualified: lt + niit + st,          // qualified dividends
      ordinary: ord + niit + st,          // ordinary income, short-term gains
      treasury: ord + niit,               // Treasury interest: no state tax
      k1: 0.6 * lt + 0.4 * ord + niit + st, // K-1 commodity marks, 60/40
      gold: Math.min(ord, 0.28) + niit + st, // collectibles cap
      longTerm: lt + niit + st,           // long-term gains
      state: st, taxed: true,
      parts: { ordinary: ord, longTerm: lt, niit: niit }
    };
  }

  // ---------- demo data (made up, clearly labeled) ----------
  function makeDemoData() {
    var months = monthList(2000, 1, 2026, 10);
    var n = months.length;
    var rng = mulberry32(fnv1a('demo-data-' + RULES_VERSION));
    var z = normalGen(rng);
    // noise with its average removed in every 60-month block, so each decade earns its intended average
    function noise(vol) {
      var a = [], t, b;
      for (t = 0; t < n; t++) a.push(vol * z());
      for (b = 0; b < n; b += 60) {
        var e = Math.min(n, b + 60), mean = 0;
        for (t = b; t < e; t++) mean += a[t];
        mean /= (e - b);
        for (t = b; t < e; t++) a[t] -= mean;
      }
      return a;
    }
    var shocks = { '2001-09': -0.08, '2002-07': -0.08, '2002-09': -0.10, '2008-09': -0.09, '2008-10': -0.17,
      '2008-11': -0.07, '2009-02': -0.10, '2020-03': -0.13, '2022-06': -0.08 };
    var btcStart = months.indexOf('2015-01');
    var nm = noise(0.040), ny = noise(0.20), nr = noise(0.12), ng = noise(0.042), nc = noise(0.05), nb = noise(0.17);
    var f = { m: [], dy: [], dr: [], g: [], c: [], b: [] };
    for (var t = 0; t < n; t++) {
      var ym = months[t], shock = shocks[ym] || 0;
      // stylized rate path: falls in late 2008 and 2019 to 2020, rises through 2022 (demo only)
      var drift = (ym >= '2008-07' && ym <= '2008-12') ? -0.15 : (ym >= '2019-01' && ym <= '2020-07') ? -0.08 : (ym >= '2022-01' && ym <= '2022-12') ? 0.20 : 0;
      var m = 0.0075 + nm[t] + shock;
      var dy = ny[t] + drift + (shock < 0 ? -0.25 : 0);
      f.m.push(m); f.dy.push(dy);
      f.dr.push(0.75 * dy + nr[t]);
      f.g.push(0.006 + ng[t] - 0.01 * dy + (shock < 0 ? 0.01 : 0));
      f.c.push(0.002 + nc[t] + 0.3 * (m - 0.0075));
      f.b.push(t >= btcStart ? 0.03 + nb[t] + 1.1 * (m - 0.0075) : null);
    }
    function series(fn) { var a = []; for (var t = 0; t < n; t++) a.push(fn(t)); return a; }
    function bond(dur, conv, key, vol) {
      var e = noise(vol);
      return series(function (t) { var d = f[key][t]; return (-dur * d + 0.5 * conv * d * d) / 100 + e[t]; });
    }
    function idio(vol) { var e = noise(vol); return function (t) { return e[t]; }; }
    var funds = [
      { ticker: 'DUSA', name: 'US stock fund', slot: 'us-stock', cls: 'stock', kind: 'Stock ETF', role: 'market',
        expenseRatio: 0.0003, yield: 0.013, spread: 0.0001, tax: 'qualified', assets: 400,
        returns: (function () { var e = idio(0.004); return series(function (t) { return f.m[t] + e(t); }); })() },
      { ticker: 'DINT', name: 'International stock fund', slot: 'intl-stock', cls: 'stock', kind: 'Stock ETF',
        expenseRatio: 0.0005, yield: 0.03, spread: 0.0002, tax: 'qualified', assets: 80,
        returns: (function () { var e = idio(0.022); return series(function (t) { return 0.9 * (f.m[t] - 0.0075) + 0.0068 + e(t); }); })() },
      { ticker: 'DT10', name: '7 to 10 year Treasury fund', slot: 'tsy-7-10', cls: 'bond', kind: 'Bond ETF',
        expenseRatio: 0.0015, yield: 0.042, duration: 7.0, convexity: 0.6, rateType: 'nominal', spread: 0.0001, tax: 'treasury', assets: 35,
        returns: bond(7.0, 0.6, 'dy', 0.002) },
      { ticker: 'DT20', name: '20+ year Treasury fund', slot: 'tsy-20', cls: 'bond', kind: 'Bond ETF',
        expenseRatio: 0.0015, yield: 0.046, duration: 15.5, convexity: 3.0, rateType: 'nominal', spread: 0.0001, tax: 'treasury', assets: 60,
        returns: bond(15.5, 3.0, 'dy', 0.003) },
      { ticker: 'DTIP', name: 'Inflation-protected Treasury fund', slot: 'tips', cls: 'bond', kind: 'Bond ETF',
        expenseRatio: 0.0003, yield: 0.035, duration: 6.5, convexity: 0.5, rateType: 'real', spread: 0.0001, tax: 'treasury', assets: 25,
        returns: bond(6.5, 0.5, 'dr', 0.002) },
      { ticker: 'DGLD', name: 'Gold fund', slot: 'gold', cls: 'real', kind: 'Gold ETF',
        expenseRatio: 0.0010, yield: 0, spread: 0.0002, tax: 'gold', assets: 15,
        returns: series(function (t) { return f.g[t]; }) },
      { ticker: 'DCOM', name: 'Commodity fund', slot: 'commodities', cls: 'real', kind: 'Commodity ETF',
        expenseRatio: 0.0059, yield: 0.03, spread: 0.0005, tax: 'commodity1099', assets: 5,
        returns: series(function (t) { return f.c[t]; }) },
      { ticker: 'DBTC', name: 'Bitcoin fund', slot: 'bitcoin', cls: 'bitcoin', kind: 'Bitcoin ETF',
        expenseRatio: 0.0025, yield: 0, spread: 0.0003, tax: 'bitcoin', assets: 70,
        returns: series(function (t) { return f.b[t]; }) }
    ];
    var sectorSpec = [
      ['Information Technology', 'IT', 4000, 3500], ['Communication Services', 'CS', 2500, 1500],
      ['Consumer Discretionary', 'CD', 2200, 1100], ['Financials', 'FN', 900, 700], ['Health Care', 'HC', 800, 500],
      ['Energy', 'EN', 500, 300], ['Consumer Staples', 'ST', 450, 350], ['Industrials', 'IN', 300, 250],
      ['Materials', 'MT', 200, 120], ['Utilities', 'UT', 150, 120], ['Real Estate', 'RE', 120, 100]
    ];
    var betas = [1.25, 1.35, 1.10, 1.20, 1.15, 1.30, 1.10, 1.05, 0.80, 0.85, 1.05, 0.95, 0.70, 0.75, 1.05, 1.10, 1.00, 0.95, 0.55, 0.60, 0.90, 0.95];
    var stocks = [];
    var k = 0;
    sectorSpec.forEach(function (s) {
      [2, 3].forEach(function (col, j) {
        var beta = betas[k], vol = 0.05 + 0.03 * ((k * 7) % 11) / 10;
        var mu = 0.0065 + 0.001 * (k % 3);
        stocks.push({ ticker: 'D' + s[1] + (j + 1), name: s[0] + ' company ' + (j === 0 ? 'A' : 'B'),
          cls: 'stock', kind: 'Stock', sector: s[0], marketValue: s[col],
          expenseRatio: 0, yield: 0.005 + 0.004 * (k % 5), spread: 0.0002, tax: 'qualified',
          returns: (function () { var e = idio(vol); return series(function (t) { return beta * (f.m[t] - 0.0075) + mu + e(t); }); })() });
        k++;
      });
    });
    var holdings = funds.concat(stocks).map(function (h) {
      h.returns = h.returns.map(function (r) { return r === null ? null : Math.round(r * 1e6) / 1e6; });
      return h;
    });
    return {
      label: 'Demo data', asOf: 'Demo', demo: true, expectedInflation: 0.025,
      months: months, holdings: holdings, scenarios: scenarioTable(true)
    };
  }

  // Scenario market moves from the rule book (sourced). Real-yield moves are demo values only.
  function scenarioTable(withDemoRealRates) {
    var d = withDemoRealRates;
    return [
      { id: 'none', name: 'No change', years: 1, stocks: 0, rate: 0, realRate: 0, gold: 0, commodities: 0, cpi: null },
      { id: 'boom', name: 'Boom (2017)', years: 1, stocks: 0.194, rate: -0.05, realRate: d ? -0.10 : null, gold: 0.131, commodities: 0.017, cpi: null },
      { id: 'recession', name: 'Recession (2008)', years: 1, stocks: -0.385, rate: -1.79, realRate: d ? 0.80 : null, gold: 0.043, commodities: -0.366, cpi: null },
      { id: 'inflation', name: 'Inflation (2022)', years: 1, stocks: -0.194, rate: 2.37, realRate: d ? 2.50 : null, gold: -0.003, commodities: 0.161, cpi: null },
      { id: 'disinflation', name: 'Disinflation (2023)', years: 1, stocks: 0.242, rate: 0.00, realRate: d ? 0.20 : null, gold: 0.130, commodities: -0.079, cpi: null },
      { id: 'dotcom', name: 'Dot-com crash (2000 to 2002)', years: 3, stocks: null, rate: null, realRate: null, gold: null, commodities: null, cpi: null },
      { id: 'covid', name: 'Covid crash (2020)', years: 0.09, stocks: null, rate: null, realRate: null, gold: null, commodities: null, cpi: null },
      { id: 'crash', name: 'Crash test (Fed 2026)', years: 1, stocks: -0.58, rate: -1.8, realRate: d ? -1.00 : null, gold: 0.043, commodities: -0.366, cpi: null }
    ];
  }

  // ---------- data checks ----------
  function validateData(data) {
    var errs = [];
    if (!data || typeof data !== 'object') return ['The file is not a JSON object.'];
    if (!Array.isArray(data.months) || data.months.length < COV_MONTHS) errs.push('"months" needs at least 60 month labels.');
    if (!Array.isArray(data.holdings) || !data.holdings.length) errs.push('"holdings" is missing.');
    if (!Array.isArray(data.scenarios)) errs.push('"scenarios" is missing.');
    if (typeof data.expectedInflation !== 'number') errs.push('"expectedInflation" must be a number, such as 0.025.');
    if (errs.length) return errs;
    var market = 0;
    data.holdings.forEach(function (h, i) {
      var where = 'Holding ' + (h.ticker || '#' + (i + 1)) + ': ';
      if (!h.ticker) errs.push(where + 'needs a "ticker".');
      if (CLASSES.indexOf(h.cls) < 0) errs.push(where + '"cls" must be stock, bond, real or bitcoin.');
      if (h.kind !== 'Stock' && SLOTS.indexOf(h.slot) < 0) errs.push(where + 'funds need a "slot" such as us-stock or tips.');
      if (h.kind === 'Stock' && (!h.sector || typeof h.marketValue !== 'number')) errs.push(where + 'stocks need "sector" and "marketValue".');
      if (!Array.isArray(h.returns) || h.returns.length !== data.months.length) errs.push(where + '"returns" must have one value per month.');
      if (h.cls === 'bond' && (typeof h.duration !== 'number' || typeof h.convexity !== 'number')) errs.push(where + 'bond funds need "duration" and "convexity".');
      ['expenseRatio', 'yield', 'spread'].forEach(function (k) { if (typeof h[k] !== 'number') errs.push(where + '"' + k + '" must be a number.'); });
      if (h.role === 'market') market++;
    });
    if (market !== 1) errs.push('Exactly one holding needs "role": "market" (the US stock fund).');
    return errs;
  }

  // ---------- statistics ----------
  function recentCommonIndex(holdings, months, count) {
    var idx = [];
    for (var t = months.length - 1; t >= 0 && idx.length < count; t--) {
      var ok = true;
      for (var i = 0; i < holdings.length; i++) if (holdings[i].returns[t] === null || holdings[i].returns[t] === undefined) { ok = false; break; }
      if (ok) idx.push(t); else if (idx.length) break;
    }
    return idx.reverse();
  }
  function covariance(holdings, idx) {
    var k = holdings.length, n = idx.length, means = [], S = [];
    for (var i = 0; i < k; i++) {
      var s = 0; for (var t = 0; t < n; t++) s += holdings[i].returns[idx[t]];
      means.push(s / n);
    }
    for (i = 0; i < k; i++) {
      S.push([]);
      for (var j = 0; j < k; j++) {
        var c = 0;
        for (t = 0; t < n; t++) c += (holdings[i].returns[idx[t]] - means[i]) * (holdings[j].returns[idx[t]] - means[j]);
        S[i].push(c / (n - 1));
      }
    }
    return S;
  }
  function portfolioVol(S, w) {
    var v = 0;
    for (var i = 0; i < w.length; i++) for (var j = 0; j < w.length; j++) v += w[i] * w[j] * S[i][j];
    return Math.sqrt(Math.max(v, 0) * 12);
  }
  function ercWeights(S) {
    var k = S.length;
    if (k === 1) return [1];
    var w = [], i, j;
    for (i = 0; i < k; i++) w.push(1 / Math.sqrt(S[i][i]));
    var tot = sum(w); for (i = 0; i < k; i++) w[i] /= tot;
    for (var it = 0; it < 2000; it++) {
      var rc = [], all = 0;
      for (i = 0; i < k; i++) { var m = 0; for (j = 0; j < k; j++) m += S[i][j] * w[j]; rc.push(w[i] * m); all += w[i] * m; }
      var target = all / k, dev = 0;
      for (i = 0; i < k; i++) {
        var fct = rc[i] > 0 ? Math.sqrt(target / rc[i]) : 1.5;
        w[i] *= fct; dev = Math.max(dev, Math.abs(rc[i] - target) / target);
      }
      tot = sum(w); for (i = 0; i < k; i++) w[i] /= tot;
      if (dev < 1e-10) break;
    }
    return w;
  }
  function betaToMarket(h, market, months) {
    var idx = recentCommonIndex([h, market], months, COV_MONTHS);
    var S = covariance([h, market], idx);
    return S[0][1] / S[1][1];
  }

  // ---------- step 1 and 2: eligible holdings, picks ----------
  function eligible(data) {
    var n = data.months.length;
    return data.holdings.filter(function (h) {
      var have = 0;
      for (var t = 0; t < n; t++) if (h.returns[t] !== null && h.returns[t] !== undefined) have++;
      var minMonths = h.slot === 'bitcoin' ? 24 : COV_MONTHS; // bitcoin uses its own (shorter) history
      return have >= minMonths && h.returns[n - 1] !== null && h.returns[n - 1] !== undefined;
    });
  }
  function pickFunds(list) {
    var out = {};
    SLOTS.forEach(function (slot) {
      var c = list.filter(function (h) { return h.slot === slot && h.kind !== 'Stock'; });
      c.sort(function (a, b) {
        if (slot === 'bitcoin') return ((b.assets || 0) - (a.assets || 0)) || (a.expenseRatio - b.expenseRatio) || byTicker(a, b);
        return (a.expenseRatio - b.expenseRatio) || ((b.assets || 0) - (a.assets || 0)) || byTicker(a, b);
      });
      if (c.length) out[slot] = c[0];
    });
    return out;
  }
  function pickStocks(list, count) {
    var stocks = list.filter(function (h) { return h.kind === 'Stock'; });
    var bySector = {};
    stocks.forEach(function (s) { (bySector[s.sector] = bySector[s.sector] || []).push(s); });
    var sectors = Object.keys(bySector).map(function (name) {
      var arr = bySector[name].slice().sort(function (a, b) { return (b.marketValue - a.marketValue) || byTicker(a, b); });
      return { name: name, size: sum(arr.map(function (s) { return s.marketValue; })), list: arr };
    });
    sectors.sort(function (a, b) { return (b.size - a.size) || (a.name < b.name ? -1 : 1); });
    var picks = [];
    for (var rank = 0; picks.length < count; rank++) {
      var added = false;
      for (var i = 0; i < sectors.length && picks.length < count; i++) {
        if (sectors[i].list[rank]) { picks.push(sectors[i].list[rank]); added = true; }
      }
      if (!added) break;
    }
    return picks;
  }

  // ---------- steps 3 to 7: build one choice ----------
  function classPath(b) {
    function finish(w) {
      var others = w.stock + w.real + w.bitcoin;
      w.bond = 1 - others;
      if (w.bond > b.bond[1]) {
        var ex = w.bond - b.bond[1]; w.bond = b.bond[1];
        ['real', 'stock', 'bitcoin'].forEach(function (c) { var room = b[c][1] - w[c]; var add = Math.min(room, ex); w[c] += add; ex -= add; });
      } else if (w.bond < b.bond[0]) {
        var de = b.bond[0] - w.bond; w.bond = b.bond[0];
        ['real', 'stock', 'bitcoin'].forEach(function (c) { var room = w[c] - b[c][0]; var cut = Math.min(room, de); w[c] -= cut; de -= cut; });
      }
      return w;
    }
    function mid(c) { return (b[c][0] + b[c][1]) / 2; }
    return {
      L: finish({ stock: b.stock[0], real: mid('real'), bitcoin: b.bitcoin[0] }),
      M: finish({ stock: mid('stock'), real: mid('real'), bitcoin: mid('bitcoin') }),
      H: finish({ stock: b.stock[1], real: mid('real'), bitcoin: b.bitcoin[1] })
    };
  }
  function lerpClasses(p, t) {
    var a = t <= 1 ? p.L : p.M, b = t <= 1 ? p.M : p.H, u = t <= 1 ? t : t - 1, w = {};
    CLASSES.forEach(function (c) { w[c] = a[c] + u * (b[c] - a[c]); });
    return w;
  }
  function holdingWeights(members, within, cw) {
    return members.map(function (h, i) { return cw[h.cls] * within[i]; });
  }
  function scenarioMove(h, s, beta) {
    if (h.cls === 'stock' || h.cls === 'bitcoin') return s.stocks === null ? null : beta * s.stocks;
    if (h.cls === 'bond') {
      var dy = h.rateType === 'real' ? s.realRate : s.rate;
      if (dy === null || dy === undefined) return null;
      return (-h.duration * dy + 0.5 * h.convexity * dy * dy) / 100;
    }
    if (h.slot === 'gold') return s.gold;
    return s.commodities;
  }
  // Scenario result per $1 for weights w, before taxes (used to build choices; inputs never change picks).
  function scenarioPreTax(members, betas, w, s) {
    var total = 0;
    for (var i = 0; i < members.length; i++) {
      var h = members[i], mv = scenarioMove(h, s, betas[i]);
      if (mv === null) return null;
      total += w[i] * (1 + mv + h.yield * s.years - h.expenseRatio * s.years) * (1 - h.spread / 2);
    }
    return total - 1;
  }
  function worstPreTax(members, betas, w, scenarios) {
    var worst = Infinity, which = null;
    scenarios.forEach(function (s) {
      var r = scenarioPreTax(members, betas, w, s);
      if (r !== null && r < worst) { worst = r; which = s; }
    });
    return { value: worst, scenario: which };
  }
  function applyCaps(members, w, choice, stockClassWeight) {
    w = w.slice();
    var flags = { stockCapped: {}, sectorCapped: {} };
    for (var it = 0; it < 100; it++) {
      var excess = 0;
      members.forEach(function (h, i) {
        if (h.kind === 'Stock' && w[i] > choice.stockCap + 1e-12) { excess += w[i] - choice.stockCap; w[i] = choice.stockCap; flags.stockCapped[h.ticker] = true; }
      });
      var sectors = {};
      members.forEach(function (h, i) { if (h.kind === 'Stock') sectors[h.sector] = (sectors[h.sector] || 0) + w[i]; });
      Object.keys(sectors).forEach(function (sec) {
        var limit = choice.sectorCap * stockClassWeight;
        if (sectors[sec] > limit + 1e-12) {
          var scale = limit / sectors[sec];
          members.forEach(function (h, i) { if (h.kind === 'Stock' && h.sector === sec) w[i] *= scale; });
          excess += sectors[sec] - limit; flags.sectorCapped[sec] = true;
        }
      });
      if (excess < 1e-12) break;
      var open = [], openTotal = 0;
      members.forEach(function (h, i) {
        if (h.cls !== 'stock') return;
        if (h.kind === 'Stock' && (flags.stockCapped[h.ticker] || flags.sectorCapped[h.sector])) return;
        open.push(i); openTotal += w[i];
      });
      if (!open.length || openTotal <= 0) break;
      open.forEach(function (i) { w[i] += excess * w[i] / openTotal; });
    }
    return w;
  }
  function toDollars(w, amount) {
    var dollars = w.map(function (x) { return Math.round(x * amount); });
    var diff = amount - sum(dollars), big = 0;
    for (var i = 1; i < w.length; i++) if (w[i] > w[big]) big = i;
    dollars[big] += diff;
    return dollars;
  }

  function buildChoices(data) {
    var list = eligible(data);
    var funds = pickFunds(list);
    var market = data.holdings.filter(function (h) { return h.role === 'market'; })[0];
    var usable = data.scenarios.filter(function (s) { return s.stocks !== null && s.stocks !== undefined; });
    return CHOICES.map(function (choice) {
      var issues = [];
      var members = [];
      SLOTS.forEach(function (slot) {
        var h = funds[slot];
        if (!h) { issues.push('No eligible fund for ' + slot + '.'); return; }
        if (h.cls === 'bitcoin' && choice.bands.bitcoin[1] === 0) return;
        members.push(h);
      });
      var stocks = pickStocks(list, choice.nStocks);
      if (stocks.length < choice.nStocks) issues.push('Only ' + stocks.length + ' eligible stocks.');
      members = members.concat(stocks);
      var idx = recentCommonIndex(members, data.months, COV_MONTHS);
      var S = covariance(members, idx);
      var betas = members.map(function (h) { return h === market ? 1 : betaToMarket(h, market, data.months); });
      // step 3: equal risk within each class
      var within = new Array(members.length);
      CLASSES.forEach(function (c) {
        var ids = []; members.forEach(function (h, i) { if (h.cls === c) ids.push(i); });
        if (!ids.length) return;
        var sub = ids.map(function (i) { return ids.map(function (j) { return S[i][j]; }); });
        var ww = ercWeights(sub);
        ids.forEach(function (i, k) { within[i] = ww[k]; });
      });
      // step 4: class weights to hit the target swing
      var path = classPath(choice.bands);
      function volAt(t) { return portfolioVol(S, holdingWeights(members, within, lerpClasses(path, t))); }
      var t, lo = 0, hi = 2;
      if (volAt(0) >= choice.target) t = 0;
      else if (volAt(2) <= choice.target) t = 2;
      else { for (var k = 0; k < 60; k++) { var midT = (lo + hi) / 2; if (volAt(midT) < choice.target) lo = midT; else hi = midT; } t = (lo + hi) / 2; }
      var cw = lerpClasses(path, t);
      // step 5: worst tested case within the limit
      // steps 5 and 6 repeat together: caps can move the worst case, so it is re-tested after every cap
      var moves = 0, w, worst;
      for (;;) {
        w = applyCaps(members, holdingWeights(members, within, cw), choice, cw.stock);
        worst = worstPreTax(members, betas, w, usable);
        var canMove = cw.stock - 0.01 >= choice.bands.stock[0] - 1e-9 && cw.bond + 0.01 <= choice.bands.bond[1] + 1e-9;
        if (worst.value >= choice.worstLimit || !canMove) break;
        cw.stock -= 0.01; cw.bond += 0.01; moves++;
      }
      var swing = portfolioVol(S, w);
      if (swing < choice.swing[0] - 1e-9 || swing > choice.swing[1] + 1e-9) issues.push('Yearly swing ' + (swing * 100).toFixed(1) + '% is outside its ' + Math.round(choice.swing[0] * 100) + '% to ' + Math.round(choice.swing[1] * 100) + '% range' + (moves ? ', after ' + moves + ' point' + (moves > 1 ? 's' : '') + ' moved from stocks to bonds to meet the worst-case limit.' : '.'));
      if (worst.value < choice.worstLimit - 1e-9) issues.push('Worst tested case ' + (worst.value * 100).toFixed(1) + '% is past its ' + Math.round(choice.worstLimit * 100) + '% limit, with bonds already at their maximum.');
      return {
        choice: choice, members: members, betas: betas, weights: w, classWeights: cw,
        swing: swing, worstPreTax: worst, stockMovesToBonds: moves, issues: issues
      };
    });
  }

  // ---------- results for a user's inputs ----------
  function scenarioResults(built, dollars, data, inp, rates) {
    var amount = inp.amount;
    return data.scenarios.map(function (s) {
      if (s.stocks === null || s.stocks === undefined) return { id: s.id, name: s.name, missing: true };
      var end = 0, tax = 0, missing = false;
      built.members.forEach(function (h, i) {
        var mv = scenarioMove(h, s, built.betas[i]);
        if (mv === null) { missing = true; return; }
        var a = dollars[i] * (1 - h.spread / 2);
        var income = a * h.yield * s.years;
        end += a * (1 + mv - h.expenseRatio * s.years) + income;
        if (rates.taxed) tax += income * incomeRate(h, rates);
      });
      if (missing) return { id: s.id, name: s.name, missing: true };
      var value = end - tax;
      if (inp.view === 'real') value = value / Math.pow(1 + (s.cpi === null || s.cpi === undefined ? data.expectedInflation : s.cpi), s.years);
      return { id: s.id, name: s.name, result: value - amount, pct: (value - amount) / amount, missing: false };
    });
  }
  function incomeRate(h, r) {
    if (h.tax === 'treasury') return r.treasury;
    if (h.tax === 'commodity1099') return r.ordinary;
    if (h.tax === 'qualified') return r.qualified;
    return r.ordinary;
  }
  function saleRate(h, r, months) {
    if (months <= 12) return h.tax === 'gold' ? r.gold : r.longTerm; // 1-year view assumes 1 year and 1 day
    if (h.tax === 'gold') return r.gold;
    return r.longTerm;
  }

  // Months in which every holding of all three choices has data: the same history for every choice.
  function sharedMonths(builtAll, data) {
    var all = [];
    builtAll.forEach(function (b) { b.members.forEach(function (h) { if (all.indexOf(h) < 0) all.push(h); }); });
    var idx = [];
    for (var t = 0; t < data.months.length; t++) {
      var ok = true;
      for (var i = 0; i < all.length; i++) if (all[i].returns[t] === null || all[i].returns[t] === undefined) { ok = false; break; }
      if (ok) idx.push(t);
    }
    return idx;
  }

  function simulate(built, dollars, data, inp, rates, idx) {
    var members = built.members, k = members.length, T = Math.round(inp.years * 12), i, t;
    var M = idx.length, R = [];
    for (i = 0; i < k; i++) { var arr = new Float64Array(M); for (t = 0; t < M; t++) arr[t] = members[i].returns[idx[t]]; R.push(arr); }
    var rng = mulberry32(fnv1a(RULES_VERSION + '|simulation')); // same draws for every choice
    var before = new Float64Array(PATHS), sold = new Float64Array(PATHS);
    var fee = members.map(function (h) { return h.expenseRatio / 12; });
    var yld = members.map(function (h) { return h.yield / 12; });
    var incR = members.map(function (h) { return incomeRate(h, rates); });
    var k1 = members.map(function (h) { return h.tax === 'commodityK1'; });
    var saleR = members.map(function (h) { return saleRate(h, rates, T); });
    var inflate = inp.view === 'real' ? Math.pow(1 + data.expectedInflation, inp.years) : 1;
    var value = new Float64Array(k), basis = new Float64Array(k), yearInc = new Float64Array(k), yearStart = new Float64Array(k);
    for (var p = 0; p < PATHS; p++) {
      for (i = 0; i < k; i++) { value[i] = dollars[i] * (1 - members[i].spread / 2); basis[i] = dollars[i]; yearInc[i] = 0; yearStart[i] = value[i]; }
      var month = 0;
      while (month < T) {
        var s0 = Math.floor(rng() * (M - 2));
        for (var b = 0; b < 3 && month < T; b++, month++) {
          var row = s0 + b;
          for (i = 0; i < k; i++) {
            var v = value[i] * (1 + R[i][row] - fee[i]);
            var inc = v * yld[i];
            value[i] = v + inc; basis[i] += inc; yearInc[i] += inc;
          }
          if (rates.taxed && ((month + 1) % 12 === 0 || month + 1 === T)) {
            var tax = 0, tot = 0;
            for (i = 0; i < k; i++) {
              tax += yearInc[i] * incR[i];
              if (k1[i]) { var g = value[i] - yearStart[i] - yearInc[i]; if (g > 0) { tax += g * rates.k1; basis[i] += g; } }
              yearInc[i] = 0; tot += value[i];
            }
            if (tax > 0 && tot > 0) for (i = 0; i < k; i++) { var cut = tax * value[i] / tot; if (value[i] > 0) basis[i] *= 1 - cut / value[i]; value[i] -= cut; }
            for (i = 0; i < k; i++) yearStart[i] = value[i];
          }
        }
      }
      var end = 0, proceeds = 0, gainPos = 0, gainNet = 0, taxPos = 0;
      for (i = 0; i < k; i++) {
        end += value[i];
        var pr = value[i] * (1 - members[i].spread / 2);
        proceeds += pr;
        var gn = pr - basis[i];
        gainNet += gn;
        if (gn > 0) { gainPos += gn; taxPos += gn * saleR[i]; }
      }
      var cgTax = rates.taxed && gainNet > 0 && gainPos > 0 ? taxPos * (gainNet / gainPos) : 0;
      before[p] = end / inflate - inp.amount;
      sold[p] = (proceeds - cgTax) / inflate - inp.amount;
    }
    return { before: before, sold: sold, monthsUsed: M, firstMonth: data.months[idx[0]] };
  }

  function quantile(sorted, q) {
    var pos = (sorted.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  function run(data, built, inputs) {
    var inp = {};
    Object.keys(DEFAULT_INPUTS).forEach(function (key) { inp[key] = inputs[key] === undefined ? DEFAULT_INPUTS[key] : inputs[key]; });
    var rates = taxRates(inp);
    var idx = sharedMonths(built, data);
    var cards = built.map(function (b) {
      var dollars = toDollars(b.weights, inp.amount);
      var sc = scenarioResults(b, dollars, data, inp, rates);
      var worst = null;
      sc.forEach(function (r) { if (!r.missing && (worst === null || r.result < worst.result)) worst = r; });
      var sim = simulate(b, dollars, data, inp, rates, idx);
      var bs = Float64Array.from(sim.before).sort();
      var ss = Float64Array.from(sim.sold).sort();
      var losses = 0; for (var i = 0; i < sim.before.length; i++) if (sim.before[i] < 0) losses++;
      var holdings = b.members.map(function (h, i) {
        return { ticker: h.ticker, name: h.name, kind: h.kind, sector: h.sector || null, dollars: dollars[i], weight: b.weights[i] };
      }).sort(function (x, y) { return (y.dollars - x.dollars) || (x.ticker < y.ticker ? -1 : 1); });
      var overLimit = inp.lossLimit !== null && worst !== null && -worst.pct > inp.lossLimit + 1e-9;
      return {
        id: b.choice.id, name: b.choice.name,
        typical: Math.round(quantile(bs, 0.5)),
        rangeLow: Math.round(quantile(bs, 0.05)), rangeHigh: Math.round(quantile(bs, 0.95)),
        worst: worst ? { result: Math.round(worst.result), pct: worst.pct, name: worst.name } : null,
        chanceOfLoss: losses / sim.before.length,
        typicalIfSold: Math.round(quantile(ss, 0.5)),
        holdings: holdings, total: sum(dollars),
        scenarios: sc, swing: b.swing, worstPreTax: b.worstPreTax.value,
        historyFrom: sim.firstMonth, monthsUsed: sim.monthsUsed,
        overLimit: overLimit, issues: b.issues.slice()
      };
    });
    return { inputs: inp, cards: cards, checks: checks(built, cards, inp), rates: rates };
  }

  function checks(built, cards, inp) {
    var out = [];
    var ordered = built[0].swing < built[1].swing && built[1].swing < built[2].swing;
    out.push({ name: 'Swing rises from Lower risk to Higher growth', pass: ordered });
    var wOrdered = built[0].worstPreTax.value > built[1].worstPreTax.value && built[1].worstPreTax.value > built[2].worstPreTax.value;
    out.push({ name: 'Worst tested case deepens from Lower risk to Higher growth', pass: wOrdered });
    var limits = built.every(function (b) { return b.issues.length === 0; });
    out.push({ name: 'Every choice inside its swing range and worst-case limit', pass: limits });
    var totals = cards.every(function (c) { return c.total === inp.amount; });
    out.push({ name: 'Amounts add up exactly to the input', pass: totals });
    return out;
  }

  function canonical(inputs, data) {
    var inp = {};
    Object.keys(DEFAULT_INPUTS).sort().forEach(function (k) { inp[k] = inputs[k] === undefined ? DEFAULT_INPUTS[k] : inputs[k]; });
    var dataSig = fnv1a(JSON.stringify({ m: data.months, h: data.holdings, s: data.scenarios, e: data.expectedInflation })).toString(16);
    return JSON.stringify({ rules: RULES_VERSION, dataAsOf: data.asOf, data: dataSig, inputs: inp });
  }

  var Engine = {
    RULES_VERSION: RULES_VERSION, PATHS: PATHS, CHOICES: CHOICES, DEFAULT_INPUTS: DEFAULT_INPUTS,
    makeDemoData: makeDemoData, scenarioTable: scenarioTable, validateData: validateData,
    buildChoices: buildChoices, run: run, canonical: canonical, taxRates: taxRates,
    _internal: { fnv1a: fnv1a, mulberry32: mulberry32, ercWeights: ercWeights, covariance: covariance, portfolioVol: portfolioVol,
      toDollars: toDollars, pickStocks: pickStocks, pickFunds: pickFunds, eligible: eligible, quantile: quantile, bracketRate: bracketRate }
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = Engine;
  else root.Engine = Engine;
})(this);
