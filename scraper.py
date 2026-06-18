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

CITIES = {
    "Toowong": {
        "url_template": "https://www.theonsitemanager.com.au/rental-property/apartment?location=TOOWONG&proximity=0.20&page={page}",
        "link_pattern": "/apartment-for-rent/",
    },
    "South Brisbane": {
        "url_template": "https://www.theonsitemanager.com.au/rental-property?location=South%20Brisbane&page={page}",
        "link_pattern": "/apartment-for-rent/",
    },
}

SUBURBS_ORDER = [
    'Toowong','Taringa','Auchenflower','Indooroopilly','St Lucia','Paddington',
    'South Brisbane','West End','Highgate Hill','Spring Hill','Kangaroo Point',
    'Brisbane City','Woolloongabba','Dutton Park','Milton','Other',
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
            page.goto(full_url, wait_until="load", timeout=20000)
            page.wait_for_timeout(800)

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

                fb = {'是': '<span class="badge badge-yes">是</span>',
                      '否': '<span class="badge badge-no">否</span>'}.get(furnished, '<span class="badge badge-unk">未知</span>')
                ph = f'<a href="tel:{phone.replace(" ","")}" class="phone-link">{phone}</a>' if phone and phone != '?' else '<span class="no-link">—</span>'
                btn = f'<a href="{detail_url}" target="_blank" class="apply-btn">详情</a>' if detail_url else '<span class="no-link">—</span>'

                rows += f'<tr><td><span class="room-name">{layout}</span><div class="addr">{addr}</div></td><td>{fb}</td><td><span class="price">${rent}</span>/周</td><td>{date_avail}</td><td>{contact}</td><td>{ph}</td><td>{btn}</td></tr>'

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
        <div class="table-wrap">
            <table>
                <thead><tr><th>户型 / 地址</th><th>家具</th><th>周租金</th><th>可入住</th><th>联系人</th><th>电话</th><th>详情</th></tr></thead>
                <tbody>{rows}</tbody>
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
    @media(max-width:768px){{ body{{ padding:20px 12px 50px }} .table-wrap{{ overflow-x:auto;-webkit-overflow-scrolling:touch }} table{{ min-width:720px;font-size:0.8rem }} .stats{{ gap:8px }} .stat{{ padding:10px 14px }} }}
</style>
</head>
<body>
<div class="header">
    <h1><span class="dot"></span>布里斯班租房列表</h1>
    <p class="meta">📍 数据来源: <a href="https://www.theonsitemanager.com.au" target="_blank">The Onsite Manager</a> &ensp;|&ensp; 更新于 {now} &ensp;|&ensp; 共 {total_all} 套可租房源 &ensp;|&ensp; 每周一三五 10:00 自动更新</p>
</div>
<nav class="tab-nav">{tab_btns}</nav>
{panels}
<div class="footer"><p>数据来源: <a href="https://www.theonsitemanager.com.au" target="_blank">theonsitemanager.com.au</a> · 更新于 {now}</p></div>
<script>
function switchCity(slug) {{
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('.city-panel').forEach(p=>p.classList.remove('active'));
    document.querySelector('.tab-btn[data-slug="'+slug+'"]').classList.add('active');
    document.getElementById('panel-'+slug).classList.add('active');
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

    html = generate_html(all_data)
    OUTPUT_FILE.write_text(html, encoding='utf-8')
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ✅ Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
