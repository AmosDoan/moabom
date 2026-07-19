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


def _us_prices(tickers: tuple) -> dict:
    if not tickers:
        return {}

    def _p():
        out = {}
        data = yf.download(list(tickers), period="5d", progress=False, group_by="ticker")
        for t in tickers:
            try:
                if len(tickers) == 1:
                    close = data["Close"]
                else:
                    close = data[t]["Close"]
                out[t] = float(close.dropna().iloc[-1])
            except Exception:
                out[t] = None
        return out
    return _cached(("us", tickers), _p)


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
    us_tickers = tuple(
        sorted({p["ticker"] for p in positions if str(p["market"]).upper() == "US" and p.get("ticker")})
    )
    us = _us_prices(us_tickers)

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
            price = us.get(p["ticker"]) if mkt == "US" else _kr_price(p["ticker"])
            if price is None:
                mkt_krw = float(fallback) if fallback is not None else 0.0
                stale = True
            else:
                val = (shares or 0) * price
                mkt_krw = val * fx if cur == "USD" else val

        if shares is not None and avg is not None:
            cost = shares * avg
            cost_krw = cost * fx if cur == "USD" else cost
            pl_krw = mkt_krw - cost_krw
            pl_pct = (pl_krw / cost_krw * 100) if cost_krw else None
        else:
            cost_krw = pl_krw = pl_pct = None

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
