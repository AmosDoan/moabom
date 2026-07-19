"""Live market prices + FX, with a short in-memory TTL cache.

Free data only (no broker API):
  - US tickers via yfinance (batch)
  - KR 6-digit codes via FinanceDataReader
"""
from __future__ import annotations

import time
import warnings

warnings.filterwarnings("ignore")

import pandas as pd
import yfinance as yf
import FinanceDataReader as fdr

_CACHE: dict = {}
_TTL = 600  # seconds


def _cached(key, producer):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    val = producer()
    _CACHE[key] = (now, val)
    return val


def get_fx() -> float:
    def _p():
        fx = fdr.DataReader("USD/KRW")
        return float(fx["Close"].dropna().iloc[-1])
    return _cached("fx", _p)


def get_fx_jpy() -> float:
    def _p():
        fx = fdr.DataReader("JPY/KRW")
        return float(fx["Close"].dropna().iloc[-1])
    return _cached("fx_jpy", _p)


def _yf_prices(tickers: tuple) -> dict:
    """Latest close per yfinance ticker (works for US e.g. AAPL and JP e.g. 4689.T)."""
    if not tickers:
        return {}

    def _p():
        out = {}
        data = yf.download(list(tickers), period="5d", progress=False, group_by="ticker")
        for t in tickers:
            close = None
            # grouped columns (data[ticker][Close]) or flat (data[Close])
            try:
                close = data[t]["Close"]
            except Exception:
                try:
                    close = data["Close"]
                except Exception:
                    close = None
            try:
                out[t] = float(close.dropna().iloc[-1]) if close is not None else None
            except Exception:
                out[t] = None
        return out
    return _cached(("yf", tickers), _p)


def _kr_price(code: str):
    def _p():
        h = fdr.DataReader(code)
        return float(h["Close"].dropna().iloc[-1])
    try:
        return _cached(("kr", code), _p)
    except Exception:
        return None


def enrich(positions: list[dict]) -> dict:
    """Return {rows, accounts, total_krw, total_pl_krw, fx}. Values in KRW."""
    fx = get_fx()

    def to_krw(v, cur):
        if cur == "USD":
            return v * fx
        if cur == "JPY":
            return v * get_fx_jpy()
        return v  # KRW

    # US and JP both priced through yfinance (AAPL, 4689.T, ...)
    yf_tickers = tuple(sorted({
        p["ticker"] for p in positions
        if str(p["market"]).upper() in ("US", "JP") and p.get("ticker")
    }))
    yfp = _yf_prices(yf_tickers)

    rows = []
    for p in positions:
        mkt = str(p["market"]).upper()
        cur = str(p["currency"]).upper()
        shares = p.get("shares")
        avg = p.get("avg_cost")
        fallback = p.get("manual_value_krw")
        price = None
        stale = False

        if mkt == "MANUAL":
            mkt_krw = float(fallback) if fallback is not None else 0.0
        else:
            price = yfp.get(p["ticker"]) if mkt in ("US", "JP") else _kr_price(p["ticker"])
            if price is None:
                mkt_krw = float(fallback) if fallback is not None else 0.0
                stale = True
            else:
                mkt_krw = to_krw((shares or 0) * price, cur)

        # cost basis: fixed KRW (accurate for foreign holdings incl. FX) takes priority,
        # else derive from shares × avg_cost at current FX
        fixed_krw = p.get("cost_krw")
        if fixed_krw is not None:
            cost_krw = float(fixed_krw)
        elif shares is not None and avg is not None:
            cost_krw = to_krw(shares * avg, cur)
        else:
            cost_krw = None

        if cost_krw is not None:
            pl_krw = mkt_krw - cost_krw
            pl_pct = (pl_krw / cost_krw * 100) if cost_krw else None
        else:
            pl_krw = pl_pct = None

        rows.append(
            {
                **p,
                "price": price,
                "stale": stale,
                "mkt_krw": round(mkt_krw),
                "pl_krw": round(pl_krw) if pl_krw is not None else None,
                "pl_pct": round(pl_pct, 1) if pl_pct is not None else None,
            }
        )

    total = sum(r["mkt_krw"] for r in rows) or 1
    total_pl = sum(r["pl_krw"] for r in rows if r["pl_krw"] is not None)
    for r in rows:
        r["weight"] = round(r["mkt_krw"] / total * 100, 1)

    # group by account preserving order
    accounts = []
    seen = {}
    for r in rows:
        acc = r["account"]
        if acc not in seen:
            seen[acc] = {"account": acc, "rows": [], "mkt_krw": 0, "pl_krw": 0}
            accounts.append(seen[acc])
        seen[acc]["rows"].append(r)
        seen[acc]["mkt_krw"] += r["mkt_krw"]
        if r["pl_krw"] is not None:
            seen[acc]["pl_krw"] += r["pl_krw"]
    for a in accounts:
        a["weight"] = round(a["mkt_krw"] / total * 100, 1)

    return {
        "rows": rows,
        "accounts": accounts,
        "total_krw": round(total),
        "total_pl_krw": round(total_pl),
        "fx": fx,
    }
