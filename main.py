import sqlite3
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI()
DB_PATH = "funding_history.db"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            coin TEXT,
            oi_usd REAL,
            spread_apr REAL,
            long_venue TEXT,
            long_apr REAL,
            short_venue TEXT,
            short_apr REAL
        )
    ''')
    conn.commit()
    conn.close()

def clean_symbol(sym):
    s = sym.upper()
    for suffix in ["-PERP", "_PERP", "-USD", "_USD", "-USDT", "_USDT", "USDT", "USDC", "-USDC", "_USDC"]:
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    if s.startswith("1000"):
        s = s[4:]
    return s

# 1. BYBIT (8h Standard -> * 3 * 365)
def fetch_bybit():
    data = {}
    try:
        r = requests.get("https://api.bybit.com/v5/market/tickers?category=linear", headers=HEADERS, timeout=12).json()
        for item in r.get("result", {}).get("list", []):
            sym = item.get("symbol", "")
            if sym.endswith("USDT"):
                coin = clean_symbol(sym)
                fr = float(item.get("fundingRate") or 0)
                oi = float(item.get("openInterestValue") or 0)
                data[coin] = {"apr": fr * 3 * 365 * 100, "oi": oi}
    except Exception as e:
        print("Bybit Error:", e)
    return data

# 2. HYPERLIQUID (1h -> * 24 * 365)
def fetch_hyperliquid():
    data = {}
    try:
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, headers=HEADERS, timeout=12).json()
        for meta, ctx in zip(r[0]["universe"], r[1]):
            coin = clean_symbol(meta["name"])
            fr_1h = float(ctx.get("funding") or 0)
            oi_coins = float(ctx.get("openInterest") or 0)
            mark_px = float(ctx.get("markPx") or 0)
            data[coin] = {"apr": fr_1h * 24 * 365 * 100, "oi": oi_coins * mark_px}
    except Exception as e:
        print("Hyperliquid Error:", e)
    return data

# 3. BITGET (8h Standard -> * 3 * 365)
def fetch_bitget():
    data = {}
    try:
        r = requests.get("https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES", headers=HEADERS, timeout=12).json()
        for item in r.get("data", []):
            sym = item.get("symbol", "")
            if sym.endswith("USDT"):
                coin = clean_symbol(sym)
                fr = float(item.get("fundingRate") or 0)
                oi_coins = float(item.get("holdingAmount") or 0)
                price = float(item.get("lastPr") or 0)
                data[coin] = {"apr": fr * 3 * 365 * 100, "oi": oi_coins * price}
    except Exception as e:
        print("Bitget Error:", e)
    return data

# 4. XT.COM (8h Standard -> * 3 * 365)
def fetch_xt():
    data = {}
    try:
        r = requests.get("https://fapi.xt.com/future/market/v1/public/cg/contracts", headers=HEADERS, timeout=12).json()
        items = r if isinstance(r, list) else r.get("result", [])
        for item in items:
            sym = item.get("symbol", "")
            if "usdt" in sym.lower():
                coin = clean_symbol(sym)
                fr = float(item.get("funding_rate") or 0)
                oi_contracts = float(item.get("open_interest") or 0)
                c_size = float(item.get("contractSize") or 1)
                price = float(item.get("last_price") or item.get("index_price") or 0)
                data[coin] = {"apr": fr * 3 * 365 * 100, "oi": oi_contracts * c_size * price}
    except Exception as e:
        print("XT Error:", e)
    return data

# 5. ASTER (8h/4h dynamisch über fundingInfo -> fallback 8h)
def fetch_aster():
    data = {}
    try:
        intervals = {}
        try:
            fi = requests.get("https://fapi.asterdex.com/fapi/v1/fundingInfo", headers=HEADERS, timeout=10).json()
            for x in fi:
                intervals[x.get("symbol")] = int(x.get("fundingIntervalHours", 8))
        except Exception:
            pass
        r = requests.get("https://fapi.asterdex.com/fapi/v1/premiumIndex", headers=HEADERS, timeout=12).json()
        for item in r:
            sym = item.get("symbol", "")
            if sym.endswith("USDT"):
                coin = clean_symbol(sym)
                fr = float(item.get("lastFundingRate") or 0)
                hrs = intervals.get(sym, 8)
                apr = fr * (24 / hrs) * 365 * 100
                data[coin] = {"apr": apr, "oi": 0}
    except Exception as e:
        print("Aster Error:", e)
    return data

# 6. LIGHTER (1h -> * 24 * 365)
def fetch_lighter():
    data = {}
    try:
        r = requests.get("https://mainnet.zklighter.elliot.ai/api/v1/funding-rates", headers=HEADERS, timeout=12).json()
        rates_list = r.get("funding_rates", []) if isinstance(r, dict) else r
        for item in rates_list:
            if item.get("exchange", "lighter").lower() == "lighter":
                coin = clean_symbol(item.get("symbol", ""))
                fr_1h = float(item.get("rate") or 0)
                data[coin] = {"apr": fr_1h * 24 * 365 * 100, "oi": 0}
    except Exception as e:
        print("Lighter Error:", e)
    return data

# 7. EXTENDED (1h -> * 24 * 365)
def fetch_extended():
    data = {}
    try:
        r = requests.get("https://api.starknet.extended.exchange/api/v1/info/markets", headers=HEADERS, timeout=12).json()
        markets = r.get("data", []) if isinstance(r, dict) else r
        for m in markets:
            stats = m.get("marketStats", {})
            coin = clean_symbol(m.get("assetName") or m.get("name", ""))
            fr_1h = float(stats.get("fundingRate") or 0)
            oi = float(stats.get("openInterest") or 0)
            data[coin] = {"apr": fr_1h * 24 * 365 * 100, "oi": oi}
    except Exception as e:
        print("Extended Error:", e)
    return data

# 8. VARIATIONAL (liefert annualized_rate bereits direkt als Dezimalzahl)
def fetch_variational():
    data = {}
    try:
        r = requests.get("https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats", headers=HEADERS, timeout=12).json()
        for item in r.get("listings", []):
            if item.get("contract_type") == "perpetual":
                coin = clean_symbol(item.get("ticker", ""))
                funding = item.get("funding") or {}
                last = funding.get("last") or {}
                ann_rate = float(last.get("annualized_rate") or 0)
                oi_obj = item.get("open_interest") or {}
                oi = float(oi_obj.get("long_open_interest") or 0) + float(oi_obj.get("short_open_interest") or 0)
                data[coin] = {"apr": ann_rate * 100, "oi": oi}
    except Exception as e:
        print("Variational Error:", e)
    return data

def update_all_data():
    print(f"[{datetime.now()}] Starte 8-Börsen-Scan...")
    fetchers = {
        "Bybit": fetch_bybit,
        "Hyperliquid": fetch_hyperliquid,
        "Bitget": fetch_bitget,
        "XT.com": fetch_xt,
        "Aster": fetch_aster,
        "Lighter": fetch_lighter,
        "Extended": fetch_extended,
        "Variational": fetch_variational
    }

    venues = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {name: executor.submit(fn) for name, fn in fetchers.items()}
        for name, fut in futures.items():
            venues[name] = fut.result()
            print(f"  -> {name}: {len(venues[name])} Coins geladen")

    all_coins = set()
    for v_data in venues.values():
        all_coins.update(v_data.keys())

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM snapshots WHERE timestamp = ?", (now_str,))
    for coin in all_coins:
        if not coin:
            continue
        rates = {}
        total_oi = 0
        for v_name, v_data in venues.items():
            if coin in v_data:
                rates[v_name] = v_data[coin]["apr"]
                total_oi += v_data[coin]["oi"]

        # Nur Coins mit mindestens 2 Börsen und > 2 Mio. USD Open Interest
        # Nur Coins mit mindestens 2 Börsen
        if len(rates) >= 2:
            short_venue = max(rates, key=rates.get)
            long_venue = min(rates, key=rates.get)
            spread = rates[short_venue] - rates[long_venue]

            long_oi = ois.get(long_venue, 0.0)
            short_oi = ois.get(short_venue, 0.0)
            pair_oi = long_oi + short_oi

            if pair_oi > 2_000_000:
                c.execute('''
                    INSERT INTO snapshots (timestamp, coin, oi_usd, spread_apr, long_venue, long_apr, short_venue, short_apr)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (now_str, coin, pair_oi, spread, long_venue, rates[long_venue], short_venue, rates[short_venue]))


    conn.commit()
    conn.close()
    print("8-Börsen-Scan abgeschlossen.")

def get_latest_analyzed():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT timestamp FROM snapshots ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    if not row:
        conn.close()
        return [], "Noch keine Daten"

    latest_ts = row[0]
    c.execute("SELECT * FROM snapshots WHERE timestamp = ? ORDER BY spread_apr DESC LIMIT 200", (latest_ts,))
    latest_rows = c.fetchall()

    results = []
    seen_coins = set()
    for r in latest_rows:
        _, ts, coin, oi, spread, l_ven, l_apr, s_ven, s_apr = r
        if coin in seen_coins:
            continue
        seen_coins.add(coin)

        c.execute("SELECT long_venue, short_venue, spread_apr FROM snapshots WHERE coin = ? ORDER BY id DESC LIMIT 6", (coin,))
        hist = c.fetchall()

        flips = sum(1 for h_l, h_s, _ in hist if h_l != l_ven or h_s != s_ven)

        if len(hist) < 2:
            status = "NEU (Beobachten)"
        elif len(hist) < 6:
            status = f"AUFBAU ({len(hist)}/6 Scans)"
        elif flips == 0 and spread > 25:
            status = "STABIL"
        else:
            status = f"UNRUHIG ({flips}x Wechsel)"

        results.append({
            "coin": coin,
            "oi": oi,
            "spread": spread,
            "long_venue": l_ven,
            "long_apr": l_apr,
            "short_venue": s_ven,
            "short_apr": s_apr,
            "status": status
        })
        if len(results) >= 100:
            break

    conn.close()
    return results, latest_ts

@app.on_event("startup")
def startup_event():
    init_db()
    update_all_data()
    scheduler = BackgroundScheduler()
    scheduler.add_job(update_all_data, 'interval', hours=4)
    scheduler.start()

@app.get("/", response_class=HTMLResponse)
def read_root():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/data")
def api_data():
    rows, ts = get_latest_analyzed()
    return {"timestamp": ts, "data": rows}

@app.post("/api/refresh")
def manual_refresh():
    update_all_data()
    return {"status": "ok"}
