"""Read non-stock assets from the personal Google 자산 sheet via a service account.

Graceful: if the key file or sheet id is missing, returns None so the dashboard
keeps working with stocks only.

Values in the sheet are in 만원 (10k KRW) units -> multiplied to KRW here.
Only NON-stock, NON-duplicated items are pulled:
  - 은행 총액   (예적금/파킹; sheet 달러칸은 0)
  - 엔화
금/달러/스톡옵션은 대시보드 DB에 이미 있으므로 제외
  (금현물·USD현금 = 나무 계좌, LY 스톡옵션 = 4689.T 실시간 추적 종목)
"""
from __future__ import annotations

import os
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


def _raw_cached() -> list[list[str]] | None:
    """Cached sheet read shared by get_nonstock/get_banks (avoids double API calls)."""
    now = time.time()
    hit = _CACHE.get("raw")
    if hit and now - hit[0] < _TTL:
        return hit[1]
    try:
        rows = _read_raw()
    except Exception:
        return None
    _CACHE["raw"] = (now, rows)
    return rows


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
