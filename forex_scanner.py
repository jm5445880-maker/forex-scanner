import os
import time
import threading
import datetime as dt
import requests
from flask import Flask

# ─── KEYS (read from Render environment variables) ─────────────────────────
TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── PAIRS: 7 true USD majors ──────────────────────────────────────────────
PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF",
    "AUD/USD", "USD/CAD", "NZD/USD",
]

CHECK_INTERVAL = 3600          # run once per hour
STALE_HOURS    = 3             # ignore data older than this (market closed / frozen feed)

# ─── STATE: remember what we've already alerted, so it never repeats ───────
# key = (pair, level_name, direction) -> the level price we last alerted on
# When a new day/week forms, the level price changes, which re-arms the alert.
alerted = {}

app = Flask(__name__)

@app.route("/")
def home():
    return "Forex level-cross scanner running."

# ─── TELEGRAM ──────────────────────────────────────────────────────────────
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception as e:
        print(f"  Telegram error: {e}")

# ─── MARKET OPEN CHECK (UTC) ───────────────────────────────────────────────
# Forex week: opens ~Sun 22:00 UTC, closes ~Fri 22:00 UTC.
def market_open(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    wd = now.weekday()          # Mon=0 ... Sat=5, Sun=6
    if wd == 5:                 # Saturday: closed all day
        return False
    if wd == 6:                 # Sunday: opens 22:00 UTC
        return now.hour >= 22
    if wd == 4:                 # Friday: closes 22:00 UTC
        return now.hour < 22
    return True                 # Mon–Thu: open

# ─── TWELVE DATA FETCH ─────────────────────────────────────────────────────
def fetch(symbol, interval, outputsize):
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval={interval}&outputsize={outputsize}&apikey={TWELVE_DATA_KEY}")
    r = requests.get(url, timeout=20)
    data = r.json()
    if data.get("status") == "error":
        print(f"  {symbol} {interval}: API error - {data.get('message')}")
        return None
    return data.get("values")   # newest first

def prev_period_levels(symbol, interval):
    # index 0 = current forming period, index 1 = previous COMPLETED period
    vals = fetch(symbol, interval, 3)
    if not vals or len(vals) < 2:
        return None, None
    prev = vals[1]
    return float(prev["high"]), float(prev["low"])

def last_two_closed_1h(symbol):
    vals = fetch(symbol, "1h", 5)
    if not vals or len(vals) < 3:
        return None
    # Drop a still-forming top bar if its hour == current UTC hour
    now = dt.datetime.now(dt.timezone.utc)
    top_time = vals[0]["datetime"]          # "YYYY-MM-DD HH:MM:SS"
    forming = top_time[:13] == now.strftime("%Y-%m-%d %H")
    base = 1 if forming else 0
    last = vals[base]
    prev = vals[base + 1]
    # Stale guard: last closed candle must be recent
    t = dt.datetime.strptime(last["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    if (now - t).total_seconds() > STALE_HOURS * 3600:
        return None
    return float(prev["close"]), float(last["close"]), float(last["high"]), float(last["low"])

# ─── CROSS LOGIC ───────────────────────────────────────────────────────────
def check_cross(prev_close, last_close, level):
    # Did the close move through the level between the last two closed bars?
    if prev_close < level <= last_close:
        return "up"
    if prev_close > level >= last_close:
        return "down"
    return None

def fmt(symbol, price):
    digits = 3 if "JPY" in symbol else 5
    return f"{price:.{digits}f}"

def scan_pair(pair):
    name = pair.replace("/", "")
    try:
        c = last_two_closed_1h(pair)
        if not c:
            print(f"  {name}: no fresh candle")
            return
        prev_close, last_close, _, _ = c

        pdh, pdl = prev_period_levels(pair, "1day")
        pwh, pwl = prev_period_levels(pair, "1week")

        levels = {
            "Previous Day High":  pdh,
            "Previous Day Low":   pdl,
            "Previous Week High": pwh,
            "Previous Week Low":  pwl,
        }

        for lname, lvl in levels.items():
            if lvl is None:
                continue
            direction = check_cross(prev_close, last_close, lvl)
            if not direction:
                continue
            key = (pair, lname, direction)
            if alerted.get(key) == lvl:      # already alerted this exact level/direction
                continue
            alerted[key] = lvl
            arrow = "🟢⬆️" if direction == "up" else "🔴⬇️"
            word  = "above" if direction == "up" else "below"
            send_telegram(
                f"{arrow} <b>{name} crossed {word} {lname}</b>\n"
                f"Level {fmt(pair, lvl)} | Close {fmt(pair, last_close)}"
            )
            print(f"  {name}: ALERT {lname} {direction}")
    except Exception as e:
        print(f"  {name}: error {e}")

# ─── MAIN LOOP ─────────────────────────────────────────────────────────────
def scanner_loop():
    send_telegram("✅ <b>Level-cross scanner is live.</b>\n"
                  "Alerts only when a 1H candle closes through a Prev Day / Week High or Low.")
    while True:
        if market_open():
            print("Scan start", dt.datetime.now(dt.timezone.utc))
            for p in PAIRS:
                scan_pair(p)
                time.sleep(20)          # stay under Twelve Data 8 req/min
        else:
            print("Market closed — skipping scan.")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=scanner_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
