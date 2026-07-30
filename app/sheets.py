"""(선택) 구글 시트에서 주식 외 자산(예적금·연금 등)을 읽어 대시보드에 합산.

서비스 계정으로 시트를 읽습니다. 키 파일이나 시트 ID가 없으면 None을 돌려주고
대시보드는 주식만으로 정상 동작합니다.

시트 라벨 스캔 방식이라 **본인 시트 구조에 맞게 아래 WANTED / get_banks 를 수정**하세요.
이 예시는 col A 라벨이 "은행 총액", "엔화" 인 행을 찾아 만원(10k KRW) 단위로 읽습니다.
DB에 이미 있는 자산(주식·금·현금 등)과 중복되지 않는 항목만 넣으세요.
"""
from __future__ import annotations

import os
import threading
import time

KEY_PATH = os.environ.get("GOOGLE_SA_KEY", os.path.join(os.path.dirname(__file__), "..", "data", "gsa.json"))
SHEET_ID = os.environ.get("ASSET_SHEET_ID", "")

# label in sheet column A -> display name on dashboard
WANTED = {
    "은행 총액": "은행 예적금",
    "엔화": "엔화",
}

_CACHE: dict = {}
_TTL = 900  # 15 min


def available() -> bool:
    return bool(SHEET_ID) and os.path.exists(KEY_PATH)


def _read_raw() -> list[list[str]]:
    import gspread  # lazy import so app runs without the dep until enabled

    gc = gspread.service_account(filename=KEY_PATH)
    ws = gc.open_by_key(SHEET_ID).sheet1
    return ws.get_all_values()


_REFRESHING: set = set()
_REFRESH_LOCK = threading.Lock()


def _bg_refresh(key, producer):
    with _REFRESH_LOCK:
        if key in _REFRESHING:
            return
        _REFRESHING.add(key)

    def run():
        try:
            val = producer()
            if val is not None:
                _CACHE[key] = (time.time(), val)
        except Exception:
            pass
        finally:
            with _REFRESH_LOCK:
                _REFRESHING.discard(key)

    threading.Thread(target=run, daemon=True).start()


def _swr(key, producer, ttl=_TTL):
    """Serve cached sheet data immediately; refresh in background when stale.
    Only a truly cold key blocks (the Google Sheets read is the slow part)."""
    now = time.time()
    hit = _CACHE.get(key)
    if hit is not None:
        if now - hit[0] >= ttl:
            _bg_refresh(key, producer)
        return hit[1]
    val = producer()
    if val is not None:
        _CACHE[key] = (now, val)
    return val


def _read_raw_safe():
    try:
        return _read_raw()
    except Exception:
        return None


def _raw_cached() -> list[list[str]] | None:
    """Cached sheet read shared by get_nonstock/get_banks (avoids double API calls)."""
    return _swr("raw", _read_raw_safe)


def _num_cell(s: str):
    s = (s or "").strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def get_banks() -> list[dict] | None:
    """Itemized bank deposits from the sheet's top block (rows before '은행 총액').
    Returns [{name, krw, maturity, rate}] for non-zero balances, or None."""
    if not available():
        return None
    rows = _raw_cached()
    if rows is None:
        return None
    banks = []
    for row in rows:
        label = (row[0] if row else "").strip().strip("\x08").strip()
        if label == "은행 총액":
            break
        if not label or label in ("만기일", "금리"):
            continue
        amt = _num_cell(row[1]) if len(row) > 1 else None
        if not amt:  # skip empty / zero balances
            continue
        maturity = (row[2].strip() if len(row) > 2 else "")
        rate = (row[3].strip() if len(row) > 3 else "")
        # 카카오(달러) 등: col2 이 만기일이 아니라 숫자면 만기 표기 비움
        if maturity and not any(c in maturity for c in ".-/"):
            maturity = ""
        banks.append({"name": label, "krw": round(amt * 10000), "maturity": maturity, "rate": rate})
    return banks or None


def get_stock_history() -> list[dict] | None:
    """Parse the 'Stock' worksheet: date -> 주식 평가액(만원). Returns
    [{day:'YYYY-MM-DD', krw}] sorted ascending, or None."""
    if not available():
        return None
    return _swr("stockhist", _stock_history_producer)


def _stock_history_producer():
    import re
    try:
        import gspread
        gc = gspread.service_account(filename=KEY_PATH)
        rows = gc.open_by_key(SHEET_ID).worksheet("Stock").get_all_values()
    except Exception:
        return None

    date_re = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}$")
    pts: dict[str, int] = {}
    for r in rows:
        day = (r[0] if r else "").strip()
        if not date_re.match(day):
            continue
        val = _num_cell(r[1]) if len(r) > 1 else None
        if val is None:
            continue
        y, m, d = day.split(".")
        iso = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        pts[iso] = round(val * 10000)  # last occurrence wins
    series = [{"day": k, "krw": pts[k]} for k in sorted(pts)]
    return series or None


def get_nonstock() -> dict | None:
    """Return {'assets':[{name,krw}], 'total_krw':int} or None if not configured."""
    if not available():
        return None

    now = time.time()
    hit = _CACHE.get("nonstock")
    if hit and now - hit[0] < _TTL:
        return hit[1]

    rows = _raw_cached()
    if rows is None:
        return None

    found: dict[str, float] = {}
    for row in rows:
        if not row:
            continue
        label = (row[0] or "").strip()
        if label in WANTED and label not in found:
            # first non-empty cell after the label
            for cell in row[1:]:
                cell = (cell or "").strip().replace(",", "")
                if cell not in ("", "-"):
                    try:
                        found[label] = float(cell)
                    except ValueError:
                        pass
                    break

    assets = [
        {"name": disp, "krw": round(found.get(lbl, 0) * 10000)}
        for lbl, disp in WANTED.items()
    ]
    result = {"assets": assets, "total_krw": sum(a["krw"] for a in assets)}
    _CACHE["nonstock"] = (now, result)
    return result
