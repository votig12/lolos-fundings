import os
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

# 6. LIGHTER (rate als 8h-Prozentwert -> * 3 * 365 für einfache APR in %)
def fetch_lighter():
    data = {}
    try:
        r = requests.get("https://mainnet.zklighter.elliot.ai/api/v1/funding-rates", headers=HEADERS, timeout=12).json()
        rates_list = r.get("funding_rates", []) if isinstance(r, dict) else r
        for item in rates_list:
            if item.get("exchange", "lighter").lower() == "lighter":
                coin = clean_symbol(item.get("symbol", ""))
                rate_8h_pct = float(item.get("rate") or 0)
                data[coin] = {"apr": rate_8h_pct * 3 * 365, "oi": 0}
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

# COINMARKETCAP MARKET CAPS (USD)
def fetch_cmc_marketcaps(limit=500):
    mcaps = {}
    best_rank = {}
    api_key = os.getenv("CMC_API_KEY", "").strip()

    try:
        if api_key:
            url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
            headers = {**HEADERS, "X-CMC_PRO_API_KEY": api_key}
        else:
            url = "https://pro-api.coinmarketcap.com/public-api/v1/cryptocurrency/listings/latest"
            headers = HEADERS

        r = requests.get(
            url,
            params={"start": 1, "limit": limit, "convert": "USD", "sort": "market_cap"},
            headers=headers,
            timeout=15
        ).json()

        for item in r.get("data", []):
            sym = clean_symbol(item.get("symbol", ""))
            if not sym:
                continue
            rank = int(item.get("cmc_rank") or 999999)
            quote_usd = (item.get("quote") or {}).get("USD") or {}
            mcap = float(quote_usd.get("market_cap") or 0)

            if sym not in
