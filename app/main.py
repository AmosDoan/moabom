"""Asset dashboard - FastAPI. Holdings in SQLite, live prices via free data.

Auth: session cookie via a web login form. Credentials from env ASSET_USER/ASSET_PASSWORD.
Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import secrets

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from datetime import datetime

from . import db, prices

BASE = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
VERSION = "1.0"
templates.env.globals["VERSION"] = VERSION

USER = os.environ.get("ASSET_USER", "admin")
PASSWORD = os.environ.get("ASSET_PASSWORD", "changeme")


def _session_secret() -> str:
    """Persist a signing secret so sessions survive restarts (data/ is mounted)."""
    env = os.environ.get("ASSET_SECRET")
    if env:
        return env
    path = os.path.join(BASE, "data", "secret.key")
    if os.path.exists(path):
        return open(path).read().strip()
    key = secrets.token_hex(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return key


app = FastAPI(title="Asset Dashboard")
app.add_middleware(SessionMiddleware, secret_key=_session_secret(), max_age=60 * 60 * 24 * 14)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


class NotAuthed(Exception):
    pass


@app.exception_handler(NotAuthed)
async def _to_login(request: Request, exc: NotAuthed):
    return RedirectResponse("/login", status_code=303)


def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise NotAuthed()
    return user


# ---- password: stored as pbkdf2 hash in DB, seeded from env on first run ----
_PBKDF2_ROUNDS = 200_000


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS).hex()
    return f"{salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS).hex()
    return hmac.compare_digest(calc, h)


def stored_hash() -> str:
    """Current password hash; seed from env ASSET_PASSWORD if not set yet."""
    h = db.get_setting("password_hash")
    if not h:
        h = hash_password(PASSWORD)
        db.set_setting("password_hash", h)
    return h


def _num(v):
    """Parse form field to float or None."""
    v = (v or "").strip().replace(",", "")
    return float(v) if v else None


def _money(val, cur) -> str:
    """Per-share price/cost in native currency (mirrors the template macro)."""
    if val is None:
        return "-"
    if cur == "USD":
        return f"${val:.2f}"
    if cur == "JPY":
        return f"¥{val:.0f}"
    return f"{val:,.0f}원"


# fields tracked in the change log, with Korean labels
_LOG_FIELDS = [
    ("account", "계좌"), ("name", "종목"), ("ticker", "티커"), ("market", "구분"),
    ("currency", "통화"), ("shares", "수량"), ("avg_cost", "평단"),
    ("manual_value_krw", "수동평가액"), ("cost_krw", "취득원가"),
]


def _fmt(v):
    return "-" if v in (None, "") else (f"{v:g}" if isinstance(v, float) else str(v))


def _summary(data: dict) -> str:
    """Compact 'label 값' list for a new position."""
    parts = [f"{lbl} {_fmt(data.get(k))}" for k, lbl in _LOG_FIELDS
             if k in ("shares", "avg_cost", "ticker", "manual_value_krw") and data.get(k) not in (None, "")]
    return ", ".join(parts) or "-"


def _diff(old: dict, new: dict) -> str:
    """'label old→new' for changed fields only."""
    changed = []
    for k, lbl in _LOG_FIELDS:
        ov, nv = old.get(k), new.get(k)
        if _fmt(ov) != _fmt(nv):
            changed.append(f"{lbl} {_fmt(ov)}→{_fmt(nv)}")
    return ", ".join(changed) or "변경 없음"


def seed_if_empty():
    db.init()
    if db.count() > 0:
        return
    # Prefer the user's own seed; fall back to the bundled example on first run.
    csv_path = os.path.join(BASE, "seed_positions.csv")
    if not os.path.exists(csv_path):
        csv_path = os.path.join(BASE, "seed_positions.example.csv")
    if not os.path.exists(csv_path):
        return
    with open(csv_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            db.upsert_position(
                {
                    "account": r["account"],
                    "name": r["name"],
                    "ticker": None if r["ticker"] in ("", "-") else r["ticker"],
                    "market": r["market"],
                    "currency": r["currency"],
                    "shares": _num(r.get("shares")),
                    "avg_cost": _num(r.get("avg_cost")),
                    "manual_value_krw": _num(r.get("manual_value_krw")),
                }
            )


seed_if_empty()

# Warm the price cache at startup (off the request path) so the first load is instant.
import threading as _threading

_threading.Thread(target=lambda: prices.warm(db.all_positions()), daemon=True).start()


def _account_view(accounts):
    """Each account gets sub-groups. 종합매매 splits into 국내주식/해외주식 within ONE card."""
    def grp(label, rows_):
        pls = [r["pl_krw"] for r in rows_ if r["pl_krw"] is not None]
        return {"label": label, "rows": rows_,
                "mkt_krw": sum(r["mkt_krw"] for r in rows_),
                "pl_krw": sum(pls) if pls else None}

    out = []
    for a in accounts:
        if a["account"] == "종합매매":
            kr = [r for r in a["rows"] if str(r.get("market") or "").upper() == "KR"]
            ov = [r for r in a["rows"] if str(r.get("market") or "").upper() != "KR"]
            groups = [g for g in (grp("국내주식", kr) if kr else None,
                                  grp("해외주식", ov) if ov else None) if g]
        else:
            groups = [grp(None, a["rows"])]
        out.append({**a, "groups": groups})
    return out


@app.get("/")
def dashboard(request: Request, user: str = Depends(require_login)):
    data = prices.enrich(db.all_positions())
    account_groups = _account_view(data["accounts"])
    top = sorted(data["rows"], key=lambda r: r["mkt_krw"], reverse=True)[:8]
    net_worth = data["total_krw"]  # 모든 자산이 DB에 있음 (은행·현금·금·차 포함)

    # US 프리/애프터마켓: 시간외 가격 기준 평가액(정규가와의 차이만큼 보정)
    us_session = prices.us_session()
    ext_delta = round(sum(
        r["shares"] * (r["live_price"] - r["price"]) * data["fx"]
        for r in data["rows"]
        if r.get("live_price") and r.get("price") and r.get("shares")
        and str(r.get("market") or "").upper() == "US"
    )) if us_session in ("프리마켓", "애프터마켓") else 0
    ext_net_worth = net_worth + ext_delta if ext_delta else None

    # aggregate every holding by its account's category (fully user-configurable)
    acct_cat = db.account_categories()
    cat_totals = {c: 0.0 for c in db.CATEGORIES}
    for r in data["rows"]:
        cat = acct_cat.get(r.get("account") or "", db.DEFAULT_CATEGORY)
        cat_totals[cat] = cat_totals.get(cat, 0.0) + r["mkt_krw"]

    real_krw = cat_totals.get("실물자산", 0.0)      # 금·부동산·차 등
    financial_krw = net_worth - real_krw            # 금융자산 = 실물 제외
    pct = lambda v: round(v / net_worth * 100, 1) if net_worth else 0
    cat_weights = [
        {"name": c, "krw": cat_totals[c], "pct": pct(cat_totals[c])}
        for c in db.CATEGORIES if cat_totals[c] > 0
    ]

    # --- chart data (rendered client-side by ApexCharts) ---
    # 자산 구성 도넛 = category breakdown
    alloc = [{"name": c["name"], "krw": c["krw"]} for c in cat_weights]
    bar_items = [{"name": a["account"], "krw": a["mkt_krw"]} for a in data["accounts"]]
    bar_items.sort(key=lambda b: b["krw"], reverse=True)

    # net worth history: snapshot today, then read series
    today = datetime.now().strftime("%Y-%m-%d")
    db.record_net_worth(today, net_worth)
    series = db.net_worth_series(120)

    # heatmap: each stock tile sized by value, colored by return %.
    # Fold holdings under 1% of the total into a single 기타 tile so small tiles stay readable.
    _hm = sorted(
        [{"id": r["id"], "name": r["name"], "krw": r["mkt_krw"], "pct": r["pl_pct"]}
         for r in data["rows"]
         if str(r.get("market") or "").upper() in ("US", "KR", "JP")
         and r.get("ticker") and r["mkt_krw"] > 0],
        key=lambda h: h["krw"], reverse=True,
    )
    _hm_total = sum(h["krw"] for h in _hm) or 1
    _thresh = _hm_total * 0.01
    heatmap = [h for h in _hm if h["krw"] >= _thresh]
    _small = [h for h in _hm if h["krw"] < _thresh]
    if _small:
        heatmap.append({
            "id": None,
            "name": f"기타 {len(_small)}종목",
            "krw": sum(h["krw"] for h in _small),
            "pct": None,
        })

    chart_data = {
        "alloc": [a for a in alloc if a["krw"] > 0],
        "bars": bar_items,
        "net": series,
        "heatmap": heatmap,
    }

    priced_at_str = (
        datetime.fromtimestamp(data["priced_at"]).strftime("%m-%d %H:%M")
        if data.get("priced_at") else None
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "d": data, "account_groups": account_groups, "top": top,
         "net_worth": net_worth,
         "financial_krw": financial_krw, "real_krw": real_krw, "cat_weights": cat_weights,
         "us_session": us_session, "ext_delta": ext_delta, "ext_net_worth": ext_net_worth,
         "chart_data": json.dumps(chart_data, ensure_ascii=False),
         "markets": prices.market_status(), "priced_at": priced_at_str},
    )


@app.get("/positions")
def positions(request: Request, user: str = Depends(require_login)):
    rows = db.all_positions()
    accounts = db.list_accounts()
    # group positions under their account (accounts with no holdings still show)
    by_acc = {a["name"]: [] for a in accounts}
    for r in rows:
        by_acc.setdefault(r.get("account") or "", []).append(r)
    groups = [
        {"name": a["name"], "category": a["category"], "rows": by_acc.get(a["name"], [])}
        for a in accounts
    ]
    flash = request.session.pop("flash", None)
    return templates.TemplateResponse(
        "positions.html",
        {"request": request, "groups": groups, "accounts": accounts,
         "categories": db.CATEGORIES, "flash": flash},
    )


@app.post("/accounts")
def account_add(request: Request, user: str = Depends(require_login),
                name: str = Form(...), category: str = Form(db.DEFAULT_CATEGORY)):
    db.add_account(name, category)
    return RedirectResponse("/positions", status_code=303)


@app.post("/accounts/category")
def account_category(request: Request, user: str = Depends(require_login),
                     name: str = Form(...), category: str = Form(...)):
    db.set_account_category(name, category)
    return RedirectResponse("/positions", status_code=303)


@app.post("/accounts/rename")
def account_rename(request: Request, user: str = Depends(require_login),
                   old: str = Form(...), new: str = Form(...)):
    err = db.rename_account(old, new)
    if err:
        request.session["flash"] = err
    return RedirectResponse("/positions", status_code=303)


@app.post("/accounts/delete")
def account_delete(request: Request, user: str = Depends(require_login), name: str = Form(...)):
    err = db.delete_account(name)
    if err:
        request.session["flash"] = err
    return RedirectResponse("/positions", status_code=303)


@app.get("/positions/new")
def new_form(request: Request, user: str = Depends(require_login), account: str = ""):
    return templates.TemplateResponse(
        "edit.html",
        {"request": request, "p": {"account": account}, "pid": None, "accounts": db.list_accounts()},
    )


@app.get("/positions/{pid}/edit")
def edit_form(request: Request, pid: int, user: str = Depends(require_login)):
    p = db.get_position(pid)
    if not p:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "edit.html", {"request": request, "p": p, "pid": pid, "accounts": db.list_accounts()}
    )


@app.post("/positions")
def create(
    user: str = Depends(require_login),
    account: str = Form(...),
    name: str = Form(...),
    ticker: str = Form(""),
    market: str = Form(...),
    currency: str = Form(...),
    shares: str = Form(""),
    avg_cost: str = Form(""),
    manual_value_krw: str = Form(""),
    cost_krw: str = Form(""),
):
    data = {
        "account": account, "name": name,
        "ticker": ticker.strip() or None, "market": market, "currency": currency,
        "shares": _num(shares), "avg_cost": _num(avg_cost),
        "manual_value_krw": _num(manual_value_krw), "cost_krw": _num(cost_krw),
    }
    db.upsert_position(data)
    db.add_log("추가", account, name, _summary(data))
    return RedirectResponse("/positions", status_code=303)


@app.post("/positions/{pid}")
def update(
    pid: int,
    user: str = Depends(require_login),
    account: str = Form(...),
    name: str = Form(...),
    ticker: str = Form(""),
    market: str = Form(...),
    currency: str = Form(...),
    shares: str = Form(""),
    avg_cost: str = Form(""),
    manual_value_krw: str = Form(""),
    cost_krw: str = Form(""),
):
    new = {
        "account": account, "name": name,
        "ticker": ticker.strip() or None, "market": market, "currency": currency,
        "shares": _num(shares), "avg_cost": _num(avg_cost),
        "manual_value_krw": _num(manual_value_krw), "cost_krw": _num(cost_krw),
    }
    old = db.get_position(pid) or {}
    db.upsert_position(new, pid=pid)
    db.add_log("수정", account, name, _diff(old, new))
    return RedirectResponse("/positions", status_code=303)


@app.post("/positions/{pid}/delete")
def remove(pid: int, user: str = Depends(require_login)):
    p = db.get_position(pid) or {}
    db.delete_position(pid)
    db.add_log("삭제", p.get("account", ""), p.get("name", ""), _summary(p))
    return RedirectResponse("/positions", status_code=303)


@app.get("/positions/{pid}/trade")
def trade_form(request: Request, pid: int, user: str = Depends(require_login)):
    p = db.get_position(pid)
    if not p:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "trade.html", {"request": request, "p": p, "error": None}
    )


@app.post("/positions/{pid}/trade")
def trade(
    request: Request,
    pid: int,
    user: str = Depends(require_login),
    side: str = Form(...),      # 매수 | 매도
    qty: str = Form(...),
    price: str = Form(...),
):
    p = db.get_position(pid)
    if not p:
        raise HTTPException(404)

    def fail(msg):
        return templates.TemplateResponse(
            "trade.html", {"request": request, "p": p, "error": msg}, status_code=400
        )

    q = _num(qty)
    px = _num(price)
    if not q or q <= 0 or px is None or px < 0:
        return fail("수량과 가격을 올바르게 입력해 주세요.")

    old_sh = p.get("shares") or 0
    old_avg = p.get("avg_cost") or 0
    old_ck = p.get("cost_krw")
    cur = str(p.get("currency") or "KRW").upper()

    if side == "매수":
        new_sh = old_sh + q
        # weighted-average cost in the position's native currency
        new_avg = (old_sh * old_avg + q * px) / new_sh if new_sh else px
        # fixed KRW cost basis (if used): add this trade's KRW cost at current FX
        fx = prices.get_fx() if cur == "USD" else (prices.get_fx_jpy() if cur == "JPY" else 1)
        new_ck = (old_ck + q * px * fx) if old_ck is not None else None
        detail = (f"{_money(px, cur)} × {q:g}주 매수 → "
                  f"수량 {old_sh:g}→{new_sh:g}, 평단 {_money(old_avg, cur)}→{_money(new_avg, cur)}")
    elif side == "매도":
        if q > old_sh:
            return fail(f"보유 수량({old_sh:g})보다 많이 팔 수 없습니다.")
        new_sh = old_sh - q
        new_avg = old_avg  # 평단은 매도로 바뀌지 않음
        # reduce fixed KRW basis pro-rata
        new_ck = (old_ck * new_sh / old_sh) if (old_ck is not None and old_sh) else old_ck
        detail = (f"{_money(px, cur)} × {q:g}주 매도 → "
                  f"수량 {old_sh:g}→{new_sh:g} (평단 {_money(new_avg, cur)} 유지)")
    else:
        return fail("매수 또는 매도를 선택해 주세요.")

    db.upsert_position(
        {**p, "shares": new_sh, "avg_cost": new_avg, "cost_krw": new_ck}, pid=pid
    )
    db.add_log(side, p.get("account", ""), p.get("name", ""), detail)
    return RedirectResponse("/positions", status_code=303)


@app.get("/market/{region}")
def market_page(request: Request, region: str, user: str = Depends(require_login)):
    detail = prices.market_detail(region)
    if not detail:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "market.html",
        {"request": request, "m": detail, "indices": prices.get_indices(region)},
    )


@app.get("/stock/{pid}")
def stock_detail(request: Request, pid: int, user: str = Depends(require_login)):
    p = db.get_position(pid)
    if not p or not p.get("ticker"):
        raise HTTPException(404)
    market = str(p["market"]).upper()
    row = next((r for r in prices.enrich(db.all_positions())["rows"] if r["id"] == pid), None)
    hist = prices.get_history(p["ticker"], market)
    news = prices.get_news(p["ticker"]) if market in ("US", "JP") else []
    naver = f"https://finance.naver.com/item/main.naver?code={p['ticker']}" if market == "KR" else None
    fund = prices.get_fundamentals(p["ticker"], market, str(p["currency"]).upper())
    return templates.TemplateResponse(
        "stock.html",
        {"request": request, "p": p, "row": row, "market": market, "fund": fund,
         "hist_json": json.dumps(hist, ensure_ascii=False), "news": news, "naver": naver},
    )


@app.get("/log")
def change_log(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        "log.html", {"request": request, "logs": db.recent_logs(200)}
    )


@app.get("/login")
def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ok = secrets.compare_digest(username, USER) and verify_password(password, stored_hash())
    if not ok:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "아이디 또는 비밀번호가 올바르지 않습니다."},
            status_code=401,
        )
    request.session["user"] = username
    return RedirectResponse("/", status_code=303)


@app.get("/password")
def password_form(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        "password.html", {"request": request, "error": None, "ok": False}
    )


@app.post("/password")
def password_change(
    request: Request,
    user: str = Depends(require_login),
    current: str = Form(...),
    new1: str = Form(...),
    new2: str = Form(...),
):
    def fail(msg):
        return templates.TemplateResponse(
            "password.html", {"request": request, "error": msg, "ok": False}, status_code=400
        )

    if not verify_password(current, stored_hash()):
        return fail("현재 비밀번호가 올바르지 않습니다.")
    if new1 != new2:
        return fail("새 비밀번호가 서로 일치하지 않습니다.")
    if len(new1) < 8:
        return fail("새 비밀번호는 8자 이상이어야 합니다.")
    if new1 == current:
        return fail("현재 비밀번호와 다른 비밀번호를 입력해 주세요.")
    db.set_setting("password_hash", hash_password(new1))
    return templates.TemplateResponse("password.html", {"request": request, "error": None, "ok": True})


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/api/live")
def api_live(user: str = Depends(require_login)):
    """Lightweight JSON for client polling: live prices/values per position."""
    data = prices.enrich(db.all_positions())
    rows = {}
    for r in data["rows"]:
        pl = r["pl_krw"]
        chg = r["chg_pct"]
        chg_str = (("(" + ("+" if chg >= 0 else "") + f"{chg:.1f}%)") + ("🛒" if r["buy"] else "")) if chg is not None else ""
        ext = ""
        if r.get("live_price"):
            ext = r["live_session"] + " " + _money(r["live_price"], r["currency"])
        rows[str(r["id"])] = {
            "price": _money(r["price"], r["currency"]),
            "ext": ext,
            "chg": chg_str,
            "chg_up": (chg or 0) >= 0,
            "mkt": f'{r["mkt_krw"]:,}',
            "pl": (("+" if pl >= 0 else "") + f"{pl:,}") if pl is not None else "-",
            "pl_pct": (("+" if r["pl_pct"] >= 0 else "") + f'{r["pl_pct"]}%') if r["pl_pct"] is not None else "-",
            "up": (pl or 0) >= 0,
            "pl_ext": (("+" if r["pl_krw_ext"] >= 0 else "") + f'{r["pl_krw_ext"]:,}') if r.get("pl_krw_ext") is not None else "",
            "pl_ext_up": (r.get("pl_krw_ext") or 0) >= 0,
            "pl_pct_ext": (("+" if r["pl_pct_ext"] >= 0 else "") + f'{r["pl_pct_ext"]}%') if r.get("pl_pct_ext") is not None else "",
        }
    priced = (
        datetime.fromtimestamp(data["priced_at"]).strftime("%m-%d %H:%M")
        if data.get("priced_at") else None
    )
    tpl = data["total_pl_krw"]
    return {
        "priced_at": priced,
        "markets": prices.market_status(),
        "total_krw": f'{data["total_krw"]:,}',
        "total_pl": ("+" if tpl >= 0 else "") + f"{tpl:,}",
        "total_up": tpl >= 0,
        "fx": f'{data["fx"]:,.1f}',
        "fx_jpy100": f'{data["fx_jpy"] * 100:,.0f}',
        "rows": rows,
    }


@app.get("/health")
def health():
    return {"ok": True}
