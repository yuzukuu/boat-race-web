from django.contrib import admin
from django.urls import path
from django.http import HttpResponse, JsonResponse
import requests
from bs4 import BeautifulSoup
from datetime import date, datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
from concurrent.futures import ThreadPoolExecutor, as_completed
import re, random, time, threading

_cache = {}
CACHE_TTL = 300
_last_refresh_time = None
_bg_thread = None
REFRESH_INTERVAL = 290
BET = 300

def _cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() - entry[1] < CACHE_TTL:
        return entry[0]
    return None

def _cache_set(key, val):
    _cache[key] = (val, time.time())

def _fetch_and_save_date(hd):
    """指定日の全レースを取得してDBに保存"""
    _cache.pop(f"stadiums_{hd}", None)
    stadiums = get_stadiums_for_date(hd)
    if not stadiums:
        return
    tasks = [(s, r) for s in stadiums[:6] for r in range(1, 13)]
    for s, r in tasks:
        _cache.pop(f"card_{s['code']}_{hd}_{r}", None)
        _cache.pop(f"result_{s['code']}_{hd}_{r}", None)
        _cache.pop(f"before_{s['code']}_{hd}_{r}", None)
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures  = [executor.submit(get_race_card,   s["code"], hd, r) for s, r in tasks]
        futures += [executor.submit(get_race_result,  s["code"], hd, r) for s, r in tasks]
        futures += [executor.submit(get_before_info,  s["code"], hd, r) for s, r in tasks]
        futures += [executor.submit(get_race_odds,    s["code"], hd, r) for s, r in tasks]
        for f in as_completed(futures):
            pass
    try:
        from boat_race.agent import PredictionAgent
        agent = PredictionAgent()
        # Pass 1: 予測を先に作成（オッズ込みでKelly判断）
        for s, rno in tasks:
            jcd   = s["code"]
            boats = _cache_get(f"card_{jcd}_{hd}_{rno}")
            if not boats:
                continue
            before_info = _cache_get(f"before_{jcd}_{hd}_{rno}")
            odds        = _cache_get(f"odds_{jcd}_{hd}_{rno}") or {}
            try:
                agent.predict(boats, hd, jcd, s["name"], rno, before_info, odds)
            except Exception:
                pass
        # Pass 2: 結果を紐付け（hit=True/False を設定）
        for s, rno in tasks:
            jcd    = s["code"]
            result = _cache_get(f"result_{jcd}_{hd}_{rno}")
            if not result:
                continue
            try:
                actual = int(result[0].get("boat", ""))
                payout = _cache_get(f"payout_{jcd}_{hd}_{rno}")
                agent.update_with_result(hd, jcd, rno, actual, payout)
            except Exception:
                pass
    except Exception:
        pass

def _update_results_for_date(hd):
    """actual_winner が未設定の予測に対して結果を再取得して設定する"""
    from boat_race.models import RacePrediction
    from boat_race.agent import PredictionAgent
    pending = list(RacePrediction.objects.filter(date=hd, actual_winner__isnull=True))
    if not pending:
        return
    agent = PredictionAgent()
    for pred in pending:
        # キャッシュをクリアして再取得
        _cache.pop(f"result_{pred.stadium_code}_{hd}_{pred.race_no}", None)
        result = get_race_result(pred.stadium_code, hd, pred.race_no)
        if not result:
            continue
        try:
            actual = int(result[0].get("boat", ""))
            payout = _cache_get(f"payout_{pred.stadium_code}_{hd}_{pred.race_no}")
            agent.update_with_result(hd, pred.stadium_code, pred.race_no, actual, payout)
        except Exception:
            pass

def _backfill_missing_dates(days=7):
    """過去N日間でDBにデータがない/不完全な日を補完"""
    from boat_race.models import RacePrediction
    today = date.today()
    for i in range(1, days + 1):
        ds = (today - timedelta(days=i)).strftime("%Y%m%d")
        count = RacePrediction.objects.filter(date=ds).count()
        if count == 0:
            # 予測が全くない → フル取得
            try:
                _fetch_and_save_date(ds)
            except Exception:
                pass
        else:
            # 予測はあるが actual_winner 未設定のものがある → 結果だけ再取得
            try:
                _update_results_for_date(ds)
            except Exception:
                pass

def _force_refresh_all():
    global _last_refresh_time
    _cache.pop("race_date", None)
    hd = find_race_date()
    _fetch_and_save_date(hd)
    _last_refresh_time = datetime.now(JST)

def _background_worker():
    time.sleep(3)
    loop_count = 0
    while True:
        try:
            _force_refresh_all()
        except Exception:
            pass
        # 起動直後 (loop_count=0) と約24時間ごとに過去データを補完
        if loop_count % 288 == 0:
            try:
                _backfill_missing_dates(days=7)
            except Exception:
                pass
        loop_count += 1
        time.sleep(REFRESH_INTERVAL)

def start_background_refresh():
    global _bg_thread
    if _bg_thread is None or not _bg_thread.is_alive():
        _bg_thread = threading.Thread(target=_background_worker, daemon=True, name="boat_race_bg_refresh")
        _bg_thread.start()

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
BASE = "https://www.boatrace.jp/owpc/pc/race"
STADIUMS = {
    "01":"桐生","02":"戸田","03":"江戸川","04":"平和島","05":"多摩川",
    "06":"浜名湖","07":"蒲郡","08":"常滑","09":"津","10":"三国",
    "11":"琵琶湖","12":"住之江","13":"尼崎","14":"鳴門","15":"丸亀",
    "16":"児島","17":"宮島","18":"徳山","19":"下関","20":"若松",
    "21":"芦屋","22":"福岡","23":"唐津","24":"大村"
}

def find_race_date():
    cached = _cache_get("race_date")
    if cached:
        return cached
    today = date.today()
    ds = today.strftime("%Y%m%d")
    try:
        r = requests.get(f"{BASE}/index?hd={ds}", headers=HEADERS, timeout=10)
        if "text_place1_" in r.text or "jcd=" in r.text:
            _cache_set("race_date", ds)
            return ds
    except:
        pass
    for days_ago in range(1, 5):
        d = today - timedelta(days=days_ago)
        ds = d.strftime("%Y%m%d")
        try:
            r = requests.get(f"{BASE}/index?hd={ds}", headers=HEADERS, timeout=10)
            if "text_place1_" in r.text or "jcd=" in r.text:
                _cache_set("race_date", ds)
                return ds
        except:
            continue
    result = today.strftime("%Y%m%d")
    _cache_set("race_date", result)
    return result

def get_stadiums_for_date(hd):
    cache_key = f"stadiums_{hd}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        r = requests.get(f"{BASE}/index?hd={hd}", headers=HEADERS, timeout=10)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        seen = set()
        races = []
        for img in soup.select("img[src*='text_place1_']"):
            m = re.search(r'text_place1_(\d+)', img.get("src",""))
            if m:
                code = m.group(1).zfill(2)
                if code not in seen:
                    seen.add(code)
                    races.append({"code": code, "name": STADIUMS.get(code, f"場{code}")})
        for a in soup.select("a[href*='jcd=']"):
            m = re.search(r'jcd=(\d+)', a.get("href", ""))
            if m:
                code = m.group(1).zfill(2)
                if code not in seen and code in STADIUMS:
                    seen.add(code)
                    races.append({"code": code, "name": STADIUMS.get(code, f"場{code}")})
        _cache_set(cache_key, races)
        return races
    except:
        return []

def get_race_card(jcd, hd, rno):
    cache_key = f"card_{jcd}_{hd}_{rno}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    url = f"{BASE}/racelist?jcd={jcd}&hd={hd}&rno={rno}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        boats = []
        rows = soup.select("tbody.is-fs12")
        for i, row in enumerate(rows[:6], 1):
            name_el = row.select_one(".is-fs18")
            name = name_el.get_text(strip=True) if name_el else f"選手{i}"
            nums = re.findall(r'\d+\.\d+', row.get_text())
            win_rate   = nums[0] if len(nums) > 0 else "3.00"
            place_rate = nums[1] if len(nums) > 1 else "30.0"
            local_rate = nums[2] if len(nums) > 2 else win_rate
            motor_rate = nums[4] if len(nums) > 4 else "30.0"
            boats.append({
                "number": i, "name": name,
                "win_rate": win_rate, "place_rate": place_rate,
                "local_rate": local_rate, "motor_rate": motor_rate,
            })
        _cache_set(cache_key, boats)
        return boats
    except:
        return []

_KANJI_RANK = {'１': 1, '２': 2, '３': 3, '４': 4, '５': 5, '６': 6}

def get_race_result(jcd, hd, rno):
    cache_key = f"result_{jcd}_{hd}_{rno}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    url = f"{BASE}/raceresult?jcd={jcd}&hd={hd}&rno={rno}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        # 着順・艇番が含まれる行を全 tbody から探す
        for tbody in soup.find_all("tbody"):
            for row in tbody.find_all("tr"):
                tds = row.find_all("td")
                if len(tds) < 3:
                    continue
                rank_text = tds[0].get_text(strip=True)
                boat_text = tds[1].get_text(strip=True)
                # 着順: 漢数字 or 算用数字 1〜6
                rank_num = _KANJI_RANK.get(rank_text)
                if rank_num is None:
                    if rank_text.isdigit() and 1 <= int(rank_text) <= 6:
                        rank_num = int(rank_text)
                    else:
                        continue
                # 艇番: 1〜6
                boat_digits = re.sub(r'\D', '', boat_text)
                if not boat_digits or not (1 <= int(boat_digits) <= 6):
                    continue
                # 選手名: 先頭の登録番号（4桁）を除去
                name_raw = tds[2].get_text(strip=True)
                name = re.sub(r'^\d{4}\s*', '', name_raw).strip() or name_raw
                results.append({"rank": str(rank_num), "boat": boat_digits, "name": name})
        # 着順でソートして1〜6着に絞る
        results.sort(key=lambda x: int(x["rank"]))
        results = results[:6]
        payout_key = f"payout_{jcd}_{hd}_{rno}"
        if results and _cache_get(payout_key) is None:
            payout = _extract_tansho_payout(soup)
            if payout:
                _cache_set(payout_key, payout)
        _cache_set(cache_key, results)
        return results
    except:
        return []

def get_before_info(jcd, hd, rno):
    cache_key = f"before_{jcd}_{hd}_{rno}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    url = f"{BASE}/beforeinfo?jcd={jcd}&hd={hd}&rno={rno}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        boats = {}
        for row in soup.select("tbody tr"):
            tds = row.select("td")
            if len(tds) < 5:
                continue
            try:
                num = int(re.sub(r'\D', '', tds[0].get_text(strip=True)))
                if not (1 <= num <= 6):
                    continue
                tilt_text  = tds[2].get_text(strip=True)
                ttime_text = tds[3].get_text(strip=True)
                st_text    = tds[4].get_text(strip=True)
                tilt  = float(tilt_text)  if re.match(r'-?\d+\.?\d*', tilt_text)  else 0.0
                ttime = float(ttime_text) if re.match(r'\d+\.\d+',    ttime_text) else 7.0
                st    = int(re.sub(r'\D', '', st_text)) if re.search(r'\d', st_text) else 15
                boats[num] = {'tilt': tilt, 'tenji_time': ttime, 'tenji_st': st}
            except Exception:
                continue
        weather = {}
        text = soup.get_text()
        for pattern, key in [
            (r'天候[：:\s]+([^\s\n　]+)', 'tenki'),
            (r'風速[：:\s]+(\d+)',         'wind_speed'),
            (r'波高[：:\s]+(\d+)',         'wave'),
            (r'水温[：:\s]+(\d+\.?\d*)',   'water_temp'),
        ]:
            m = re.search(pattern, text)
            if m:
                weather[key] = m.group(1)
        result = {'boats': boats, 'weather': weather}
        _cache_set(cache_key, result)
        return result
    except Exception:
        return {'boats': {}, 'weather': {}}

def get_race_odds(jcd, hd, rno):
    """単勝オッズを取得: {boat_num: payout_per_100}
    例: {1: 250, 2: 480, 3: 820, ...}
    レース前のみ有効。結果確定後・未開催日は空辞書を返す。
    """
    cache_key = f"odds_{jcd}_{hd}_{rno}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    url = f"{BASE}/oddstf?jcd={jcd}&hd={hd}&rno={rno}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        odds = {}
        for tbody in soup.find_all("tbody"):
            for row in tbody.find_all("tr"):
                tds = row.find_all("td")
                if len(tds) < 2:
                    continue
                boat_digits = re.sub(r'\D', '', tds[0].get_text(strip=True))
                if not boat_digits or not (1 <= int(boat_digits) <= 6):
                    continue
                for td in tds[1:]:
                    text = td.get_text(strip=True).replace(',', '').replace('¥', '').strip()
                    try:
                        val = float(text)
                        if 1.0 <= val < 100:       # 小数オッズ (例: 2.5倍)
                            payout = int(val * 100)
                        elif 100 <= val <= 99900:   # 払戻額形式 (例: 250円)
                            payout = int(val)
                        else:
                            continue
                        odds[int(boat_digits)] = payout
                        break
                    except Exception:
                        pass
        if odds:
            _cache_set(cache_key, odds)
        return odds
    except Exception:
        return {}

def _extract_tansho_payout(soup):
    try:
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            texts = [c.get_text(strip=True) for c in cells]
            if not any("単勝" in t for t in texts):
                continue
            # 単勝行: ['単勝', '艇番', '¥380', ...]
            for text in texts:
                # ¥記号を除去して数値抽出
                clean = text.replace('¥', '').replace(',', '').strip()
                if clean.isdigit():
                    val = int(clean)
                    if 100 <= val <= 99900:
                        return val
            # フォールバック: テキスト全体から単勝の後ろの数値
            combined = " ".join(texts)
            for m in re.finditer(r'¥?([\d,]+)', combined):
                val = int(m.group(1).replace(',', ''))
                if 100 <= val <= 99900:
                    return val
    except:
        pass
    return None

def _make_balance_chart(history):
    if not history:
        return '<p style="color:#bbb;text-align:center;padding:24px;font-size:.85em">表示できるデータがありません</p>'
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 700, 200, 55, 20, 18, 38
    iW = W - PAD_L - PAD_R
    iH = H - PAD_T - PAD_B
    balances = [0] + [b for _, b, _ in history]
    labels   = ["開始"] + [l for l, _, _ in history]
    min_b = min(min(balances), 0)
    max_b = max(max(balances), 0)
    rng = max_b - min_b or 1

    def px(i): return PAD_L + iW * i / (len(balances) - 1) if len(balances) > 1 else PAD_L
    def py(b): return PAD_T + iH * (1 - (b - min_b) / rng)

    y0 = py(0)
    final = balances[-1]
    line_color = "#38a169" if final >= 0 else "#e53e3e"
    fill_color = "#c6f6d5" if final >= 0 else "#fed7d7"

    pts      = " ".join(f"{px(i):.1f},{py(b):.1f}" for i, b in enumerate(balances))
    fill_pts = f"{px(0):.1f},{y0:.1f} {pts} {px(len(balances)-1):.1f},{y0:.1f}"

    svg  = f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:{H}px">'
    svg += f'<line x1="{PAD_L}" y1="{y0:.1f}" x2="{W-PAD_R}" y2="{y0:.1f}" stroke="#a0aec0" stroke-width="1.5" stroke-dasharray="5,3"/>'
    for bv in [min_b, max_b]:
        if bv == 0: continue
        yv = py(bv)
        svg += f'<line x1="{PAD_L}" y1="{yv:.1f}" x2="{W-PAD_R}" y2="{yv:.1f}" stroke="#e2e8f0" stroke-width="1"/>'
    svg += f'<polygon points="{fill_pts}" fill="{fill_color}" opacity="0.45"/>'
    svg += f'<polyline points="{pts}" fill="none" stroke="{line_color}" stroke-width="2.5" stroke-linejoin="round"/>'
    step = max(1, len(balances) // 10)
    for i, (b, lbl) in enumerate(zip(balances, labels)):
        cx, cy = px(i), py(b)
        dc = "#38a169" if (i == 0 or history[i-1][2] >= 0) else "#e53e3e"
        svg += f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="{dc}" stroke="white" stroke-width="1"/>'
        if i % step == 0 or i == len(balances) - 1:
            svg += f'<text x="{cx:.1f}" y="{H-PAD_B+13}" text-anchor="middle" font-size="9" fill="#718096">{lbl}</text>'
    svg += f'<text x="{PAD_L-5}" y="{y0+4:.1f}" text-anchor="end" font-size="10" fill="#718096">0</text>'
    if max_b != 0:
        svg += f'<text x="{PAD_L-5}" y="{py(max_b)+4:.1f}" text-anchor="end" font-size="10" fill="#38a169">+{max_b:,}</text>'
    if min_b != 0:
        svg += f'<text x="{PAD_L-5}" y="{py(min_b)+4:.1f}" text-anchor="end" font-size="10" fill="#e53e3e">{min_b:,}</text>'
    svg += '</svg>'
    return svg

def simulate_race(boats, n=5000):
    if not boats: return {}
    rates = []
    for b in boats:
        try: r = float(b.get("win_rate","3.0"))
        except: r = 3.0
        rates.append(max(r, 0.1))
    total = sum(rates)
    probs = [r/total for r in rates]
    results = random.choices(range(len(boats)), weights=probs, k=n)
    counts = {}
    for r in results: counts[r] = counts.get(r, 0) + 1
    return {b["number"]: round(counts.get(i,0)/n*100, 1) for i, b in enumerate(boats)}

_CSS_BASE = """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;color:#1a1a2e;min-height:100vh}
.wrap{max-width:860px;margin:0 auto;padding:16px}
.hdr{background:linear-gradient(135deg,#4c51bf,#6b46c1);color:white;padding:16px 20px;border-radius:10px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}
.hdr h1{font-size:1.1em;font-weight:700}
.hdr .meta{font-size:.78em;opacity:.85;text-align:right}
.live{display:inline-block;width:8px;height:8px;border-radius:50%;background:#68d391;margin-right:4px;animation:blink 2s infinite;vertical-align:middle}
.live.off{background:#fc8181;animation:none}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.sc{background:white;padding:12px 10px;border-radius:8px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.sc .lbl{font-size:.68em;color:#888;margin-bottom:3px}
.sc .v{font-size:1.5em;font-weight:700}
.v.pos{color:#38a169}.v.neg{color:#e53e3e}
.card{background:white;border-radius:10px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h2{font-size:.9em;color:#4c51bf;margin-bottom:12px;font-weight:600;padding-bottom:8px;border-bottom:1px solid #f0f0f0}
table{width:100%;border-collapse:collapse}
th{font-size:.72em;color:#999;padding:6px 8px;border-bottom:2px solid #f0f0f0;text-align:left;white-space:nowrap}
td{padding:11px 8px;border-bottom:1px solid #f7f7f7;font-size:.87em}
.row-link{cursor:pointer;transition:background .12s}
.row-link:hover{background:#f8f8ff}
.bal{font-weight:700}.bal.pos{color:#38a169}.bal.neg{color:#e53e3e}
.t-today{background:#4c51bf;color:white;font-size:.62em;padding:1px 5px;border-radius:6px;margin-left:5px;vertical-align:middle}
.empty{text-align:center;color:#aaa;padding:20px 0}
.btn{display:inline-block;padding:9px 16px;border:none;border-radius:7px;cursor:pointer;font-size:.85em;font-weight:600;text-decoration:none;margin-right:8px}
.btn-p{background:#4c51bf;color:white}.btn-p:hover{background:#434190}
.btn-g{background:#edf2f7;color:#4a5568}.btn-g:hover{background:#e2e8f0}
.footer{text-align:center;color:#bbb;font-size:.73em;padding:12px 0}
@media(max-width:600px){.stats{grid-template-columns:repeat(2,1fr)}}
"""

def _get_daily_summaries():
    from boat_race.models import RacePrediction
    from collections import defaultdict
    preds = RacePrediction.objects.filter(hit__isnull=False).order_by('date', 'race_no')
    daily = defaultdict(lambda: {'hits': 0, 'total': 0, 'balance': 0, 'skipped': 0})
    for p in preds:
        bet = p.bet_amount
        if bet == 0:
            daily[p.date]['skipped'] += 1
            continue
        if p.hit and p.payout is None:
            continue
        d = daily[p.date]
        d['total'] += 1
        if p.hit:
            gain = p.payout * (bet // 100) - bet
            d['balance'] += gain
            d['hits'] += 1
        else:
            d['balance'] -= bet
    return dict(sorted(daily.items(), reverse=True))


def dashboard(request):
    from boat_race.agent import PredictionAgent
    hd      = find_race_date()
    hd_disp = f"{hd[:4]}/{hd[4:6]}/{hd[6:]}"
    now     = datetime.now(JST).strftime("%H:%M")
    bg_cls  = "live" if (_bg_thread and _bg_thread.is_alive()) else "live off"
    bg_time = _last_refresh_time.strftime('%H:%M') if _last_refresh_time else '---'

    agent       = PredictionAgent()
    agent_stats = agent.get_stats(30)
    w           = agent.weights

    daily      = _get_daily_summaries()
    total_bal  = sum(d['balance'] for d in daily.values())
    total_inv  = sum(d['total'] * BET for d in daily.values())
    total_hits = sum(d['hits']   for d in daily.values())
    total_rcs  = sum(d['total']  for d in daily.values())
    roi        = round(total_bal / total_inv * 100, 1) if total_inv else 0

    # 収支チャート（日別推移）
    chart_pts = []
    running = 0
    for ds in sorted(daily.keys()):
        running += daily[ds]['balance']
        chart_pts.append((f"{ds[4:6]}/{ds[6:]}", running, daily[ds]['balance']))
    chart_svg = _make_balance_chart(chart_pts)

    bal_str = f"+{total_bal:,}" if total_bal >= 0 else f"{total_bal:,}"
    bal_cls = "pos" if total_bal >= 0 else "neg"
    roi_str = f"+{roi}" if roi >= 0 else str(roi)
    roi_cls = "pos" if roi >= 0 else "neg"

    rows = ""
    for ds, d in daily.items():
        disp     = f"{ds[:4]}/{ds[4:6]}/{ds[6:]}"
        is_today = (ds == hd)
        today_t  = '<span class="t-today">TODAY</span>' if is_today else ""
        b_cls    = "bal pos" if d['balance'] >= 0 else "bal neg"
        b_str    = f"+{d['balance']:,}" if d['balance'] >= 0 else f"{d['balance']:,}"
        hit_pct  = round(d['hits'] / d['total'] * 100) if d['total'] else 0
        rows    += (f'<tr class="row-link" onclick="location.href=\'/day/{ds}/\'">'
                    f'<td>{disp}{today_t}</td>'
                    f'<td style="text-align:right">{d["total"]}</td>'
                    f'<td style="text-align:right">{d["hits"]} ({hit_pct}%)</td>'
                    f'<td style="text-align:right" class="{b_cls}">{b_str}円</td></tr>')
    if not rows:
        rows = '<tr><td colspan="4" class="empty">バックグラウンドでデータ取得中...<br><small>起動直後は数秒お待ちください</small></td></tr>'

    html = f"""<!DOCTYPE html><html lang="ja"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ボートレース予想</title>
<meta http-equiv="refresh" content="60">
<style>{_CSS_BASE}</style></head><body>
<div class="wrap">
<div class="hdr">
  <div>
    <h1>🏆 ボートレース予想</h1>
    <div style="font-size:.75em;margin-top:4px;opacity:.8">📅 {hd_disp} &nbsp;|&nbsp; {now}</div>
  </div>
  <div class="meta">
    <span class="{bg_cls}"></span>バックグラウンド<br>最終取得: {bg_time}
  </div>
</div>
<div class="stats">
  <div class="sc"><div class="lbl">累計収支</div><div class="v {bal_cls}">{bal_str}円</div></div>
  <div class="sc"><div class="lbl">ROI</div><div class="v {roi_cls}">{roi_str}%</div></div>
  <div class="sc"><div class="lbl">的中率（直近30）</div><div class="v">{agent_stats['hit_rate']}%</div></div>
  <div class="sc"><div class="lbl">学習済みレース</div><div class="v">{w.race_count}</div></div>
</div>
<div class="card">
  <h2>収支推移</h2>
  {chart_svg}
</div>
<div class="card">
  <h2>日別収支 <small style="color:#aaa;font-weight:400">— クリックで詳細</small></h2>
  <table>
    <thead><tr>
      <th>日付</th>
      <th style="text-align:right">レース数</th>
      <th style="text-align:right">的中</th>
      <th style="text-align:right">収支</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
<div style="margin-bottom:16px">
  <button class="btn btn-p" onclick="location.reload()">🔄 更新</button>
  <a class="btn btn-p" href="/api/refresh/" onclick="this.textContent='取得中...';setTimeout(()=>location.reload(),5000);return false;">⚡ 今すぐ取得</a>
  <a class="btn btn-g" href="/admin/">⚙️ 管理</a>
</div>
<div class="footer">データソース: boatrace.jp</div>
</div></body></html>"""
    return HttpResponse(html)


def day_detail(request, hd):
    hd_disp = f"{hd[:4]}/{hd[4:6]}/{hd[6:]}"

    from boat_race.models import RacePrediction
    preds = list(RacePrediction.objects.filter(date=hd).order_by('stadium_code', 'race_no'))

    betted = [p for p in preds if p.hit is not None and p.bet_amount > 0]
    hits   = [p for p in betted if p.hit and p.payout]
    misses = [p for p in betted if not p.hit]
    day_bal = (sum(p.payout * (p.bet_amount // 100) - p.bet_amount for p in hits)
               - sum(p.bet_amount for p in misses))
    hit_pct = round(len(hits) / len(betted) * 100) if betted else 0
    bal_cls = "pos" if day_bal >= 0 else "neg"
    bal_str = f"+{day_bal:,}" if day_bal >= 0 else f"{day_bal:,}"
    skipped = sum(1 for p in preds if p.hit is not None and p.bet_amount == 0)

    # 日内収支チャート
    chart_pts = []
    running   = 0
    rows = ""
    for p in preds:
        conf_pct = round(p.confidence * 100, 1) if p.confidence else 0
        is_done  = p.hit is not None
        bet      = p.bet_amount
        ev_str   = f"{p.ev_at_bet:+.0f}" if p.ev_at_bet is not None else "—"
        if is_done:
            actual_str = f"{p.actual_winner}番" if p.actual_winner else "?"
            if bet == 0:
                hit_cell = '<span class="bu">スキップ</span>'
                bal_cell = '<span style="color:#aaa">EV≤0</span>'
            elif p.hit and p.payout:
                gain = p.payout * (bet // 100) - bet
                running += gain
                chart_pts.append((f"R{p.race_no}", running, gain))
                hit_cell = '<span class="bh">HIT</span>'
                bal_cell = f'<span class="pos fw">+{gain:,}円</span>'
            elif not p.hit:
                running -= bet
                chart_pts.append((f"R{p.race_no}", running, -bet))
                hit_cell = '<span class="bm">MISS</span>'
                bal_cell = f'<span class="neg fw">-{bet:,}円</span>'
            else:
                hit_cell = '<span class="bh">HIT</span>'
                bal_cell = '<span style="color:#aaa">払戻不明</span>'
        else:
            actual_str = "—"
            if bet == 0:
                hit_cell = '<span class="bu">スキップ</span>'
                bal_cell = '<span style="color:#aaa">EV≤0</span>'
            else:
                hit_cell = '<span class="bu">予定</span>'
                bal_cell = f'<span style="color:#aaa">{bet}円</span>'

        rows += (f'<tr><td>{p.stadium_name}</td><td>R{p.race_no}</td>'
                 f'<td>{p.predicted_boat}番 <small style="color:#aaa">({conf_pct}%)</small></td>'
                 f'<td style="color:#888;font-size:.8em">EV:{ev_str}</td>'
                 f'<td>{actual_str}</td><td>{hit_cell}</td><td>{bal_cell}</td></tr>')

    if not rows:
        rows = '<tr><td colspan="6" class="empty">このレース日のデータがありません</td></tr>'

    day_chart = _make_balance_chart(chart_pts)

    extra_css = """
.bh{background:#c6f6d5;color:#22543d;font-size:.75em;padding:2px 8px;border-radius:8px;font-weight:600}
.bm{background:#fed7d7;color:#742a2a;font-size:.75em;padding:2px 8px;border-radius:8px;font-weight:600}
.bu{background:#e2e8f0;color:#4a5568;font-size:.75em;padding:2px 8px;border-radius:8px;font-weight:600}
.pos{color:#38a169}.neg{color:#e53e3e}.fw{font-weight:700}
.stats3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
@media(max-width:600px){td,th{font-size:.78em;padding:8px 5px}}
"""

    html = f"""<!DOCTYPE html><html lang="ja"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{hd_disp} の詳細 | ボートレース予想</title>
<style>{_CSS_BASE}{extra_css}</style></head><body>
<div class="wrap">
<div class="hdr">
  <div>
    <div style="margin-bottom:8px">
      <a href="/" style="color:white;opacity:.8;font-size:.82em;text-decoration:none">← 一覧に戻る</a>
    </div>
    <h1>📅 {hd_disp} のレース詳細</h1>
  </div>
</div>
<div class="stats3">
  <div class="sc"><div class="lbl">日別収支</div><div class="v {bal_cls}">{bal_str}円</div></div>
  <div class="sc"><div class="lbl">ベット/スキップ</div><div class="v">{len(betted)}<small style="color:#aaa;font-size:.6em"> / {skipped}skip</small></div></div>
  <div class="sc"><div class="lbl">的中率</div><div class="v">{hit_pct}%</div></div>
</div>
<div class="card">
  <h2>収支推移</h2>
  {day_chart}
</div>
<div class="card">
  <h2>レース一覧</h2>
  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th>会場</th><th>R</th><th>予想艇</th><th>EV</th><th>結果</th><th>判定</th><th>収支</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  </div>
</div>
<div style="margin-bottom:16px">
  <a class="btn btn-p" href="/">← 一覧に戻る</a>
  <a class="btn btn-p" href="/day/{hd}/report/" style="background:#6b46c1">📊 分析レポート</a>
  <button class="btn btn-g" onclick="location.reload()">🔄 更新</button>
</div>
<div class="footer">データソース: boatrace.jp</div>
</div></body></html>"""
    return HttpResponse(html)


def day_report(request, hd):
    import json as _json
    from boat_race.models import RacePrediction, AgentWeight

    hd_disp = f"{hd[:4]}/{hd[4:6]}/{hd[6:]}"
    preds = list(RacePrediction.objects.filter(date=hd).order_by('stadium_code', 'race_no'))

    if not preds:
        html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>分析レポート {hd_disp}</title>
<style>{_CSS_BASE}</style></head><body>
<div class="wrap">
<div class="hdr"><div>
  <div style="margin-bottom:8px"><a href="/day/{hd}/" style="color:white;opacity:.8;font-size:.82em;text-decoration:none">← 詳細に戻る</a></div>
  <h1>📊 {hd_disp} 分析レポート</h1>
</div></div>
<div class="card"><p class="empty">このレース日のデータがありません</p></div>
</div></body></html>"""
        return HttpResponse(html)

    done    = [p for p in preds if p.hit is not None]
    betted  = [p for p in done  if p.bet_amount > 0]
    skipped = [p for p in done  if p.bet_amount == 0]
    hits    = [p for p in betted if p.hit and p.payout]
    misses  = [p for p in betted if not p.hit]

    day_bal  = (sum(p.payout * (p.bet_amount // 100) - p.bet_amount for p in hits)
                - sum(p.bet_amount for p in misses))
    hit_rate = len(hits) / len(betted) if betted else 0

    # --- EV帯別分析 ---
    ev_groups = [
        ('高 EV≥+50',  lambda p: p.ev_at_bet is not None and p.ev_at_bet >= 50),
        ('中 0≤EV<50', lambda p: p.ev_at_bet is not None and 0 <= p.ev_at_bet < 50),
        ('低 EV<0',    lambda p: p.ev_at_bet is not None and p.ev_at_bet < 0),
        ('EV不明',     lambda p: p.ev_at_bet is None),
    ]
    ev_rows = ""
    for gname, gfn in ev_groups:
        g = [p for p in betted if gfn(p)]
        if not g:
            continue
        g_hits = [p for p in g if p.hit and p.payout]
        g_bal  = (sum(p.payout * (p.bet_amount // 100) - p.bet_amount for p in g_hits)
                  - sum(p.bet_amount for p in g if not p.hit))
        g_hr   = round(len(g_hits) / len(g) * 100)
        bs     = f"+{g_bal:,}" if g_bal >= 0 else f"{g_bal:,}"
        bc     = "pos" if g_bal >= 0 else "neg"
        avg_ev = round(sum(p.ev_at_bet for p in g if p.ev_at_bet is not None) / len(g), 1) if g else 0
        ev_rows += (f'<tr><td>{gname}</td>'
                    f'<td style="text-align:center">{avg_ev:+.1f}</td>'
                    f'<td style="text-align:center">{len(g)}</td>'
                    f'<td style="text-align:center">{len(g_hits)}/{len(g)} ({g_hr}%)</td>'
                    f'<td style="text-align:right" class="{bc}">{bs}円</td></tr>')

    # --- スキップ反実仮想 ---
    skip_hits_l = [p for p in skipped if p.hit and p.payout]
    skip_miss_l = [p for p in skipped if not p.hit]
    skip_hyp    = (sum(p.payout * 3 - 300 for p in skip_hits_l)
                   - 300 * len(skip_miss_l))
    skip_hr     = round(len(skip_hits_l) / len(skipped) * 100) if skipped else 0
    skip_hyp_s  = f"+{skip_hyp:,}" if skip_hyp >= 0 else f"{skip_hyp:,}"
    skip_hyp_c  = "pos" if skip_hyp >= 0 else "neg"

    # --- 艇番分布 ---
    pred_dist   = {i: 0 for i in range(1, 7)}
    actual_dist = {i: 0 for i in range(1, 7)}
    for p in done:
        pred_dist[p.predicted_boat] = pred_dist.get(p.predicted_boat, 0) + 1
        if p.actual_winner:
            actual_dist[p.actual_winner] = actual_dist.get(p.actual_winner, 0) + 1
    total_done = max(len(done), 1)

    # --- フィーチャー重み ---
    w_label_map = [
        ('コース位置', 'w_course'), ('全国勝率', 'w_player_rate'), ('当地勝率', 'w_local_rate'),
        ('モーター',   'w_motor_rate'), ('展示タイム', 'w_tenji_time'), ('展示ST', 'w_tenji_st'),
    ]
    w_vals = {lbl: getattr(preds[0], attr) for lbl, attr in w_label_map}
    wt     = AgentWeight.objects.order_by('-updated_at').first()

    # --- フィーチャー重みSVG ---
    def _weight_svg(wd):
        items  = list(wd.items())
        W, PL  = 460, 100
        bh, gp = 22, 6
        H      = 32 + len(items) * (bh + gp)
        max_v  = max(v for _, v in items) or 1
        s      = f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:{W}px;font-family:sans-serif">'
        for i, (lbl, val) in enumerate(items):
            y  = 10 + i * (bh + gp)
            bw = max(4, int(val / max_v * (W - PL - 65)))
            c  = "#4c51bf" if val == max(wd.values()) else "#7c8dcc"
            s += (f'<text x="{PL-6}" y="{y+bh//2+4}" text-anchor="end" font-size="12" fill="#444">{lbl}</text>'
                  f'<rect x="{PL}" y="{y}" width="{bw}" height="{bh}" fill="{c}" rx="3" opacity=".85"/>'
                  f'<text x="{PL+bw+6}" y="{y+bh//2+4}" font-size="12" fill="#333">{val:.1%}</text>')
        s += '</svg>'
        return s

    # --- 艇番分布SVG ---
    def _boat_svg(pd, ad, tot):
        W, PL  = 460, 40
        bh, gp = 16, 12
        H      = 36 + 6 * (bh * 2 + 4 + gp)
        max_v  = max(max(pd.values()), max(ad.values())) / tot or 1
        s      = (f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:{W}px;font-family:sans-serif">'
                  f'<rect x="{PL}" y="6" width="12" height="10" fill="#4c51bf" rx="2"/>'
                  f'<text x="{PL+16}" y="15" font-size="11" fill="#444">予想</text>'
                  f'<rect x="{PL+60}" y="6" width="12" height="10" fill="#ed8936" rx="2"/>'
                  f'<text x="{PL+76}" y="15" font-size="11" fill="#444">実際の1着</text>')
        for i in range(1, 7):
            y  = 26 + (i - 1) * (bh * 2 + 4 + gp)
            pv = pd.get(i, 0) / tot
            av = ad.get(i, 0) / tot
            pw = max(2, int(pv / max_v * (W - PL - 70)))
            aw = max(2, int(av / max_v * (W - PL - 70)))
            s += (f'<text x="{PL-4}" y="{y+bh//2+4}" text-anchor="end" font-size="12" fill="#444">{i}号</text>'
                  f'<rect x="{PL}" y="{y}" width="{pw}" height="{bh}" fill="#4c51bf" rx="2" opacity=".8"/>'
                  f'<text x="{PL+pw+4}" y="{y+bh//2+4}" font-size="11" fill="#666">{pv:.0%}</text>'
                  f'<rect x="{PL}" y="{y+bh+4}" width="{aw}" height="{bh}" fill="#ed8936" rx="2" opacity=".8"/>'
                  f'<text x="{PL+aw+4}" y="{y+bh+4+bh//2+4}" font-size="11" fill="#666">{av:.0%}</text>')
        s += '</svg>'
        return s

    weight_svg = _weight_svg(w_vals)
    boat_svg   = _boat_svg(pred_dist, actual_dist, total_done)

    # --- 改善提案 ---
    suggestions = []

    if betted:
        if hit_rate < 0.15:
            suggestions.append(('⚠️ EV閾値の引き上げを検討',
                f'ベットレースの的中率 {hit_rate:.0%} が低水準です。EV≥+30〜+50 にフィルタを絞ることで無駄打ちを削減できます。'))
        elif hit_rate >= 0.30:
            suggestions.append(('✅ EV判断は機能しています',
                f'ベットレースの的中率 {hit_rate:.0%} は良好です。現在のEV計算精度を維持してください。'))

    if skipped and betted:
        if len(skipped) > 0 and skip_hr > hit_rate * 100 * 1.3:
            suggestions.append(('⚠️ スキップ過剰の可能性',
                f'スキップレースの予想的中率 {skip_hr}% がベットレース {hit_rate:.0%} を上回っています。'
                'オッズ取得のタイミングが遅くEV計算が保守的すぎる可能性があります。'))
        if skip_hyp > day_bal:
            suggestions.append(('💡 スキップより全賭けが有利な日',
                f'全スキップレースに賭けた仮想収支 {skip_hyp_s}円 > 実際の収支。'
                'オッズ取得精度の改善が収益向上につながる可能性があります。'))

    top_pred = max(pred_dist, key=pred_dist.get)
    top_ratio = pred_dist[top_pred] / total_done
    if top_ratio > 0.45 and top_pred == 1:
        suggestions.append(('⚠️ 1番艇への予想集中',
            f'予想の {top_ratio:.0%} が1番艇に集中しています（コース重み: {w_vals.get("コース位置", 0):.1%}）。'
            'コース位置の重みを下げると分散が改善します。'))

    hi_odds = [p for p in betted if p.bet_odds and p.bet_odds > 500]
    if hi_odds and not any(p.hit for p in hi_odds):
        suggestions.append(('💡 高オッズレース (>500) の的中なし',
            f'{len(hi_odds)}件の高オッズベットが全て外れています。高オッズレースへのベット比率を下げることを検討してください。'))

    if wt and wt.race_count < 50:
        suggestions.append(('📈 学習データが少ない段階',
            f'学習済みレース数: {wt.race_count}件。精度向上には100件以上のデータ蓄積が必要です。'
            'しばらくデータを蓄積し続けてください。'))

    if not suggestions:
        suggestions.append(('✅ 特記事項なし', 'このレース日では大きな問題は検出されませんでした。'))

    sugg_html = "".join(
        f'<div class="si"><div class="st">{t}</div><div class="sb">{b}</div></div>'
        for t, b in suggestions
    )

    bal_str = f"+{day_bal:,}" if day_bal >= 0 else f"{day_bal:,}"
    bal_cls = "pos" if day_bal >= 0 else "neg"

    extra_css = """
.si{padding:12px 14px;border-left:4px solid #4c51bf;margin-bottom:10px;background:#f8f9ff;border-radius:0 8px 8px 0}
.st{font-weight:700;margin-bottom:4px;font-size:.9em}
.sb{font-size:.82em;color:#555;line-height:1.5}
.pos{color:#38a169}.neg{color:#e53e3e}
.stats4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
@media(max-width:600px){.stats4{grid-template-columns:repeat(2,1fr)}}
"""

    html = f"""<!DOCTYPE html><html lang="ja"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{hd_disp} 分析レポート | ボートレース予想</title>
<style>{_CSS_BASE}{extra_css}</style></head><body>
<div class="wrap">
<div class="hdr">
  <div>
    <div style="margin-bottom:8px">
      <a href="/day/{hd}/" style="color:white;opacity:.8;font-size:.82em;text-decoration:none">← 詳細に戻る</a>
    </div>
    <h1>📊 {hd_disp} 分析レポート</h1>
  </div>
</div>
<div class="stats4">
  <div class="sc"><div class="lbl">収支</div><div class="v {bal_cls}">{bal_str}円</div></div>
  <div class="sc"><div class="lbl">ベット</div><div class="v">{len(betted)}<small style="color:#aaa;font-size:.55em"> 件</small></div></div>
  <div class="sc"><div class="lbl">スキップ</div><div class="v">{len(skipped)}<small style="color:#aaa;font-size:.55em"> 件</small></div></div>
  <div class="sc"><div class="lbl">的中率</div><div class="v">{round(hit_rate*100)}%</div></div>
</div>

<div class="card">
  <h2>EV帯別 ベット実績</h2>
  <p style="font-size:.78em;color:#888;margin-bottom:10px">EV（期待値 = 確率×オッズ−100）の高さ別にベット結果を分析します。</p>
  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th>EV帯</th><th style="text-align:center">平均EV</th>
      <th style="text-align:center">件数</th><th style="text-align:center">的中</th>
      <th style="text-align:right">収支</th>
    </tr></thead>
    <tbody>{ev_rows or '<tr><td colspan="5" class="empty">ベットデータなし</td></tr>'}</tbody>
  </table>
  </div>
</div>

<div class="card">
  <h2>スキップレース 反実仮想分析</h2>
  <p style="font-size:.78em;color:#888;margin-bottom:10px">EV≤0でスキップしたレースに仮に300円ずつ賭けていた場合の試算です。</p>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:8px">
    <div class="sc"><div class="lbl">スキップ数</div><div class="v">{len(skipped)}</div></div>
    <div class="sc"><div class="lbl">仮想的中率</div><div class="v">{skip_hr}%</div></div>
    <div class="sc"><div class="lbl">仮想収支</div><div class="v {skip_hyp_c}">{skip_hyp_s}円</div></div>
  </div>
  <p style="font-size:.78em;color:#999">※ 仮想収支は一律300円賭けた場合の試算。実際のオッズが異なる場合あり。</p>
</div>

<div class="card">
  <h2>フィーチャー重み（この日の予想時点）</h2>
  <p style="font-size:.78em;color:#888;margin-bottom:10px">各特徴量が予想スコアに占める割合。学習により毎回更新されます。</p>
  {weight_svg}
</div>

<div class="card">
  <h2>艇番分布（予想 vs 実際の1着）</h2>
  <p style="font-size:.78em;color:#888;margin-bottom:10px">予想艇番の偏りと実際の1着分布を比較します。乖離が大きい艇番が改善余地です。</p>
  {boat_svg}
</div>

<div class="card">
  <h2>改善提案</h2>
  {sugg_html}
</div>

<div style="margin-bottom:16px">
  <a class="btn btn-p" href="/day/{hd}/">← 詳細に戻る</a>
  <a class="btn btn-g" href="/">🏠 一覧</a>
</div>
<div class="footer">データソース: boatrace.jp</div>
</div></body></html>"""
    return HttpResponse(html)


def api_races(request):
    hd = find_race_date()
    stadiums = get_stadiums_for_date(hd)
    all_data = []
    for stadium in stadiums[:4]:
        for rno in range(1, 13):
            boats = get_race_card(stadium["code"], hd, rno)
            if not boats: continue
            sim    = simulate_race(boats)
            result = get_race_result(stadium["code"], hd, rno)
            all_data.append({"stadium": stadium["name"], "race": rno, "boats": boats, "sim": sim, "result": result})
    return JsonResponse({"date": hd, "races": all_data})

def api_refresh(request):
    threading.Thread(target=_force_refresh_all, daemon=True).start()
    return JsonResponse({"status": "refresh started"})

urlpatterns = [
    path("admin/", admin.site.urls),
    path("",                      dashboard,   name="dashboard"),
    path("day/<str:hd>/",         day_detail,  name="day_detail"),
    path("day/<str:hd>/report/",  day_report,  name="day_report"),
    path("api/races/",            api_races,   name="api_races"),
    path("api/refresh/",          api_refresh, name="api_refresh"),
]
