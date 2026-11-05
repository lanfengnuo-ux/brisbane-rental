#!/usr/bin/env python3
"""
布里斯班租房看板 — 抓取 The Onsite Manager 公开 API 并生成静态页面。

数据源: https://www.theonsitemanager.com.au/rental-property?location=South%20Brisbane
站点是 Angular SPA，但 robots.txt 明确放行以下公开读 API:
  /api/v1/listings.php  (列表，支持 location / page / limit)
  /api/v1/listing.php   (详情，含 dateAvailable / inspections / 图片 / 电话)
所以全程纯 HTTP JSON，不需要浏览器。

用法:
  python3 scraper.py            # 抓取 + 生成 index.html
  python3 scraper.py --cache    # 用上次抓取的 data/listings.json 重新渲染（调试 UI 用）
  python3 scraper.py --dry      # 只打印统计，不写文件

产物: index.html、data/listings.json、previous_data.json
页面模板: template.html（占位符 __DATA_JSON__ / __META_JSON__）
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT_HTML = ROOT / "index.html"
TEMPLATE = ROOT / "template.html"
OUT_JSON = ROOT / "data" / "listings.json"
HISTORY = ROOT / "previous_data.json"

BASE = "https://www.theonsitemanager.com.au"
API_LIST = BASE + "/api/v1/listings.php"
API_DETAIL = BASE + "/api/v1/listing.php"
LIST_LOCATION = "South Brisbane"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE + "/",
}
BRISBANE_TZ = timezone(timedelta(hours=10))
PAGE_SIZE = 100
DETAIL_WORKERS = 10
REQUEST_RETRIES = 3
NEW_WITHIN_DAYS = 7

# 区域分组：沿用原页面的三个「圈」，其余郊区归入「其他周边」
ZONES: dict[str, list[str]] = {
    "South Brisbane 圈": [
        "South Brisbane", "West End", "Highgate Hill", "Dutton Park",
        "Woolloongabba", "Kangaroo Point", "East Brisbane", "Tarragindi",
        "Moorooka", "Salisbury", "Annerley", "Greenslopes", "Coorparoo",
        "Bulimba", "Morningside", "Cannon Hill", "Carina", "Carina Heights",
        "Carindale", "Mount Gravatt East", "Upper Mount Gravatt", "Macgregor",
        "Sunnybank", "Sunnybank Hills", "Runcorn", "Eight Mile Plains",
        "Calamvale", "Algester", "Pallara", "Robertson", "Tarragindi",
    ],
    "Toowong 圈": [
        "Toowong", "Taringa", "Auchenflower", "Indooroopilly", "St Lucia",
        "Milton", "Paddington", "Bardon", "Ashgrove", "Red Hill",
        "Petrie Terrace", "Sherwood", "Graceville", "Oxley",
        "Seventeen Mile Rocks", "Richlands",
    ],
    "Brisbane City 圈": [
        "Brisbane City", "Spring Hill", "Fortitude Valley", "Newstead",
        "Teneriffe", "Bowen Hills", "New Farm", "Herston", "Kelvin Grove",
        "Lutwyche", "Windsor", "Albion", "Alderley", "Stafford", "Enoggera",
        "Nundah", "Chermside", "Hamilton", "Hendra", "Woolloowin",
    ],
}
OTHER_ZONE = "其他周边"
SUBURB_TO_ZONE = {s.strip(): z for z, subs in ZONES.items() for s in subs}
ZONE_ORDER = list(ZONES) + [OTHER_ZONE]
ZONE_SLUG = {
    "South Brisbane 圈": "south-brisbane",
    "Toowong 圈": "toowong",
    "Brisbane City 圈": "brisbane-city",
    OTHER_ZONE: "other",
}

CATEGORY_SLUG = {
    "apartment": "apartment-for-rent",
    "house": "house-for-rent",
    "townhouse": "townhouse-for-rent",
    "unit": "unit-for-rent",
    "villa": "villa-for-rent",
    "studio": "apartment-for-rent",
    "duplex": "house-for-rent",
    "land": "house-for-rent",
}
MONTHS_EN = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# ---------------------------------------------------------------- HTTP
def _get(url: str, timeout: int = 40) -> dict:
    last = None
    for attempt in range(REQUEST_RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                OSError) as e:
            last = e
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url} -> {last}")


def fetch_all_rentals(max_pages: int = 10) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while page <= max_pages:
        url = (f"{API_LIST}?class=rental&location="
               f"{urllib.parse.quote(LIST_LOCATION)}&page={page}&limit={PAGE_SIZE}")
        data = _get(url)
        batch = data.get("data") or []
        rows.extend(batch)
        pg = data.get("pagination") or {}
        print(f"  · 列表第 {page}/{pg.get('pages', '?')} 页: {len(batch)} 条 "
              f"(total={pg.get('total')})", flush=True)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.25)
    # 去重
    seen, uniq = set(), []
    for r in rows:
        pid = str(r.get("pageId") or "")
        if pid and pid not in seen:
            seen.add(pid)
            uniq.append(r)
    return uniq


def fetch_detail(page_id: str) -> dict:
    try:
        d = _get(f"{API_DETAIL}?id={urllib.parse.quote(str(page_id))}", timeout=25)
        return (d.get("data") or {}) if d.get("success") else {}
    except Exception as e:  # noqa: BLE001
        print(f"    ! 详情 {page_id} 失败: {str(e)[:80]}", flush=True)
        return {}


# ---------------------------------------------------------------- 解析
def _as_int(v, default: int = 0) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError, AttributeError):
        return default


def _clean_phone(p) -> str:
    if not p:
        return ""
    p = str(p).strip()
    return "" if p.lower() in {"0", "nan", "none", "-", "n/a"} else p


def parse_available(raw, today: datetime.date) -> tuple[str, str, str]:
    """返回 (显示文本, 月份分档, 排序键)。过去/异常日期统一为「即日可租」。"""
    if not raw:
        return "即日可租", "now", "00000000"
    txt = str(raw).strip()
    if txt.startswith("0000"):
        return "即日可租", "now", "00000000"
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            dt = datetime.strptime(txt[:19] if fmt.startswith("%Y-%m-%d %H") else txt, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return "即日可租", "now", "00000000"
    d = dt.date()
    if d <= today:
        return "即日可租", "now", "00000000"
    y, m = d.year, d.month
    label = f"{m}月{d.day}日"
    if y != today.year:
        label = f"{y}年{label}"
    return label, f"{y:04d}-{m:02d}", d.strftime("%Y%m%d")


INSPECTION_RE = re.compile(
    r"(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}:\d{2}\s*[APap][Mm])\s+to\s+"
    r"(\d{1,2}:\d{2}\s*[APap][Mm])")


def parse_inspections(raw, now: datetime) -> list[dict]:
    """只保留未来的看房场次（官网详情页同样不展示已过期场次）。"""
    if not raw:
        return []
    out: list[dict] = []
    for line in re.split(r"[\r\n]+", str(raw)):
        m = INSPECTION_RE.search(line)
        if not m:
            continue
        day, mon, year, t1, t2 = m.groups()
        mi = MONTHS_EN.get(mon.title())
        if not mi:
            continue
        try:
            start = datetime.strptime(
                f"{int(day):02d}-{mon.title()}-{year} {t1.replace(' ', '').upper()}",
                "%d-%b-%Y %I:%M%p").replace(tzinfo=BRISBANE_TZ)
            end = datetime.strptime(
                f"{int(day):02d}-{mon.title()}-{year} {t2.replace(' ', '').upper()}",
                "%d-%b-%Y %I:%M%p").replace(tzinfo=BRISBANE_TZ)
        except ValueError:
            continue
        if start < now:
            continue
        label = f"{start.month}月{start.day}日 {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        if start.year != now.year:
            label = f"{start.year}年{label}"
        out.append({"label": label, "iso": start.strftime("%Y-%m-%d %H:%M"),
                    "key": start.strftime("%Y%m%d%H%M")})
    out.sort(key=lambda x: x["key"])
    return out


def _image_url(row: dict, detail: dict) -> str:
    img = row.get("image") or ""
    if not img:
        imgs = detail.get("images") or []
        if imgs and isinstance(imgs, list):
            img = (imgs[0] or {}).get("thumb") or ""
        if not img:
            img = detail.get("mainImage") or ""
    img = str(img or "").strip()
    if not img:
        return ""
    return img if img.startswith("http") else BASE + "/" + img.lstrip("/")


def normalize(row: dict, detail: dict, history: dict,
              now: datetime) -> dict:
    pid = str(row.get("pageId") or detail.get("pageId") or "")
    suburb = str(row.get("suburb") or detail.get("suburb") or "").strip()
    state = str(row.get("state") or detail.get("state") or "QLD").strip()
    zone = SUBURB_TO_ZONE.get(suburb, OTHER_ZONE)

    street_no = str(row.get("streetNumber") or detail.get("streetNumber") or "").strip()
    street = str(row.get("street") or detail.get("street") or "").strip()
    postcode = str(row.get("postcode") or detail.get("postcode") or "").strip()
    display_ok = str(row.get("addressDisplay") or detail.get("addressDisplay")
                     or "yes").lower() == "yes"
    if display_ok and street:
        address = ", ".join(x for x in
                            [f"{street_no} {street}".strip(), suburb, state, postcode] if x)
    elif suburb:
        address = f"{suburb}, {state} {postcode}".strip()
    else:
        address = suburb or "地址见详情"

    bed = _as_int(row.get("bedrooms", detail.get("bedrooms")))
    bath = _as_int(row.get("bathrooms", detail.get("bathrooms")))
    car = _as_int(row.get("carSpaces", detail.get("carSpaces")))
    layout = f"{bed}室{bath}卫" if bed else (row.get("category") or "房源")
    if car:
        layout += f"{car}车位"

    furn_raw = str(detail.get("furnished") if detail else "").strip().lower()
    furnished = "是" if furn_raw in {"1", "yes", "true"} else (
        "否" if furn_raw in {"0", "no", "false"} else "未知")

    rent = _as_int(row.get("price") or detail.get("price"))
    date_display, bucket, date_key = parse_available(
        detail.get("dateAvailable") or row.get("dateAvailable"), now.date())
    inspections = parse_inspections(detail.get("inspections"), now)

    phone = (_clean_phone(detail.get("listingAgentPhone"))
             or _clean_phone(detail.get("agentPhone"))
             or _clean_phone(detail.get("managerPhone")))
    contact = (str(detail.get("listingAgentName") or "").strip()
               or str(detail.get("agentContact") or "").strip()
               or str(detail.get("managerName") or "").strip()
               or str(row.get("agentContact") or "").strip()
               or str(row.get("businessName") or "").strip()
               or "见详情页")

    slug_type = CATEGORY_SLUG.get(
        str(row.get("category") or detail.get("category") or "Apartment").strip().lower(),
        "apartment-for-rent")
    sub_slug = re.sub(r"[^A-Za-z0-9]", "", suburb) or "Brisbane"
    url = f"{BASE}/{slug_type}/{sub_slug}-{state}-{pid}"

    entry = str(detail.get("entryDate") or row.get("timestamp") or "")[:10]
    is_new = False
    try:
        is_new = (now.date() - datetime.strptime(entry, "%Y-%m-%d").date()).days <= NEW_WITHIN_DAYS
    except ValueError:
        pass

    prev = history.get(pid) or {}
    prev_rent = _as_int(prev.get("rent_weekly"))
    price_change = ""
    if prev_rent > 0 and rent > 0 and rent != prev_rent:
        price_change = "down" if rent < prev_rent else "up"

    pets_raw = str(row.get("pets") or detail.get("pets") or "").lower()
    return {
        "id": pid,
        "title": str(row.get("title") or detail.get("title") or "").strip(),
        "suburb": suburb,
        "zone": zone,
        "address": address,
        "layout": layout,
        "bed": bed,
        "bath": bath,
        "car": car,
        "rent": rent,
        "bond": _as_int(row.get("bond") or detail.get("bond")),
        "category": str(row.get("category") or detail.get("category") or "").strip(),
        "furnished": furnished,
        "pets": "可养宠物" if pets_raw in {"1", "yes", "true"} else "",
        "date_display": date_display,
        "bucket": bucket,
        "date_key": date_key,
        "inspections": inspections,
        "insp_key": inspections[0]["key"] if inspections else "999999999999",
        "contact": contact,
        "phone": phone,
        "image": _image_url(row, detail),
        "url": url,
        "inspect_url": f"{BASE}/inspect/{pid}",
        "is_new": is_new,
        "price_change": price_change,
        "listed_at": entry,
    }


# ---------------------------------------------------------------- 历史
def load_history() -> dict:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8")).get("listings", {})
    except (OSError, ValueError, AttributeError):
        return {}


def save_history(items: list[dict]) -> None:
    old = load_history()
    today = datetime.now(BRISBANE_TZ).strftime("%Y-%m-%d")
    payload = {
        "listings": {
            it["id"]: {
                "rent_weekly": it["rent"],
                "city": it["zone"],
                "first_seen": (old.get(it["id"]) or {}).get("first_seen", today),
            }
            for it in items if it["id"]
        },
        "last_updated": datetime.now(BRISBANE_TZ).strftime("%Y-%m-%d %H:%M"),
    }
    HISTORY.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 生成
def month_range(items: list[dict], today: datetime.date) -> list[str]:
    """从当月到所有房源中最晚的可入住月份，连续生成（中间空月份保留）。"""
    buckets = {it["bucket"] for it in items if it["bucket"] != "now"}
    start = f"{today.year:04d}-{today.month:02d}"
    if not buckets:
        return [start]
    last = max(buckets)
    months, cur = [], start
    while cur <= last:
        months.append(cur)
        y, m = map(int, cur.split("-"))
        cur = f"{y + 1:04d}-01" if m == 12 else f"{y:04d}-{m + 1:02d}"
        if len(months) > 36:
            break
    return months


def build_meta(items: list[dict], now: datetime, months: list[str]) -> dict:
    counts = {z: sum(1 for it in items if it["zone"] == z) for z in ZONE_ORDER}
    return {
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "total": len(items),
        "months": months,
        "zones": [{"name": z, "slug": ZONE_SLUG[z], "count": counts[z]}
                  for z in ZONE_ORDER if counts[z]],
        "source_list": f"{BASE}/rental-property?location=South%20Brisbane",
        "with_slots": sum(1 for it in items if it["inspections"]),
        "available_now": sum(1 for it in items if it["bucket"] == "now"),
    }


def render(items: list[dict], meta: dict) -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    data_json = json.dumps(items, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    out = tpl.replace("__DATA_JSON__", data_json).replace("__META_JSON__", meta_json)
    out = out.replace("__SOURCE_LIST__", html.escape(meta["source_list"]))
    out = out.replace("__TOTAL__", str(meta["total"]))
    out = out.replace("__GENERATED__", html.escape(meta["generated"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", action="store_true", help="用本地缓存重新渲染")
    ap.add_argument("--dry", action="store_true", help="只打印统计")
    args = ap.parse_args()

    now = datetime.now(BRISBANE_TZ)
    history = load_history()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    if args.cache and OUT_JSON.exists():
        print("· 使用缓存数据 data/listings.json")
        items = json.loads(OUT_JSON.read_text(encoding="utf-8"))
    else:
        print("· 抓取列表 ...")
        rows = fetch_all_rentals()
        print(f"· 列表共 {len(rows)} 套，抓取详情 ...", flush=True)
        ids = [str(r["pageId"]) for r in rows]
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
            details = list(ex.map(fetch_detail, ids))
        ok = sum(1 for d in details if d)
        print(f"· 详情成功 {ok}/{len(rows)}", flush=True)
        items = [normalize(r, d, history, now) for r, d in zip(rows, details)]
        items = [it for it in items if it["id"] and it["rent"] > 0]
        items.sort(key=lambda x: (x["date_key"], x["rent"]))
        OUT_JSON.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")

    months = month_range(items, now.date())
    meta = build_meta(items, now, months)
    print(f"· 房源 {len(items)} 套 | 月份 {', '.join(months)} | "
          f"有未来看房场次 {meta['with_slots']} 套 | 即日可租 {meta['available_now']} 套")
    for z in meta["zones"]:
        print(f"    {z['name']}: {z['count']}")

    if args.dry:
        return 0

    OUT_HTML.write_text(render(items, meta), encoding="utf-8")
    save_history(items)
    print(f"· 已生成 {OUT_HTML} ({OUT_HTML.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
