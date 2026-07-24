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

from . import db, prices, sheets

BASE = os.path.dirname(os.path.dirname(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

USER = os.environ.get("ASSET_USER", "amos")
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
    csv_path = os.path.join(BASE, "seed_positions.csv")
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


@app.get("/")
def dashboard(request: Request, user: str = Depends(require_login)):
    data = prices.enrich(db.all_positions())
    top = sorted(data["rows"], key=lambda r: r["mkt_krw"], reverse=True)[:8]
    nonstock = sheets.get_nonstock()  # None until Google 시트 연동됨
    banks = sheets.get_banks()
    net_worth = data["total_krw"] + (nonstock["total_krw"] if nonstock else 0)

    # category weights vs net worth
    stock_krw = sum(
        r["mkt_krw"] for r in data["rows"]
        if str(r.get("market") or "").upper() in ("US", "KR", "JP") and r.get("ticker")
    )
    deposit_krw = sum(b["krw"] for b in banks) if banks else 0
    car_krw = sum(r["mkt_krw"] for r in data["rows"] if (r.get("account") or "") == "자동차")
    gold_krw = sum(r["mkt_krw"] for r in data["rows"] if (r.get("account") or "") == "금현물")
    pension_krw = sum(r["mkt_krw"] for r in data["rows"] if (r.get("account") or "") == "연금저축")
    financial_krw = net_worth - car_krw  # 차 제외 금융자산
    ex_pension_krw = net_worth - pension_krw  # 연금저축 제외 자산
    etc_krw = max(net_worth - stock_krw - deposit_krw - gold_krw - car_krw, 0)  # 현금·엔화 등
    pct = lambda v: round(v / net_worth * 100, 1) if net_worth else 0
    weights = {"stock_pct": pct(stock_krw), "deposit_pct": pct(deposit_krw), "car_pct": pct(car_krw)}

    # --- chart data (rendered client-side by ApexCharts) ---
    def by(acc=None, market=None):
        return sum(
            r["mkt_krw"] for r in data["rows"]
            if (acc is None or (r.get("account") or "") == acc)
            and (market is None or str(r.get("market") or "").upper() == market)
        )

    yen_krw = next((a["krw"] for a in nonstock["assets"] if a["name"] == "엔화"), 0) if nonstock else 0
    # 종합매매 = 미국주식 + 국내주식 + 현금(USD); 나머지는 계좌 단위
    alloc = [
        {"name": "미국주식", "krw": by("종합매매", "US")},
        {"name": "예적금", "krw": deposit_krw},
        {"name": "LY 스옵", "krw": by("스톡옵션")},
        {"name": "차량", "krw": car_krw},
        {"name": "ISA", "krw": by("ISA")},
        {"name": "국내주식", "krw": by("종합매매", "KR")},
        {"name": "금", "krw": gold_krw},
        {"name": "기타", "krw": pension_krw + by("종합매매", "MANUAL") + yen_krw},
    ]
    bar_items = [{"name": a["account"], "krw": a["mkt_krw"]} for a in data["accounts"]]
    if deposit_krw:
        bar_items.append({"name": "은행 예적금", "krw": deposit_krw})
    if nonstock:
        yen = next((a["krw"] for a in nonstock["assets"] if a["name"] == "엔화"), 0)
        if yen:
            bar_items.append({"name": "엔화", "krw": yen})
    bar_items.sort(key=lambda b: b["krw"], reverse=True)

    # net worth history: snapshot today, then read series
    today = datetime.now().strftime("%Y-%m-%d")
    db.record_net_worth(today, net_worth)
    series = db.net_worth_series(120)
    stock_hist = sheets.get_stock_history() or []

    chart_data = {
        "alloc": [a for a in alloc if a["krw"] > 0],
        "bars": bar_items,
        "net": series,
        "stock": stock_hist,
    }

    priced_at_str = (
        datetime.fromtimestamp(data["priced_at"]).strftime("%m-%d %H:%M")
        if data.get("priced_at") else None
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "d": data, "top": top, "nonstock": nonstock,
         "banks": banks, "net_worth": net_worth, "financial_krw": financial_krw,
         "ex_pension_krw": ex_pension_krw, "car_krw": car_krw, "w": weights,
         "chart_data": json.dumps(chart_data, ensure_ascii=False),
         "markets": prices.market_status(), "priced_at": priced_at_str},
    )


@app.get("/positions")
def positions(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        "positions.html", {"request": request, "rows": db.all_positions()}
    )


@app.get("/positions/new")
def new_form(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse("edit.html", {"request": request, "p": {}, "pid": None})


@app.get("/positions/{pid}/edit")
def edit_form(request: Request, pid: int, user: str = Depends(require_login)):
    p = db.get_position(pid)
    if not p:
        raise HTTPException(404)
    return templates.TemplateResponse("edit.html", {"request": request, "p": p, "pid": pid})


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
        chg_str = (("▲" if chg >= 0 else "▼") + f"{abs(chg):.1f}%" + (" 🛒" if r["buy"] else "")) if chg is not None else "-"
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
        "rows": rows,
    }


@app.get("/health")
def health():
    return {"ok": True}
