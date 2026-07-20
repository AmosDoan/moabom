"""Turn dashboard numbers into SVG-ready primitives (no JS, no external libs).

Colors are referenced by CSS var (--cat1..--cat5) defined in the template, so
light/dark theming lives in one place.
"""
from __future__ import annotations


def donut_segments(items: list[dict]) -> list[dict]:
    """items: [{name, krw}] -> segments with stroke-dasharray/offset for a
    r=15.915 circle (circumference 100 => percent maps directly)."""
    total = sum(i["krw"] for i in items) or 1
    segs = []
    running = 0.0
    for idx, it in enumerate(items, start=1):
        pct = it["krw"] / total * 100
        segs.append({
            "name": it["name"],
            "krw": it["krw"],
            "pct": round(pct, 1),
            "dash": round(pct, 3),
            "gap": round(100 - pct, 3),
            "offset": round(25 - running, 3),  # start at 12 o'clock
            "var": f"--cat{idx}",
        })
        running += pct
    return segs


def line_geom(series: list[dict], w: int = 320, h: int = 90, pad: int = 6) -> dict | None:
    """series: [{day, krw}] oldest->newest -> polyline points + area path."""
    if not series or len(series) < 2:
        return None
    vals = [s["krw"] for s in series]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    n = len(series)
    pts = []
    for i, s in enumerate(series):
        x = pad + i / (n - 1) * (w - 2 * pad)
        y = h - pad - (s["krw"] - lo) / span * (h - 2 * pad)
        pts.append((round(x, 1), round(y, 1)))
    poly = " ".join(f"{x},{y}" for x, y in pts)
    area = f"M{pts[0][0]},{h - pad} " + " ".join(f"L{x},{y}" for x, y in pts) + f" L{pts[-1][0]},{h - pad} Z"
    return {
        "points": poly, "area": area, "w": w, "h": h,
        "first": series[0], "last": series[-1], "lo": lo, "hi": hi,
    }
