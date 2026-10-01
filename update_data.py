#!/usr/bin/env python3
"""Refresh the data embedded in fisreport.html from the public sources.

    python3 update_data.py            # rebuild DATA from live sources, rewrite fisreport.html
    python3 update_data.py --check    # rebuild and report differences only

Live each run:
  * WDFW Puget Sound creel interviews (CSV export, Areas 10 and 11, every year in YEARS)
  * WDFW Lake Washington / Ballard Locks daily counts (current season page)
  * USACE Mud Mountain Dam (White River trap) workbook, if one is linked on the USACE page
    (needs `pip install openpyxl`; skipped otherwise)

Frozen (final, archived) and kept as-is unless a fresh pull returns data:
  * Ballard 2023 and 2025 (archived WDFW pages), Ballard 2024 (USACE final workbook)
  * White River trap 2023 and 2025 (archived USACE workbooks)

The WDFW expansion factors and post-season estimates (EST in the page) come from PDF
reports and are edited by hand when a new report is published.
"""
import csv, datetime, html, io, json, re, sys, urllib.request
from collections import defaultdict
from pathlib import Path

PAGE = Path(__file__).with_name('fisreport.html')
YEARS = [2023, 2024, 2025, 2026]
MONTHS = range(6, 12)
UA = {'User-Agent': 'Mozilla/5.0 (fishreport data refresh)'}
CREEL = 'https://wdfw.wa.gov/fishing/reports/creel/puget-annual'
LW = 'https://wdfw.wa.gov/fishing/reports/counts/lake-washington'
MMD = 'https://www.nws.usace.army.mil/Missions/Civil-Works/Locks-and-Dams/Mud-Mountain-Dam/Fish-Counts/'


def get(url, timeout=180):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def text(raw):
    t = re.sub(r'<script.*?</script>|<style.*?</style>', '', raw.decode('utf-8', 'ignore'), flags=re.S)
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', t)))


# ---------------------------------------------------------------- creel
def creel_year_index():
    """WDFW's sample_date is a list position (1 = newest year); map it from the live <select>."""
    page = get(CREEL).decode('utf-8', 'ignore')
    sel = re.search(r'name="sample_date".*?</select>', page, re.S).group(0)
    return {int(y): int(v) for v, y in re.findall(r'<option value="(\d+)"[^>]*>\s*(\d{4})', sel)}


def creel(idx):
    out = {'10': {}, '11': {}}
    for y in YEARS:
        rows = csv.DictReader(io.StringIO(get(f'{CREEL}/export?_format=csv&sample_date={idx[y]}').decode('utf-8-sig')))
        agg = defaultdict(lambda: {'i': 0, 'a': 0, 'ch': 0, 'co': 0, 'pk': 0})
        for r in rows:
            area = r['Catch area'].split(',')[0].replace('Area ', '')
            if area not in out:
                continue
            d = datetime.datetime.strptime(r['Sample date'], '%b %d, %Y')
            if d.month not in MONTHS:
                continue
            v = agg[(area, d.month)]
            for k, col in [('i', '# Interviews (Boat or Shore)'), ('a', 'Anglers'), ('ch', 'Chinook'), ('co', 'Coho'), ('pk', 'Pink')]:
                v[k] += int(float(r[col] or 0))
        for a in out:
            out[a][str(y)] = [agg.get((a, m)) for m in MONTHS]
    return out


# ---------------------------------------------------------------- Ballard (current season page)
def ballard_live():
    t = text(get(LW))
    year = int(re.search(r'Daily \w+ counts (\d{4}) daily counts', t).group(1))
    res = {}
    for sp, key in [('coho', 'co'), ('Chinook', 'ch'), ('sockeye', 'so')]:
        m = re.search(rf'Daily {sp} counts \d{{4}} daily counts Date Daily Count Running Total (.*?)(?= Daily \w+ counts| Ballard Locks| Annual|$)', t, re.I)
        body = re.sub(r'(\d+/\d+)\s*-\s*(\d+/\d+)(/\d+)?', r'\1-\2', m.group(1)) if m else ''
        mon = defaultdict(int)
        # rows are "date count running-total"; days not yet counted show only the running total
        for d, c in re.findall(r'(\d+/\d+(?:-\d+/\d+)?) ([\d,]+) [\d,]+(?![\d,/])', body):
            mon[int(d.split('-')[-1].split('/')[0])] += int(c.replace(',', ''))   # ranges land in their end month
        res[key] = mon
    today = datetime.date.today()
    months = []
    for m in MONTHS:
        started = datetime.date(year, m, 1) <= today
        months.append({k: res[k].get(m, 0) for k in ('ch', 'co', 'so')} if started and m <= 10 else None)
    return year, months


# ---------------------------------------------------------------- White River trap (current USACE workbook)
def mmd_live():
    try:
        import openpyxl
    except ImportError:
        print('  openpyxl not installed; skipping USACE workbook')
        return None, None
    links = re.findall(r'href="([^"]+\.xls[xm]?[^"]*)"', get(MMD).decode('utf-8', 'ignore'))
    if not links:
        print('  USACE page links no workbook right now')
        return None, None
    url = links[0] if links[0].startswith('http') else 'https://www.nws.usace.army.mil' + links[0]
    wb = openpyxl.load_workbook(io.BytesIO(get(url)), data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = next(r for r in rows if r and 'Coho' in r and 'Pink' in r)
    idx = {h: i for i, h in enumerate(head) if isinstance(h, str)}
    ch_cols = [h for h in idx if 'CH' in h or 'Jack' in h]
    mon = defaultdict(lambda: {'ch': 0, 'co': 0, 'pk': 0}); year = None
    for r in rows:
        d = next((c for c in r[:3] if isinstance(c, datetime.datetime)), None)
        if not d:
            continue
        year = d.year
        n = lambda v: v if isinstance(v, (int, float)) else 0
        mon[d.month]['ch'] += sum(n(r[idx[c]]) for c in ch_cols)
        mon[d.month]['co'] += n(r[idx['Coho']])
        mon[d.month]['pk'] += n(r[idx['Pink']])
    last = max(m for m in mon) if mon else 0
    return year, [mon[m] if m in mon and m <= last else None for m in MONTHS]


# ---------------------------------------------------------------- page I/O
def main():
    check = '--check' in sys.argv
    page = PAGE.read_text()
    old = json.loads(re.search(r'/\*DATA\*/(.*?)/\*END\*/', page, re.S).group(1))
    new = json.loads(json.dumps(old))

    print('WDFW creel ...')
    idx = creel_year_index()
    new['creel'] = creel(idx)
    print('Ballard Locks ...')
    y, months = ballard_live()
    if str(y) in new['ballard']:
        new['ballard'][str(y)] = months
    print('White River trap ...')
    y, months = mmd_live()
    if y and str(y) in new['puyallup'] and any(months):
        new['puyallup'][str(y)] = months

    changes = [(k, a, b) for k in ('creel', 'ballard', 'puyallup')
               for a, b in [(json.dumps(old[k], sort_keys=True), json.dumps(new[k], sort_keys=True))] if a != b]
    print('changed:', [c[0] for c in changes] or 'nothing')
    if check or not changes:
        return
    page = re.sub(r'/\*DATA\*/.*?/\*END\*/', lambda _: '/*DATA*/' + json.dumps(new, separators=(',', ':')) + '/*END*/', page, flags=re.S)
    page = re.sub(r'const CREEL_IDX = \{.*?\};', 'const CREEL_IDX = ' + json.dumps({str(k): v for k, v in idx.items() if k in YEARS}) + ';', page)
    page = re.sub(r'const UPDATED = \'[^\']*\';', f"const UPDATED = '{datetime.date.today():%b %-d, %Y}';", page)
    PAGE.write_text(page)
    print('wrote', PAGE.name)


if __name__ == '__main__':
    main()
