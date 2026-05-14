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
CACHE_TTL = 300  # 5分
_last_refresh_time = None
_bg_thread = None
REFRESH_INTERVAL = 290  # TTLより少し短く設定し常に新鮮なキャッシュを維持

def _cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() - entry[1] < CACHE_TTL:
        return entry[0]
    return None

def _cache_set(key, val):
    _cache[key] = (val, time.time())

def _force_refresh_all():
    """キャッシュを無視して全データを再取得し直す"""
    global _last_refresh_time
    _cache.pop("race_date", None)
    hd = find_race_date()
    _cache.pop(f"stadiums_{hd}", None)
    stadiums = get_stadiums_for_date(hd)
    tasks = [(s, r) for s in stadiums[:6] for r in range(1, 13)]
    for s, r in tasks:
        _cache.pop(f"card_{s['code']}_{hd}_{r}", None)
        _cache.pop(f"result_{s['code']}_{hd}_{r}", None)
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(get_race_card, s["code"], hd, r) for s, r in tasks]
        futures += [executor.submit(get_race_result, s["code"], hd, r) for s, r in tasks]
        for f in as_completed(futures):
            pass
    _last_refresh_time = datetime.now(JST)

def _background_worker():
    time.sleep(3)  # サーバー起動直後の余裕を持たせる
    while True:
        try:
            _force_refresh_all()
        except Exception:
            pass
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
            win_rate = nums[0] if nums else "3.00"
            boats.append({"number": i, "name": name, "win_rate": win_rate})
        _cache_set(cache_key, boats)
        return boats
    except:
        return []

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
        rows = soup.select("tbody.is-fs14 tr, .is-w495 tbody tr")
        for row in rows[:6]:
            tds = row.select("td")
            if len(tds) >= 3:
                rank = tds[0].get_text(strip=True)
                boat_num = tds[1].get_text(strip=True)
                name = tds[2].get_text(strip=True)
                results.append({"rank": rank, "boat": boat_num, "name": name})
        _cache_set(cache_key, results)
        return results
    except:
        return []

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

def dashboard(request):
    hd = find_race_date()
    hd_display = f"{hd[:4]}/{hd[4:6]}/{hd[6:]}"
    stadiums = get_stadiums_for_date(hd)
    now = datetime.now(JST).strftime("%H:%M:%S")
    bg_status = f"最終バックグラウンド取得: {_last_refresh_time.strftime('%H:%M:%S')}" if _last_refresh_time else "バックグラウンド取得: 準備中..."
    bg_active = _bg_thread is not None and _bg_thread.is_alive()
    max_stadiums = min(len(stadiums), 6)
    tasks = [(stadium, rno) for stadium in stadiums[:max_stadiums] for rno in range(1, 13)]

    def fetch_race(stadium, rno):
        boats = get_race_card(stadium["code"], hd, rno)
        if not boats:
            return None
        result = get_race_result(stadium["code"], hd, rno)
        sim = simulate_race(boats)
        best = max(sim, key=sim.get) if sim else 1
        best_rate = sim.get(best, 0)
        ev = round((best_rate/100)*3.5-(1-best_rate/100), 2)
        hit = ""
        if result:
            try:
                hit = "HIT" if int(result[0].get("boat","")) == best else "MISS"
            except:
                pass
        return {
            "stadium": stadium["name"], "stadium_code": stadium["code"],
            "rno": rno, "boats": boats, "sim": sim, "best": best,
            "best_rate": best_rate, "ev": ev, "result": result, "hit": hit
        }

    raw_results = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_map = {executor.submit(fetch_race, s, r): (s["code"], r) for s, r in tasks}
        for future in as_completed(future_map):
            key = future_map[future]
            data = future.result()
            if data:
                raw_results[key] = data

    all_races = [raw_results[k] for k in sorted(raw_results, key=lambda x: (x[0], x[1]))]

    total_races = len(all_races)
    high_count = sum(1 for r in all_races if r["best_rate"]>35)
    buy_count = sum(1 for r in all_races if r["ev"]>0.1)
    hit_count = sum(1 for r in all_races if r["hit"]=="HIT")
    miss_count = sum(1 for r in all_races if r["hit"]=="MISS")
    acc = round(hit_count/(hit_count+miss_count)*100,1) if (hit_count+miss_count)>0 else 0

    race_htmls = []
    for rc in all_races:
        conf = "HIGH" if rc["best_rate"]>35 else "MEDIUM" if rc["best_rate"]>25 else "LOW"
        rec = "BUY" if rc["ev"]>0.1 else "HOLD" if rc["ev"]>-0.1 else "SKIP"
        bars = ""
        for b in rc["boats"]:
            rate = rc["sim"].get(b["number"],0)
            bars += f'<div class="bc"><div class="bl">艇{b["number"]}</div><div class="br"><div class="bf" style="width:{rate}%"></div></div><div class="bv">{rate}%</div></div>'
        names = " / ".join([b["name"] for b in rc["boats"]])
        result_html = ""
        if rc["result"]:
            top3 = " → ".join([f'{r["rank"]}着:{r["name"]}' for r in rc["result"][:3]])
            result_html = f'<div class="result-box"><span class="result-label">結果:</span> {top3}</div>'
        hit_html = ""
        if rc["hit"]=="HIT":
            hit_html = '<span class="badge hit">🎯 的中!</span>'
        elif rc["hit"]=="MISS":
            hit_html = '<span class="badge miss">✗ 不的中</span>'
        race_htmls.append(f'''<div class="race-item"><div style="flex:1">
<div class="race-name">{rc["stadium"]} R{rc["rno"]} <span class="badge {conf.lower()}">{conf}</span> {hit_html}</div>
<div class="race-detail">{names}</div>
<div class="race-detail">推奨艇: {rc["best"]}番 ｜ 期待値: {rc["ev"]} ｜ {rec}</div>
<div style="margin-top:6px;max-width:350px">{bars}</div>
{result_html}
</div></div>''')

    if not race_htmls:
        race_htmls.append('<p style="color:#999;text-align:center;padding:40px">レースデータが見つかりませんでした</p>')

    html = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>ボートレース予想ダッシュボード</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,sans-serif;background:linear-gradient(135deg,#667eea,#764ba2);min-height:100vh}}
.container{{max-width:1200px;margin:0 auto;padding:20px}}
.header{{background:white;padding:25px;border-radius:10px;text-align:center;margin-bottom:20px;box-shadow:0 4px 6px rgba(0,0,0,.1)}}
.header h1{{color:#667eea;font-size:1.8em}}.header p{{color:#666;margin-top:6px}}
.live{{display:inline-block;background:#e53e3e;color:white;padding:2px 8px;border-radius:8px;font-size:.7em;animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.5}}}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}}
.sc{{background:white;padding:15px;border-radius:10px;border-left:4px solid #667eea;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
.sc h3{{color:#666;font-size:.75em;text-transform:uppercase;margin-bottom:5px}}
.sc .v{{font-size:1.8em;font-weight:bold;color:#667eea}}
.sc.g{{border-left-color:#48bb78}}.sc.g .v{{color:#48bb78}}
.sc.o{{border-left-color:#f6ad55}}.sc.o .v{{color:#f6ad55}}
.sc.r{{border-left-color:#e53e3e}}.sc.r .v{{color:#e53e3e}}
.section{{background:white;padding:20px;border-radius:10px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,.1)}}
.section h2{{color:#667eea;margin-bottom:12px;border-bottom:2px solid #667eea;padding-bottom:6px;font-size:1.2em}}
.race-item{{border:1px solid #e2e8f0;padding:12px;margin-bottom:8px;border-radius:8px}}
.race-item:hover{{border-color:#667eea;box-shadow:0 2px 8px rgba(102,126,234,.15)}}
.race-name{{font-weight:bold;font-size:1em;margin-bottom:4px}}
.race-detail{{color:#666;font-size:.8em;margin-top:2px}}
.badge{{padding:3px 8px;border-radius:10px;font-weight:bold;font-size:.75em;margin-left:5px}}
.badge.high{{background:#c6f6d5;color:#22543d}}.badge.medium{{background:#feebc8;color:#7c2d12}}.badge.low{{background:#e2e8f0;color:#2d3748}}
.badge.hit{{background:#c6f6d5;color:#22543d}}.badge.miss{{background:#fed7d7;color:#742a2a}}
.bc{{display:flex;align-items:center;margin:2px 0}}.bl{{width:30px;font-size:.75em;color:#666}}
.br{{height:8px;background:#e2e8f0;border-radius:4px;flex:1;margin:0 5px;overflow:hidden}}
.bf{{height:100%;background:linear-gradient(90deg,#667eea,#764ba2);border-radius:4px}}
.bv{{width:38px;text-align:right;font-size:.75em;font-weight:bold;color:#667eea}}
.result-box{{margin-top:8px;padding:6px 10px;background:#f7fafc;border-radius:6px;border-left:3px solid #667eea;font-size:.8em;color:#333}}
.result-label{{font-weight:bold;color:#667eea}}
.btn{{padding:8px 16px;border:none;border-radius:5px;cursor:pointer;font-weight:bold;margin-right:6px;font-size:.85em}}
.btn-p{{background:#667eea;color:white}}.btn-p:hover{{background:#764ba2}}
.btn-s{{background:#e2e8f0;color:#333}}
.bg-status{{display:inline-flex;align-items:center;gap:6px;font-size:.78em;color:#555;margin-top:6px}}
.bg-dot{{width:8px;height:8px;border-radius:50%;background:#48bb78;animation:pulse 2s infinite}}
.bg-dot.inactive{{background:#cbd5e0;animation:none}}
.footer{{text-align:center;color:white;padding:15px;font-size:.8em}}
</style></head><body>
<div class="container">
<div class="header">
<h1>🏆 ボートレース予想ダッシュボード <span class="live">LIVE</span></h1>
<p>📅 {hd_display} ｜ 表示時刻: {now} ｜ データソース: boatrace.jp</p>
<div class="bg-status"><span class="bg-dot{"" if bg_active else " inactive"}"></span>{bg_status}</div>
</div>
<div class="stats">
<div class="sc"><h3>🏁 取得レース</h3><div class="v">{total_races}</div></div>
<div class="sc"><h3>📍 開催場数</h3><div class="v">{len(stadiums)}</div></div>
<div class="sc g"><h3>⭐ 高信頼度</h3><div class="v">{high_count}</div></div>
<div class="sc o"><h3>💰 推奨BUY</h3><div class="v">{buy_count}</div></div>
<div class="sc g"><h3>🎯 的中数</h3><div class="v">{hit_count}</div></div>
<div class="sc r"><h3>📊 的中率</h3><div class="v">{acc}%</div></div>
</div>
<div class="section">
<h2>📋 レース予想 & 結果（実データ）</h2>
{"".join(race_htmls)}
</div>
<div class="section">
<h2>⚙️ 操作</h2>
<button class="btn btn-p" onclick="location.reload()">🔄 表示更新</button>
<a href="/api/refresh/" class="btn btn-p" onclick="this.textContent='更新中...';setTimeout(()=>location.href='/',3000);return false;">⚡ 今すぐ取得</a>
<a href="/admin/" class="btn btn-s">⚙️ 管理画面</a>
<a href="/api/races/" class="btn btn-s">📊 API (JSON)</a>
</div>
<div class="footer">© 2026 ボートレース予想システム ｜ データソース: boatrace.jp</div>
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
            sim = simulate_race(boats)
            result = get_race_result(stadium["code"], hd, rno)
            all_data.append({"stadium":stadium["name"],"race":rno,"boats":boats,"sim":sim,"result":result})
    return JsonResponse({"date":hd,"races":all_data})

def api_refresh(request):
    threading.Thread(target=_force_refresh_all, daemon=True).start()
    return JsonResponse({"status": "refresh started"})

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", dashboard, name="dashboard"),
    path("api/races/", api_races, name="api_races"),
    path("api/refresh/", api_refresh, name="api_refresh"),
]
