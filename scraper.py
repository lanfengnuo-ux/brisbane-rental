#!/usr/bin/env python3
"""
布里斯班租房列表 — 自动更新脚本
每周一三五 10:00 运行，抓取 The Onsite Manager 数据生成 HTML
"""
from playwright.sync_api import sync_playwright
import time, re, json, sys
from datetime import datetime
from pathlib import Path

OUTPUT_FILE = Path(__file__).parent / "index.html"
HISTORY_FILE = Path(__file__).parent / "previous_data.json"


def load_previous_data():
    """Load previous scrape data, return {listing_id: {rent, city, first_seen}}."""
    if not HISTORY_FILE.exists():
        return {}
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('listings', {})
    except (json.JSONDecodeError, KeyError, IOError):
        return {}


def save_previous_data(all_data):
    """Save current listings as previous data for next comparison."""
    listings = {}
    for city_name, city_listings in all_data.items():
        for l in city_listings:
            lid = l.get('id', '')
            if lid and lid != '?' and l.get('address') != '?':
                listings[lid] = {
                    'rent_weekly': int(l.get('rent_weekly', '0') or '0'),
                    'city': city_name,
                    'first_seen': l.get('_first_seen', datetime.now().strftime('%Y-%m-%d')),
                }
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump({'listings': listings, 'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M')}, f, ensure_ascii=False)


CITIES = {
    "Toowong": {
        "url_template": "https://www.theonsitemanager.com.au/rental-property/apartment?location=TOOWONG&proximity=0.20&page={page}",
        "link_pattern": "/apartment-for-rent/",
    },
    "South Brisbane": {
        "url_template": "https://www.theonsitemanager.com.au/rental-property?location=South%20Brisbane&page={page}",
        "link_pattern": "/apartment-for-rent/",
    },
    "Brisbane City": {
        "url_template": "https://www.theonsitemanager.com.au/rental-property/apartment?location=BRISBANE+CITY&proximity=0.20&page={page}",
        "link_pattern": "/apartment-for-rent/",
    },
}

SUBURBS_ORDER = [
    'Toowong','Taringa','Auchenflower','Indooroopilly','St Lucia','Paddington',
    'South Brisbane','Brisbane City','West End','Highgate Hill','Spring Hill',
    'Kangaroo Point','Woolloongabba','Dutton Park','Milton','Other',
]

KNOWN_SUBURBS = [
    'Toowong','West End','South Brisbane','Auchenflower','Taringa',
    'Indooroopilly','St Lucia','Milton','Paddington','Bardon',
    'Brisbane City','Fortitude Valley','Kangaroo Point','Highgate Hill',
    'Spring Hill','Woolloongabba','Dutton Park',
]


def block_resources(page):
    for ext in ['png','jpg','jpeg','gif','svg','woff','woff2','ttf']:
        page.route(f"**/*.{ext}", lambda r: r.abort())
    for domain in ['google-analytics','googletagmanager','onesignal','maps.googleapis']:
        page.route(f"**/{domain}.com/**", lambda r: r.abort())


def scrape_city(browser, city_name, config):
    """Scrape one city's listings (first 2 pages)."""
    listings = []
    seen_urls = set()

    # === Collect URLs ===
    collect_page = browser.new_page()
    block_resources(collect_page)

    for page_num in [1, 2]:
        url = config["url_template"].format(page=page_num)
        print(f"  [{city_name}] Collecting page {page_num}...")
        try:
            collect_page.goto(url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
        except:
            pass
        links = collect_page.locator(f'a[href*="{config["link_pattern"]}"]').all()
        for link in links:
            try:
                href = link.get_attribute('href')
                if href and config["link_pattern"] in href:
                    seen_urls.add(href)
            except:
                pass
    collect_page.close()
    urls = sorted(seen_urls)
    print(f"  [{city_name}] {len(urls)} unique listings")

    # === Visit detail pages ===
    for i, href in enumerate(urls):
        full_url = f"https://www.theonsitemanager.com.au{href}"
        listing_id = href.split('-')[-1]

        page = browser.new_page()
        block_resources(page)

        try:
            page.goto(full_url, wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(500)

            # Click truncated phone buttons to reveal full numbers
            for btn in page.locator('a:has-text("Call"):not([href^="tel:"])').all():
                try:
                    if '...' in btn.inner_text():
                        btn.click()
                        page.wait_for_timeout(300)
                except:
                    pass

            # Extract agent phone numbers
            agent_phones = []
            for tl in page.locator('a[href^="tel:"]').all():
                try:
                    n = tl.get_attribute('href').replace('tel:', '').strip()
                    if n not in ['0738684047', '0407769944'] and n not in agent_phones:
                        agent_phones.append(n)
                except:
                    pass

            text = page.locator('body').inner_text()
            info = {'url': full_url, 'id': listing_id}

            addr = re.search(r'Address:\s*(.+?)(?:\n|$)', text)
            info['address'] = addr.group(1).strip() if addr else '?'

            date = re.search(r'Date Available:\s*(.+?)(?:\n|$)', text)
            info['date_available'] = date.group(1).strip() if date else '?'

            rent = re.search(r'Rent:\s*\$?([\d,]+)\s*weekly', text)
            info['rent_weekly'] = rent.group(1).replace(',', '') if rent else '?'

            det = re.search(r'Details:\s*\n?(\d+)\s*\n(\d+)\s*\n?(\d*)', text)
            info['bed'] = det.group(1) if det else '?'
            info['bath'] = det.group(2) if det else '?'
            info['car'] = (det.group(3) or '0') if det else '0'

            b, t_ = info['bed'], info['bath']
            layout = f"{b}室{t_}卫" if b != '?' else '?'
            if info.get('car') and info['car'] != '0':
                layout += f"{info['car']}车位"
            info['layout'] = layout

            fm = re.search(r'(?i)Furnished:\s*(Yes|No)', text)
            if fm:
                info['furnished'] = '是' if fm.group(1).lower() == 'yes' else '否'
            elif re.search(r'(?i)fully\s*furnished|furnished\s*apartment|comes\s*furnished', text):
                info['furnished'] = '是'
            elif re.search(r'(?i)unfurnished|not\s*furnished', text):
                info['furnished'] = '否'
            else:
                info['furnished'] = '未知'

            info['phone'] = ' / '.join(agent_phones) if agent_phones else '?'
            apply_links = page.locator('a[href*="2apply.com.au"]').all()
            info['apply_link'] = apply_links[0].get_attribute('href') if apply_links else '?'
            info['contact'] = '见页面'

            # Suburb detection
            for s in KNOWN_SUBURBS:
                if s.lower() in info['address'].lower():
                    info['suburb'] = s
                    break
            else:
                parts = [p.strip() for p in info['address'].split(',')]
                info['suburb'] = parts[-3] if len(parts) >= 3 else 'Other'

            listings.append(info)

        except Exception as e:
            print(f"  [{city_name}] ✗ {listing_id}: {str(e)[:60]}")

        page.close()
        time.sleep(0.2)

    return listings


def extract_contacts(browser, city_name, config, listings):
    """Extract contact names from listing cards."""
    id_map = {l['id']: l for l in listings}

    for page_num in [1, 2]:
        page = browser.new_page()
        block_resources(page)

        url = config["url_template"].format(page=page_num)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
        except:
            pass

        text = page.locator('body').inner_text()
        blocks = re.split(r'ID:\s*(\d{8})', text)

        for i in range(1, len(blocks), 2):
            lid = blocks[i]
            block = blocks[i+1] if i+1 < len(blocks) else ''
            lines = [l.strip() for l in block.split('\n') if l.strip()]

            if lid in id_map:
                for line in reversed(lines):
                    if re.match(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}$', line):
                        if not re.search(r'(?i)OPEN|INSPECTION|Rental|Property|Apartment|Available|From|QLD|Subscribe|About\s*This|Name$|Phone$|Email$|Comments$|Find|Search|Sort|Featured|PROPERTY|LOCATION|Page|Next|Previous|Sponsored|Finance|Mortgage|Apply|Book|Register|Contact|Listing', line):
                            id_map[lid]['contact'] = line
                            break
        page.close()


def generate_html(all_data):
    """Generate the combined HTML report."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    tab_btns = ""
    panels = ""
    first = True

    for city_name, listings in all_data.items():
        slug = city_name.lower().replace(' ', '-')
        valid = [l for l in listings if l.get('address') != '?']

        active = " active" if first else ""
        first = False

        tab_btns += f'<button class="tab-btn{active}" data-slug="{slug}" onclick="switchCity(\'{slug}\')">{city_name}<span class="count">{len(valid)}</span></button>\n'

        # Group by suburb
        suburb_groups = {}
        for l in valid:
            s = l.get('suburb', 'Other')
            suburb_groups.setdefault(s, []).append(l)
        for s in suburb_groups:
            suburb_groups[s].sort(key=lambda x: int(x.get('rent_weekly', '0') or '0'))

        rows = ""
        for sub in SUBURBS_ORDER:
            if sub not in suburb_groups:
                continue
            rents = [int(l.get('rent_weekly', '0') or '0') for l in suburb_groups[sub]]
            rows += f'<tr class="cat-divider"><td colspan="7"><span class="cat-label">{sub}</span><span class="cat-count">{len(suburb_groups[sub])}套 · ${min(rents)}-${max(rents)}/周</span></td></tr>'

            for l in suburb_groups[sub]:
                rent = l.get('rent_weekly', '?')
                layout = l.get('layout', '?')
                addr = l.get('address', '?')
                furnished = l.get('furnished', '未知')
                date_avail = l.get('date_available', '?')
                contact = l.get('contact', '见页面')
                phone = l.get('phone', '?')
                detail_url = l.get('url', '#')
                bed = l.get('bed', '0')
                is_new = l.get('is_new', False)
                price_drop = l.get('price_drop', 0)

                # Date formatting for sorting
                date_sort = '99999999'
                date_display = date_avail
                try:
                    dt = datetime.strptime(date_avail, '%d %b %Y')
                    date_sort = dt.strftime('%Y%m%d')
                    today = datetime.now()
                    days_until = (dt - today).days
                    if 'Available' in date_avail.lower() or 'now' in date_avail.lower() or days_until <= 0:
                        date_sort = '00000000'
                        date_display = f'<span class="date-soon">即日可租</span>'
                    elif days_until <= 14:
                        date_display = f'<span class="date-soon">{date_avail}</span>'
                except ValueError:
                    pass

                # Search text for filtering
                search_text = f'{layout} {addr} {sub} {rent} {furnished} {contact} {phone}'.lower()

                # Data attributes for filtering/sorting
                data_attrs = f'data-furnished="{furnished}" data-bed="{bed}" data-rent="{rent}" data-date="{date_sort}" data-suburb="{sub}" data-search="{search_text}"'

                fb = {'是': '<span class="badge badge-yes">是</span>',
                      '否': '<span class="badge badge-no">否</span>'}.get(furnished, '<span class="badge badge-unk">未知</span>')
                ph = f'<a href="tel:{phone.replace(" ","")}" class="phone-link">{phone}</a>' if phone and phone != '?' else '<span class="no-link">—</span>'
                btn = f'<a href="{detail_url}" target="_blank" class="apply-btn">详情</a>' if detail_url else '<span class="no-link">—</span>'

                # Price column with badges
                price_html = f'<span class="price">${rent}</span>'
                if is_new:
                    price_html += ' <span class="badge badge-new">🆕 新上</span>'
                if price_drop > 0:
                    price_html += f' <span class="badge badge-drop">🔻 -${price_drop}</span>'

                rows += f'<tr {data_attrs}><td><span class="room-name">{layout}</span><div class="addr">{addr}</div></td><td>{fb}</td><td>{price_html}<span class="pw">/周</span></td><td>{date_display}</td><td>{contact}</td><td>{ph}</td><td>{btn}</td></tr>'

        total = len(valid)
        min_p = min(int(l['rent_weekly']) for l in valid if l['rent_weekly'].isdigit()) if valid else 0
        max_p = max(int(l['rent_weekly']) for l in valid if l['rent_weekly'].isdigit()) if valid else 0
        furn_yes = sum(1 for l in valid if l['furnished'] == '是')

        panels += f'''
    <div class="city-panel{active}" id="panel-{slug}">
        <div class="stats">
            <div class="stat"><div class="num">{total}</div><div class="lbl">总房源</div></div>
            <div class="stat"><div class="num">{len(suburb_groups)}</div><div class="lbl">覆盖区域</div></div>
            <div class="stat"><div class="num">${min_p}</div><div class="lbl">最低周租</div></div>
            <div class="stat"><div class="num">${max_p}</div><div class="lbl">最高周租</div></div>
            <div class="stat"><div class="num">{furn_yes}</div><div class="lbl">包家具</div></div>
        </div>
        <div class="filter-bar" id="filter-{slug}">
            <input type="text" class="filter-search" placeholder="🔍 搜索地址/区域/联系人..." oninput="filterTable('{slug}')">
            <div class="filter-btns">
                <span class="filter-label">家具:</span>
                <button class="fbtn active" data-filter="furnished" data-val="all" onclick="setFilter('{slug}','furnished','all',this)">全部</button>
                <button class="fbtn" data-filter="furnished" data-val="是" onclick="setFilter('{slug}','furnished','是',this)">🛋️ 包家具</button>
                <button class="fbtn" data-filter="furnished" data-val="否" onclick="setFilter('{slug}','furnished','否',this)">📦 不包家具</button>
            </div>
            <div class="filter-btns">
                <span class="filter-label">户型:</span>
                <button class="fbtn active" data-filter="layout" data-val="all" onclick="setFilter('{slug}','layout','all',this)">全部</button>
                <button class="fbtn" data-filter="layout" data-val="studio" onclick="setFilter('{slug}','layout','studio',this)">Studio</button>
                <button class="fbtn" data-filter="layout" data-val="1" onclick="setFilter('{slug}','layout','1',this)">1室</button>
                <button class="fbtn" data-filter="layout" data-val="2" onclick="setFilter('{slug}','layout','2',this)">2室</button>
                <button class="fbtn" data-filter="layout" data-val="3" onclick="setFilter('{slug}','layout','3',this)">3室+</button>
            </div>
            <div class="filter-btns filter-rent">
                <span class="filter-label">周租金:</span>
                <input type="number" class="rent-input" placeholder="最低" onchange="filterTable('{slug}')" id="rent-min-{slug}">
                <span class="rent-sep">—</span>
                <input type="number" class="rent-input" placeholder="最高" onchange="filterTable('{slug}')" id="rent-max-{slug}">
            </div>
            <span class="filter-count" id="fcount-{slug}"></span>
            <button class="fbtn fclear" onclick="clearFilters('{slug}')">✕ 清除筛选</button>
        </div>
        <div class="table-wrap">
            <table>
                <thead><tr><th class="sortable" data-sort="layout" onclick="sortTable('{slug}', this)">户型 / 地址 <span class="sa"></span></th><th class="sortable" data-sort="furnished" onclick="sortTable('{slug}', this)">家具 <span class="sa"></span></th><th class="sortable" data-sort="rent" onclick="sortTable('{slug}', this)">周租金 <span class="sa"></span></th><th class="sortable" data-sort="date" onclick="sortTable('{slug}', this)">可入住 <span class="sa"></span></th><th>联系人</th><th>电话</th><th>详情</th></tr></thead>
                <tbody>{rows}<tr class="empty-state"><td colspan="7">😕 没有匹配的房源，试试调整筛选条件</td></tr></tbody>
            </table>
        </div>
    </div>'''

    total_all = sum(len([l for l in d if l.get('address') != '?']) for d in all_data.values())

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>布里斯班租房列表 — The Onsite Manager</title>
<link href="https://api.fontshare.com/v2/css?f[]=satoshi@400,500,600,700&display=swap" rel="stylesheet">
<style>
    :root {{ --bg:#fafaf9;--card-bg:#fff;--text:#1a1a1a;--text-muted:#6b7280;--border:#e5e4e1;--green:#059669;--green-bg:#ecfdf5;--amber:#d97706;--amber-bg:#fffbeb;--red:#dc2626;--red-bg:#fef2f2;--radius:10px;--font:'Satoshi',system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif }}
    @media (prefers-color-scheme:dark){{ :root{{ --bg:#0c0c0c;--card-bg:#161616;--text:#e5e5e5;--text-muted:#8b8b8b;--border:#262626 }} .cat-divider td{{ background:#1a1a1a!important }} }}
    *,*::before,*::after{{ box-sizing:border-box;margin:0;padding:0 }}
    html{{ font-family:var(--font);-webkit-font-smoothing:antialiased;background:var(--bg);color:var(--text) }}
    body{{ max-width:1150px;margin:0 auto;padding:32px 20px 60px;line-height:1.6 }}
    .header{{ margin-bottom:20px }}
    .header h1{{ font-size:clamp(1.3rem,3vw,1.6rem);font-weight:700;letter-spacing:-0.025em;display:flex;align-items:center;gap:8px }}
    .header h1 .dot{{ width:10px;height:10px;border-radius:50%;background:#E21836;flex-shrink:0 }}
    .header .meta{{ color:var(--text-muted);font-size:0.82rem;line-height:1.7 }}
    .header a{{ color:var(--text-muted) }}
    .tab-nav{{ display:flex;gap:6px;margin-bottom:22px;overflow-x:auto;scrollbar-width:none }}
    .tab-nav::-webkit-scrollbar{{ display:none }}
    .tab-btn{{ flex-shrink:0;padding:10px 22px;border-radius:8px;cursor:pointer;font-size:0.9rem;font-weight:600;color:var(--text-muted);border:1px solid var(--border);background:var(--card-bg);font-family:var(--font);transition:all 200ms;white-space:nowrap;letter-spacing:-0.01em }}
    .tab-btn:hover{{ color:var(--text);border-color:var(--text-muted) }}
    .tab-btn.active{{ background:var(--text);color:var(--bg);border-color:var(--text) }}
    .tab-btn .count{{ font-size:0.7rem;opacity:0.5;margin-left:4px;font-weight:400 }}
    .city-panel{{ display:none }}
    .city-panel.active{{ display:block }}
    .stats{{ display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px }}
    .stat{{ background:var(--card-bg);border:1px solid var(--border);border-radius:var(--radius);padding:14px 18px;min-width:90px }}
    .stat .num{{ font-size:1.4rem;font-weight:700 }}
    .stat .lbl{{ font-size:0.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em }}
    .table-wrap{{ background:var(--card-bg);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden }}
    table{{ width:100%;border-collapse:collapse;font-size:0.875rem }}
    th{{ text-align:left;padding:12px 16px;font-weight:600;font-size:0.7rem;text-transform:uppercase;letter-spacing:0.07em;color:var(--text-muted);background:var(--bg);border-bottom:1px solid var(--border);white-space:nowrap }}
    td{{ padding:12px 16px;border-bottom:1px solid var(--border);vertical-align:top }}
    tr:last-child td{{ border-bottom:none }}
    tbody tr{{ transition:background 200ms }}
    tbody tr:hover{{ background:var(--bg) }}
    .cat-divider td{{ padding:10px 16px!important;background:var(--bg);border-bottom:1px solid var(--border) }}
    .cat-label{{ font-size:0.75rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--text) }}
    .cat-count{{ font-size:0.7rem;color:var(--text-muted);margin-left:10px;font-weight:400 }}
    .room-name{{ font-weight:600 }}
    .addr{{ font-size:0.76rem;color:var(--text-muted);margin-top:2px;line-height:1.4;max-width:280px }}
    .price{{ font-weight:700;color:#E21836 }}
    .badge{{ display:inline-block;padding:3px 10px;border-radius:99px;font-size:0.75rem;font-weight:600;white-space:nowrap }}
    .badge-yes{{ background:var(--green-bg);color:var(--green) }}
    .badge-no{{ background:var(--red-bg);color:var(--red) }}
    .badge-unk{{ background:var(--amber-bg);color:var(--amber) }}
    .apply-btn{{ display:inline-block;padding:5px 14px;background:#E21836;color:#fff;border-radius:6px;text-decoration:none;font-size:0.78rem;font-weight:600;transition:opacity 200ms }}
    .apply-btn:hover{{ opacity:0.85 }}
    .phone-link{{ color:var(--text);text-decoration:none;font-variant-numeric:tabular-nums;font-weight:500 }}
    .phone-link:hover{{ color:#E21836 }}
    .no-link{{ color:var(--text-muted);font-size:0.8rem }}
    .footer{{ margin-top:40px;padding-top:20px;border-top:1px solid var(--border);text-align:center;color:var(--text-muted);font-size:0.78rem }}
    .footer a{{ color:var(--text-muted) }}
    .filter-bar{{ display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 16px;background:var(--card-bg);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:12px;position:sticky;top:0;z-index:5 }}
    .filter-search{{ flex:0 1 200px;padding:7px 12px;border:1px solid var(--border);border-radius:6px;font-size:0.82rem;font-family:var(--font);background:var(--bg);color:var(--text);min-width:160px }}
    .filter-search:focus{{ border-color:#E21836;outline:none;box-shadow:0 0 0 2px rgba(226,24,54,.12) }}
    .filter-btns{{ display:flex;align-items:center;gap:4px }}
    .filter-label{{ font-size:0.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;margin-right:2px;white-space:nowrap }}
    .fbtn{{ padding:5px 11px;border:1px solid var(--border);border-radius:99px;cursor:pointer;font-size:0.76rem;font-weight:500;background:var(--bg);color:var(--text-muted);font-family:var(--font);transition:all 150ms;white-space:nowrap }}
    .fbtn:hover{{ border-color:var(--text-muted);color:var(--text) }}
    .fbtn.active{{ background:#E21836;color:#fff;border-color:#E21836;font-weight:600 }}
    .fclear{{ background:transparent;border-color:transparent;color:var(--text-muted);font-size:0.72rem }}
    .fclear:hover{{ color:#E21836;background:transparent }}
    .filter-rent{{ gap:6px }}
    .rent-input{{ width:72px;padding:5px 8px;border:1px solid var(--border);border-radius:6px;font-size:0.8rem;font-family:var(--font);background:var(--bg);color:var(--text) }}
    .rent-input:focus{{ border-color:#E21836;outline:none;box-shadow:0 0 0 2px rgba(226,24,54,.12) }}
    .rent-sep{{ color:var(--text-muted);font-size:0.8rem }}
    .filter-count{{ font-size:0.75rem;color:var(--text-muted);white-space:nowrap;margin-left:auto }}
    .pw{{ font-size:0.82rem;color:var(--text-muted) }}
    .badge-new{{ background:#d1fae5;color:#065f46;margin-left:4px }}
    .badge-drop{{ background:#fef2f2;color:#dc2626;margin-left:4px }}
    .date-soon{{ color:#059669;font-weight:600 }}
    th.sortable{{ cursor:pointer;user-select:none;transition:background 150ms }}
    th.sortable:hover{{ background:#ffe5e6 }}
    .sa{{ font-size:0.65rem;margin-left:3px;opacity:0.4 }}
    .sa.asc{{ opacity:1;color:#E21836 }}
    .sa.desc{{ opacity:1;color:#E21836 }}
    tr.hidden{{ display:none }}
    .empty-state{{ text-align:center;padding:40px 20px;color:var(--text-muted);display:none }}
    .empty-state.show{{ display:table-row }}
    .empty-state td{{ padding:40px 20px!important }}
    @media(max-width:768px){{ body{{ padding:20px 12px 50px }} .table-wrap{{ overflow-x:auto;-webkit-overflow-scrolling:touch }} table{{ min-width:720px;font-size:0.8rem }} .stats{{ gap:8px }} .stat{{ padding:10px 14px }} }}
</style>
</head>
<body>
<div class="header">
    <h1><span class="dot"></span>布里斯班租房列表</h1>
    <p class="meta">📍 数据来源: <a href="https://www.theonsitemanager.com.au" target="_blank">The Onsite Manager</a> &ensp;|&ensp; 更新于 {now} &ensp;|&ensp; 共 {total_all} 套可租房源 &ensp;|&ensp; 每个工作日 10:10 自动更新</p>
</div>
<nav class="tab-nav">{tab_btns}</nav>
{panels}
<div class="footer"><p>数据来源: <a href="https://www.theonsitemanager.com.au" target="_blank">theonsitemanager.com.au</a> · 更新于 {now}</p></div>
<script>
// --- Filter & Sort State (per tab) ---
const _state = {{}};
function st(slug) {{
    if (!_state[slug]) _state[slug] = {{furnished:'all',layout:'all',sortCol:null,sortDir:1}};
    return _state[slug];
}}

function switchCity(slug) {{
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('.city-panel').forEach(p=>p.classList.remove('active'));
    document.querySelector('.tab-btn[data-slug="'+slug+'"]').classList.add('active');
    document.getElementById('panel-'+slug).classList.add('active');
    filterTable(slug);
}}

// --- Filter ---
function setFilter(slug, type, val, btn) {{
    st(slug)[type] = val;
    btn.parentElement.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    filterTable(slug);
}}

function clearFilters(slug) {{
    var s = st(slug);
    s.furnished = 'all'; s.layout = 'all';
    var bar = document.getElementById('filter-'+slug);
    if(bar){{
        bar.querySelectorAll('.fbtn').forEach(function(b){{ if(b.getAttribute('data-val')==='all') b.classList.add('active'); else b.classList.remove('active'); }});
        var mi = document.getElementById('rent-min-'+slug), mx = document.getElementById('rent-max-'+slug);
        if(mi) mi.value = ''; if(mx) mx.value = '';
        var si = bar.querySelector('.filter-search');
        if(si) si.value = '';
    }}
    filterTable(slug);
}}

function filterTable(slug) {{
    var s = st(slug);
    var panel = document.getElementById('panel-'+slug);
    if(!panel || !panel.classList.contains('active')) return;
    var rows = panel.querySelectorAll('tbody tr:not(.cat-divider):not(.empty-state)');
    var searchInput = panel.querySelector('.filter-search');
    var search = searchInput ? searchInput.value.toLowerCase() : '';
    var rentMin = parseInt((document.getElementById('rent-min-'+slug)||{{}}).value) || 0;
    var rentMax = parseInt((document.getElementById('rent-max-'+slug)||{{}}).value) || 99999;
    var count = 0;
    var visibleSuburbs = {{}};
    rows.forEach(function(r){{
        var show = true;
        if (s.furnished !== 'all' && r.getAttribute('data-furnished') !== s.furnished) show = false;
        var bed = parseInt(r.getAttribute('data-bed')||'0');
        if (s.layout === 'studio' && bed !== 0) show = false;
        else if (s.layout === '1' && bed !== 1) show = false;
        else if (s.layout === '2' && bed !== 2) show = false;
        else if (s.layout === '3' && bed < 3) show = false;
        var rent = parseInt(r.getAttribute('data-rent')) || 0;
        if (rent > 0 && (rent < rentMin || rent > rentMax)) show = false;
        if (search && (r.getAttribute('data-search')||'').indexOf(search) === -1) show = false;
        if (show) {{ r.classList.remove('hidden'); count++; visibleSuburbs[r.getAttribute('data-suburb')]=true; }}
        else r.classList.add('hidden');
    }});
    // Hide empty suburb dividers
    panel.querySelectorAll('tbody tr.cat-divider').forEach(function(d){{
        var label = (d.querySelector('.cat-label')||{{}}).textContent||'';
        d.classList.toggle('hidden', !visibleSuburbs[label]);
    }});
    // Update count
    var fc = document.getElementById('fcount-'+slug);
    if (fc) fc.textContent = '找到 '+count+' 套';
    // Show/hide empty state
    var es = panel.querySelector('.empty-state');
    if (es) es.classList.toggle('show', count === 0);
    // Re-apply current sort if active
    if (s.sortCol) {{
        var th = document.querySelector('#panel-'+slug+' th[data-sort="'+s.sortCol+'"]');
        if (th) sortTable(slug, th, true);
    }}
}}

// --- Sort ---
function sortTable(slug, th, keepDir) {{
    if (!th) return;
    var s = st(slug);
    var col = th.getAttribute('data-sort');
    if (!keepDir) {{
        if (s.sortCol === col) s.sortDir *= -1;
        else {{ s.sortCol = col; s.sortDir = 1; }}
    }}
    var dir = s.sortDir;
    // Update arrows
    document.querySelectorAll('#panel-'+slug+' th .sa').forEach(function(a){{ a.textContent='';a.classList.remove('asc','desc'); }});
    var arrow = th.querySelector('.sa');
    if (arrow) {{ arrow.textContent = dir > 0 ? '▲' : '▼'; arrow.classList.add(dir > 0 ? 'asc' : 'desc'); }}
    // Sort
    var panel = document.getElementById('panel-'+slug);
    var tbody = panel.querySelector('tbody');
    var rows = Array.from(tbody.querySelectorAll('tr:not(.cat-divider):not(.empty-state)'));
    rows.sort(function(a,b){{
        var va, vb;
        if (col === 'rent') {{ va = parseInt(a.getAttribute('data-rent'))||0; vb = parseInt(b.getAttribute('data-rent'))||0; }}
        else if (col === 'date') {{ va = a.getAttribute('data-date')||'99999999'; vb = b.getAttribute('data-date')||'99999999'; }}
        else if (col === 'furnished') {{ var fo={{'是':1,'否':2,'未知':3}}; va=fo[a.getAttribute('data-furnished')]||2; vb=fo[b.getAttribute('data-furnished')]||2; }}
        else if (col === 'layout') {{ va = parseInt(a.getAttribute('data-bed'))||0; vb = parseInt(b.getAttribute('data-bed'))||0; if(va===vb){{ va=(a.getAttribute('data-suburb')||''); vb=(b.getAttribute('data-suburb')||''); return va.localeCompare(vb)*dir; }} }}
        if (typeof va === 'string') return va.localeCompare(vb) * dir;
        return (va - vb) * dir;
    }});
    // Reinsert rows in order
    var fragment = document.createDocumentFragment();
    rows.forEach(function(r){{ fragment.appendChild(r); }});
    tbody.appendChild(fragment);
    // Update suburb dividers
    var vs = {{}};
    rows.forEach(function(r){{ if(!r.classList.contains('hidden')) vs[r.getAttribute('data-suburb')]=true; }});
    panel.querySelectorAll('tbody tr.cat-divider').forEach(function(d){{
        var lbl = (d.querySelector('.cat-label')||{{}}).textContent||'';
        d.classList.toggle('hidden', !vs[lbl]);
    }});
}}
</script>
</body>
</html>'''


def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting rental report update...")
    all_data = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])

        for city_name, config in CITIES.items():
            print(f"\n--- {city_name} ---")
            listings = scrape_city(browser, city_name, config)
            extract_contacts(browser, city_name, config, listings)
            all_data[city_name] = listings
            valid = sum(1 for l in listings if l.get('address') != '?')
            print(f"  ✓ {city_name}: {valid} listings")

        browser.close()

    total_listings = sum(len(v) for v in all_data.values() if v)
    if total_listings == 0:
        print("\n❌ ERROR: No data fetched for any city! Refusing to write empty report.")
        print("   (GitHub runner IP likely blocked. Run locally instead.)")
        sys.exit(1)

    # Compare with previous data for new/drop detection
    previous = load_previous_data()
    today_str = datetime.now().strftime('%Y-%m-%d')
    new_count = 0
    drop_count = 0
    for city_name in all_data:
        for l in all_data[city_name]:
            lid = l.get('id', '')
            if not lid or lid == '?' or l.get('address') == '?':
                continue
            rent = int(l.get('rent_weekly', '0') or '0')
            if lid in previous:
                prev = previous[lid]
                prev_rent = prev.get('rent_weekly', 0)
                if rent > 0 and prev_rent > 0 and rent < prev_rent:
                    l['price_drop'] = prev_rent - rent
                    drop_count += 1
                l['_first_seen'] = prev.get('first_seen', today_str)
            else:
                l['is_new'] = True
                l['_first_seen'] = today_str
                new_count += 1
    print(f"\n  🆕 {new_count} new, 🔻 {drop_count} price drops")

    html = generate_html(all_data)
    OUTPUT_FILE.write_text(html, encoding='utf-8')
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ✅ Saved to {OUTPUT_FILE}")

    # Save current data for next comparison
    save_previous_data(all_data)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Saved comparison data to {HISTORY_FILE}")


if __name__ == "__main__":
    main()
