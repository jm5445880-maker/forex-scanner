import os
import json
import time
import threading
import datetime as dt
from zoneinfo import ZoneInfo
import requests
from flask import Flask

# ─── KEYS (Render environment variables) ───────────────────────────────────
TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
UPSTASH_URL      = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN    = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_KEY        = "forex_line_counts"

# ─── PAIRS & WINDOW ────────────────────────────────────────────────────────
PAIRS        = ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD"]
DAILY_LINES  = 5      # line 1 = yesterday ... line 5 = 5 days back
WEEKLY_LINES = 2      # line 1 = last week, line 2 = the week before

# ─── SETTINGS ──────────────────────────────────────────────────────────────
CHECK_MINUTE   = 5
LEVEL_TIMEZONE = "America/New_York"
MAX_ALERTS     = 2
PRUNE_DAYS     = 60         # forget stored counts older than this (housekeeping)

# ─── STATE ─────────────────────────────────────────────────────────────────
levels_cache = {}     # pair -> [ {key, price, line, type, date} ]
levels_day   = None
counts       = {}     # key -> alerts used (PERSISTENT)

app = Flask(__name__)

@app.route("/")
def home():
    return "Forex line scanner running."

# ─── PERSISTENCE (Upstash REST, in-memory fallback) ────────────────────────
def redis_cmd(*args):
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return None
    try:
        r = requests.post(UPSTASH_URL, json=list(args),
                          headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=10)
        return r.json().get("result")
    except Exception as e:
        print(f"  Redis error: {e}")
        return None

def load_counts():
    global counts
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        print("  No Upstash set — caps work but reset on restart.")
        counts = {}; return
    raw = redis_cmd("GET", REDIS_KEY)
    try:
        counts = json.loads(raw) if raw else {}
        print(f"  Loaded {len(counts)} saved line-counts.")
    except Exception:
        counts = {}

def save_counts():
    if UPSTASH_URL and UPSTASH_TOKEN:
        redis_cmd("SET", REDIS_KEY, json.dumps(counts))

def prune_counts():
    cutoff = (dt.date.today() - dt.timedelta(days=PRUNE_DAYS)).isoformat()
    for k in list(counts.keys()):
        parts = k.split("|")
        if len(parts) >= 3 and parts[2] < cutoff:
            del counts[k]

# ─── TELEGRAM ──────────────────────────────────────────────────────────────
def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                      timeout=10)
    except Exception as e:
        print(f"  Telegram error: {e}")

# ─── MARKET OPEN (UTC) ─────────────────────────────────────────────────────
def market_open(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    wd = now.weekday()
    if wd == 5: return False
    if wd == 6: return now.hour >= 22
    if wd == 4: return now.hour < 22
    return True

# ─── TWELVE DATA ───────────────────────────────────────────────────────────
def fetch_series(pair, interval, n, tz):
    url = (f"https://api.twelvedata.com/time_series?symbol={pair}"
           f"&interval={interval}&outputsize={n}&timezone={tz}&apikey={TWELVE_DATA_KEY}")
    try:
        return requests.get(url, timeout=20).json().get("values")
    except Exception as e:
        print(f"  {pair} {interval} error: {e}")
        return None

def build_levels():
    global levels_cache, levels_day
    cache = {}
    for p in PAIRS:
        daily  = fetch_series(p, "1day",  DAILY_LINES + 1,  LEVEL_TIMEZONE); time.sleep(5)
        weekly = fetch_series(p, "1week", WEEKLY_LINES + 1, LEVEL_TIMEZONE); time.sleep(5)
        lst = []
        if daily and len(daily) >= DAILY_LINES + 1:
            for line in range(1, DAILY_LINES + 1):
                b = daily[line]; d = b["datetime"][:10]
                lst.append({"key": f"{p}|D|{d}|H", "price": float(b["high"]),
                            "line": line, "type": "Daily High", "date": d})
                lst.append({"key": f"{p}|D|{d}|L", "price": float(b["low"]),
                            "line": line, "type": "Daily Low",  "date": d})
        if weekly and len(weekly) >= WEEKLY_LINES + 1:
            for line in range(1, WEEKLY_LINES + 1):
                b = weekly[line]; d = b["datetime"][:10]
                lst.append({"key": f"{p}|W|{d}|H", "price": float(b["high"]),
                            "line": line, "type": "Weekly High", "date": d})
                lst.append({"key": f"{p}|W|{d}|L", "price": float(b["low"]),
                            "line": line, "type": "Weekly Low",  "date": d})
        cache[p] = lst
    levels_cache = cache
    prune_counts(); save_counts()
    levels_day = dt.datetime.now(ZoneInfo(LEVEL_TIMEZONE)).date()
    print("Levels rebuilt for", levels_day)

def last_closed_candle(pair):
    v = fetch_series(pair, "1h", 4, "UTC")
    if not v or len(v) < 2: return None
    now = dt.datetime.now(dt.timezone.utc)
    forming = v[0]["datetime"][:13] == now.strftime("%Y-%m-%d %H")
    c = v[1] if forming else v[0]
    return float(c["high"]), float(c["low"])

def fmt(symbol, price):
    return f"{price:.{3 if 'JPY' in symbol else 5}f}"

def dmy(iso):
    # "2026-06-27" -> "27-06-2026"
    y, m, d = iso.split("-")
    return f"{d}-{m}-{y}"

# ─── TOUCH CHECK ───────────────────────────────────────────────────────────
def check_pair(pair, c_high, c_low):
    for lv in levels_cache.get(pair, []):
        key, price = lv["key"], lv["price"]
        used = counts.get(key, 0)
        if used >= MAX_ALERTS:
            continue
        if c_low <= price <= c_high:
            counts[key] = used + 1
            save_counts()
            arrow = "🟢⬆️" if "High" in lv["type"] else "🔴⬇️"
            send_telegram(
                f"{arrow} <b>{pair.replace('/','')} {lv['type']} · line {lv['line']}</b>\n"
                f"Price {fmt(pair, price)} · {dmy(lv['date'])}"
            )
            print(f"  ALERT {key} line{lv['line']} ({used+1}/{MAX_ALERTS})")

# ─── HOURLY PASS ───────────────────────────────────────────────────────────
def run_check():
    if not market_open():
        print("Market closed — skipping."); return
    today = dt.datetime.now(ZoneInfo(LEVEL_TIMEZONE)).date()
    if today != levels_day or not levels_cache:
        build_levels()
    for p in PAIRS:
        c = last_closed_candle(p)
        if c: check_pair(p, c[0], c[1])
        time.sleep(3)

# ─── SCHEDULER ─────────────────────────────────────────────────────────────
def seconds_to_next_check():
    now = dt.datetime.now(dt.timezone.utc)
    target = now.replace(minute=CHECK_MINUTE, second=0, microsecond=0)
    if now >= target: target += dt.timedelta(hours=1)
    return (target - now).total_seconds()

def loop():
    load_counts()
    send_telegram(f"✅ <b>Scanner live.</b> Tracking last {DAILY_LINES} daily + "
                  f"{WEEKLY_LINES} weekly lines per pair, max {MAX_ALERTS} alerts each, "
                  "labelled by line number.")
    while True:
        time.sleep(seconds_to_next_check())
        try:
            print("Check at", dt.datetime.now(dt.timezone.utc))
            run_check()
        except Exception as e:
            print(f"  check error: {e}")

if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
