"""Live market prices + FX, with a short in-memory TTL cache.

Free data only (no broker API):
  - US tickers via yfinance (batch)
  - KR 6-digit codes via FinanceDataReader
"""
from __future__ import annotations

import threading
import time
import warnings
from datetime import datetime, time as dtime

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
    _KST = ZoneInfo("Asia/Seoul")
    _JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover
    _ET = _KST = _JST = None

warnings.filterwarnings("ignore")

import pandas as pd
import yfinance as yf
import FinanceDataReader as fdr


def us_session() -> str:
    """Current US equities session by New York wall clock: 프리마켓/장중/애프터마켓/휴장."""
    if _ET is None:
        return "휴장"
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return "휴장"
    t = now.time()
    if dtime(4, 0) <= t < dtime(9, 30):
        return "프리마켓"
    if dtime(9, 30) <= t < dtime(16, 0):
        return "장중"
    if dtime(16, 0) <= t < dtime(20, 0):
        return "애프터마켓"
    return "휴장"


def kr_session() -> str:
    """KRX session by Seoul wall clock (holidays not accounted)."""
    if _KST is None:
        return "휴장"
    now = datetime.now(_KST)
    if now.weekday() >= 5:
        return "휴장"
    t = now.time()
    if dtime(8, 30) <= t < dtime(9, 0):
        return "장전"
    if dtime(9, 0) <= t < dtime(15, 30):
        return "장중"
    return "장마감"


def jp_session() -> str:
    """TSE session by Tokyo wall clock (lunch break 11:30-12:30)."""
    if _JST is None:
        return "휴장"
    now = datetime.now(_JST)
    if now.weekday() >= 5:
        return "휴장"
    t = now.time()
    if dtime(9, 0) <= t < dtime(11, 30):
        return "장중"
    if dtime(11, 30) <= t < dtime(12, 30):
        return "점심시간"
    if dtime(12, 30) <= t < dtime(15, 30):
        return "장중"
    return "장마감"


def _mkt_state(label: str) -> str:
    if label == "장중":
        return "open"
    if label in ("프리마켓", "애프터마켓", "장전"):
        return "ext"
    return "closed"  # 장마감 / 휴장 / 점심시간


def market_status() -> list[dict]:
    raw = [
        {"name": "국내", "region": "kr", "flag": "🇰🇷", "label": kr_session()},
        {"name": "미국", "region": "us", "flag": "🇺🇸", "label": us_session()},
        {"name": "일본", "region": "jp", "flag": "🇯🇵", "label": jp_session()},
    ]
    for m in raw:
        m["state"] = _mkt_state(m["label"])
    return raw


_INDICES = {
    "kr": [("코스피", "^KS11"), ("코스닥", "^KQ11")],
    "us": [("S&P 500", "^GSPC"), ("나스닥", "^IXIC"), ("다우존스", "^DJI"), ("VIX", "^VIX")],
    "jp": [("닛케이 225", "^N225")],
}

_MARKET_INFO = {
    "kr": {"name": "국내 (KRX)", "flag": "🇰🇷", "tzkey": "kst",
           "hours": [("장 시작 동시호가", "08:30", "09:00"), ("정규장", "09:00", "15:30"),
                     ("장 마감 동시호가", "15:20", "15:30"), ("시간외 단일가", "16:00", "18:00")]},
    "us": {"name": "미국 (NYSE/NASDAQ)", "flag": "🇺🇸", "tzkey": "et",
           "hours": [("프리마켓", "04:00", "09:30"), ("정규장", "09:30", "16:00"),
                     ("애프터마켓", "16:00", "20:00")]},
    "jp": {"name": "일본 (TSE)", "flag": "🇯🇵", "tzkey": "jst",
           "hours": [("전장", "09:00", "11:30"), ("점심 휴장", "11:30", "12:30"),
                     ("후장", "12:30", "15:30")]},
}


def _et_to_kst(hhmm: str) -> str | None:
    """Convert a US-Eastern HH:MM to Korea time today (DST-aware)."""
    if _ET is None or _KST is None:
        return None
    h, m = map(int, hhmm.split(":"))
    d = datetime.now(_ET).date()
    return datetime(d.year, d.month, d.day, h, m, tzinfo=_ET).astimezone(_KST).strftime("%H:%M")


def market_detail(region: str) -> dict | None:
    info = _MARKET_INFO.get(region)
    if not info:
        return None
    tz = {"kst": _KST, "et": _ET, "jst": _JST}.get(info["tzkey"])
    session = {"kr": kr_session, "us": us_session, "jp": jp_session}[region]()
    now_local = datetime.now(tz).strftime("%Y-%m-%d %H:%M") if tz else "-"
    now_kst = datetime.now(_KST).strftime("%H:%M") if _KST else "-"
    # US 장 시간엔 한국시간 병기 (서머타임 자동 반영)
    hours = []
    for label, s, e in info["hours"]:
        kst = None
        if region == "us":
            ks, ke = _et_to_kst(s), _et_to_kst(e)
            if ks and ke:
                kst = f"{ks} ~ {ke}"
        hours.append({"label": label, "time": f"{s} ~ {e}", "kst": kst})
    return {
        "region": region, "name": info["name"], "flag": info["flag"],
        "session": session, "state": _mkt_state(session),
        "hours": hours, "now_local": now_local, "now_kst": now_kst,
        "tz_label": {"kst": "한국시간", "et": "미 동부시간", "jst": "일본시간"}[info["tzkey"]],
        "show_kst": region == "us",
    }


def get_indices(region: str) -> list[dict]:
    specs = _INDICES.get(region, [])
    if not specs:
        return []

    def _p():
        out = []
        for name, tk in specs:
            try:
                h = yf.Ticker(tk).history(period="5d")["Close"].dropna()
                val = float(h.iloc[-1])
                prev = float(h.iloc[-2]) if len(h) >= 2 else val
                chg = (val - prev) / prev * 100 if prev else 0.0
                out.append({"name": name, "value": round(val, 2), "chg": round(chg, 2)})
            except Exception:
                out.append({"name": name, "value": None, "chg": None})
        return out
    return _cached_ttl(("idx", region), _p, 120)  # 2 min

_CACHE: dict = {}
_TTL = 30  # seconds (client polls ~30s; keep cache short so polls get fresh data)
_HIGH_TTL = 3600  # 52주 고가는 거의 안 변하니 1시간 캐시

BUY_DIP_PCT = -3.0  # 전일 대비 이 % 이하로 하락하면 "매수?" 표시 (단순 규칙, 자문 아님)

# epoch of the most recent *actual* market-data fetch (not a cache hit)
_LAST_FETCH: dict = {"ts": None}


def _touch_fetch():
    _LAST_FETCH["ts"] = time.time()


def priced_at() -> float | None:
    return _LAST_FETCH["ts"]


_REFRESHING: set = set()
_REFRESH_LOCK = threading.Lock()
# Serialize yf.download across threads: yfinance uses a shared global (_DFS) that
# corrupts under concurrent multi-ticker downloads ("dictionary changed size...").
_YF_LOCK = threading.Lock()


def _bg_refresh(key, producer):
    """Refresh one cache key off the request path (stale-while-revalidate)."""
    with _REFRESH_LOCK:
        if key in _REFRESHING:
            return
        _REFRESHING.add(key)

    def run():
        try:
            val = producer()
            _CACHE[key] = (time.time(), val)
        except Exception:
            pass
        finally:
            with _REFRESH_LOCK:
                _REFRESHING.discard(key)

    threading.Thread(target=run, daemon=True).start()


def _cached_ttl(key, producer, ttl):
    """Serve cached value immediately; when older than ttl, return it and refresh
    in the background. Only a truly cold key (never fetched) blocks the caller."""
    now = time.time()
    hit = _CACHE.get(key)
    if hit is not None:
        if now - hit[0] >= ttl:
            _bg_refresh(key, producer)  # stale: refresh async, serve stale now
        return hit[1]
    val = producer()  # cold: unavoidable first-time fetch
    _CACHE[key] = (now, val)
    return val


def _cached(key, producer):
    return _cached_ttl(key, producer, _TTL)


def warm(positions):
    """Prime the price/FX caches so the first page load is instant."""
    try:
        enrich(positions)
    except Exception:
        pass


def get_history(ticker: str, market: str, period: str = "6mo") -> list[dict]:
    """Daily close history for a stock detail chart. [{day, close}] ascending."""
    def _p():
        try:
            if market == "KR":
                h = fdr.DataReader(ticker)["Close"].dropna().iloc[-130:]
            else:
                h = yf.Ticker(ticker).history(period=period)["Close"].dropna()
            return [{"day": d.strftime("%Y-%m-%d"), "close": round(float(v), 4)}
                    for d, v in h.items()]
        except Exception:
            return []
    return _cached_ttl(("hist", ticker, period), _p, 1800)  # 30 min


def _fmt_cap(v: float, cur: str) -> str:
    if cur == "USD":
        if v >= 1e12:
            return f"${v/1e12:.2f}T"
        if v >= 1e9:
            return f"${v/1e9:.1f}B"
        return f"${v/1e6:.0f}M"
    if cur == "JPY":
        return f"¥{v/1e12:.2f}조"
    if v >= 1e12:
        return f"{v/1e12:.1f}조"
    return f"{v/1e8:.0f}억"


def get_fundamentals(ticker: str, market: str, currency: str) -> dict:
    """PER 등 기업 지표 via yfinance info. KR은 .KS 접미사, 일부 값만 제공."""
    yft = ticker + ".KS" if market == "KR" else ticker

    def _p():
        tk = yf.Ticker(yft)
        try:
            info = tk.get_info() or {}
        except Exception:
            info = {}
        try:
            cal = tk.calendar or {}
        except Exception:
            cal = {}

        def num(k):
            v = info.get(k)
            return v if isinstance(v, (int, float)) else None

        def date_of(v):
            if isinstance(v, (list, tuple)):
                v = v[0] if v else None
            return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else None

        cap = num("marketCap")
        return {
            "per": num("trailingPE"),
            "forward_per": num("forwardPE"),
            "eps": num("trailingEps"),
            "pbr": num("priceToBook"),
            "div_yield": num("dividendYield"),
            "market_cap": _fmt_cap(cap, currency) if cap else None,
            "earnings_date": date_of(cal.get("Earnings Date")),
            "ex_div_date": date_of(cal.get("Ex-Dividend Date")),
        }
    return _cached_ttl(("fund", yft), _p, 3600)  # 1 hour


def get_news(ticker: str, limit: int = 8) -> list[dict]:
    """Recent news via yfinance (US/JP). [{title, summary, publisher, url, time}]."""
    def _p():
        out = []
        try:
            items = yf.Ticker(ticker).news or []
        except Exception:
            return []
        for it in items[:limit]:
            c = it.get("content") or {}
            url = (c.get("canonicalUrl") or c.get("clickThroughUrl") or {}).get("url")
            title = c.get("title")
            if not title or not url:
                continue
            out.append({
                "title": title,
                "summary": (c.get("summary") or "")[:200],
                "publisher": (c.get("provider") or {}).get("displayName", ""),
                "url": url,
                "time": (c.get("pubDate") or "")[:10],
            })
        return out
    return _cached_ttl(("news", ticker), _p, 1800)  # 30 min


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


def _last_prev(series):
    """(latest, previous) close from a pandas Close series, or (None, None)."""
    try:
        s = series.dropna()
        last = float(s.iloc[-1])
        prev = float(s.iloc[-2]) if len(s) >= 2 else None
        return last, prev
    except Exception:
        return None, None


def _yf_prices(tickers: tuple) -> dict:
    """{ticker: {'price':last, 'prev':prev_close}} for US (AAPL) and JP (4689.T)."""
    if not tickers:
        return {}

    def _p():
        _touch_fetch()
        out = {}
        with _YF_LOCK:
            data = yf.download(list(tickers), period="7d", progress=False, group_by="ticker", threads=False)
        for t in tickers:
            close = None
            try:
                close = data[t]["Close"]
            except Exception:
                try:
                    close = data["Close"]
                except Exception:
                    close = None
            last, prev = _last_prev(close) if close is not None else (None, None)
            out[t] = {"price": last, "prev": prev}
        return out
    return _cached(("yf", tickers), _p)


def _yf_live(tickers: tuple) -> dict:
    """Latest 1-minute close incl. pre/post-market, {ticker: price}. US ext hours."""
    if not tickers:
        return {}

    def _p():
        _touch_fetch()
        out = {}
        with _YF_LOCK:
            data = yf.download(list(tickers), period="1d", interval="1m",
                               prepost=True, progress=False, group_by="ticker", threads=False)
        for t in tickers:
            close = None
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
    return _cached(("live", tickers), _p)


def _yf_high(tickers: tuple) -> dict:
    """52-week high per yfinance ticker (US/JP), long-cached (stale-while-revalidate)."""
    if not tickers:
        return {}

    def _p():
        out = {}
        try:
            with _YF_LOCK:
                data = yf.download(list(tickers), period="1y", progress=False, group_by="ticker", threads=False)
            for t in tickers:
                high = None
                try:
                    high = data[t]["High"]
                except Exception:
                    try:
                        high = data["High"]
                    except Exception:
                        high = None
                try:
                    out[t] = float(high.dropna().max()) if high is not None else None
                except Exception:
                    out[t] = None
        except Exception:
            out = {t: None for t in tickers}
        return out

    return _cached_ttl(("high", tickers), _p, _HIGH_TTL)


def _kr_high(code: str):
    """52-week high for a KR code, long-cached (stale-while-revalidate)."""
    def _p():
        try:
            h = fdr.DataReader(code)["High"].dropna()
            return float(h.iloc[-252:].max())
        except Exception:
            return None
    return _cached_ttl(("krhigh", code), _p, _HIGH_TTL)


def _kr_price(code: str) -> dict:
    def _p():
        _touch_fetch()
        return dict(zip(("price", "prev"), _last_prev(fdr.DataReader(code)["Close"])))
    try:
        return _cached(("kr", code), _p)
    except Exception:
        return {"price": None, "prev": None}


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

    # US pre/after-market live price: only fetch during ext-hours sessions
    session = us_session()
    us_ext = session in ("프리마켓", "애프터마켓")
    us_tickers = tuple(sorted({
        p["ticker"] for p in positions
        if str(p["market"]).upper() == "US" and p.get("ticker")
    }))
    live_map = _yf_live(us_tickers) if (us_ext and us_tickers) else {}
    high_map = _yf_high(yf_tickers)  # 52주 고가 (US/JP), 1h 캐시

    rows = []
    for p in positions:
        mkt = str(p["market"]).upper()
        cur = str(p["currency"]).upper()
        shares = p.get("shares")
        avg = p.get("avg_cost")
        fallback = p.get("manual_value_krw")
        price = None
        prev = None
        stale = False

        if mkt == "MANUAL":
            mkt_krw = float(fallback) if fallback is not None else 0.0
        else:
            info = (yfp.get(p["ticker"]) if mkt in ("US", "JP") else _kr_price(p["ticker"])) or {}
            price = info.get("price")
            prev = info.get("prev")
            if price is None:
                mkt_krw = float(fallback) if fallback is not None else 0.0
                stale = True
            else:
                mkt_krw = to_krw((shares or 0) * price, cur)

        # US pre/after-market price (shown separately from regular 현재가)
        live_price = None
        live_session = None
        if mkt == "US" and us_ext:
            lp = live_map.get(p.get("ticker"))
            if lp is not None:
                live_price = lp
                live_session = session

        # day-over-day change (native price) + naive buy-the-dip flag
        if price is not None and prev:
            chg_pct = (price - prev) / prev * 100
            buy = chg_pct <= BUY_DIP_PCT
        else:
            chg_pct = None
            buy = False

        # 52-week high (native) + how far current price sits below it
        if mkt in ("US", "JP"):
            high = high_map.get(p.get("ticker"))
        elif mkt == "KR":
            high = _kr_high(p.get("ticker"))
        else:
            high = None
        from_high_pct = ((price - high) / high * 100) if (price and high) else None

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

        # after-hours P/L (US pre/after-market): valuation with the extended-hours price
        pl_krw_ext = pl_pct_ext = None
        if live_price is not None and cost_krw is not None and shares is not None:
            mkt_ext = to_krw(shares * live_price, cur)
            pl_krw_ext = mkt_ext - cost_krw
            pl_pct_ext = (pl_krw_ext / cost_krw * 100) if cost_krw else None

        rows.append(
            {
                **p,
                "price": price,
                "stale": stale,
                "mkt_krw": round(mkt_krw),
                "pl_krw": round(pl_krw) if pl_krw is not None else None,
                "pl_pct": (round(pl_pct, 1) + 0.0) if pl_pct is not None else None,
                "pl_krw_ext": round(pl_krw_ext) if pl_krw_ext is not None else None,
                "pl_pct_ext": (round(pl_pct_ext, 1) + 0.0) if pl_pct_ext is not None else None,
                "chg_pct": (round(chg_pct, 1) + 0.0) if chg_pct is not None else None,
                "buy": buy,
                "live_price": live_price,
                "live_session": live_session,
                "high": high,
                "from_high_pct": (round(from_high_pct, 1) + 0.0) if from_high_pct is not None else None,
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
        "fx_jpy": get_fx_jpy(),
        "priced_at": priced_at(),
    }
