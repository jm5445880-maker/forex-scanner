import os
import time
import threading
import datetime as dt
from zoneinfo import ZoneInfo
import requests
from flask import Flask

# ─── KEYS (from Render environment variables) ──────────────────────────────
TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── PAIRS: 7 true USD majors ──────────────────────────────────────────────
PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD"]

# ─── SCHEDULE ──────────────────────────────────────────────────────────────
CHECK_MINUTE   = 5                    # run at HH:05 every hour
LEVEL_TIMEZONE = "America/New_York"   # FX daily/weekly rollover; matches most charts
REARM_PCT      = 0.0005               # level must be cleared by 0.05% before it re-alerts

# ─── STATE ─────────────────────────────────────────────────────────────────
levels_cache = {}     # pair -> {level_name: value}
levels_day   = None   # NY date the levels were built for
armed        = {}     # (pair, level_name) -> True/False  (False = already alerted)
armed_value  = {}     # (pair, level_name) -> level value armed against

app = Flask(__name__)

@app.route("/")
def home():
    return "Forex hourly level-touch scanner running."

# ─── TELEGRAM ──────────────────────────────────────────────────────────────
def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10)
    except Exception as e:
        print(f"  Telegram error: {e}")

# ─── MARKET OPEN (UTC) ─────────────────────────────────────────────────────
def market_open(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    wd = now.weekday()                      # Mon=0 .. Sat=5, Sun=6
    if wd == 5:
        return False
    if wd == 6:
        return now.hour >= 22
    if wd == 4:
        return now.hour < 22
    return True

# ─── TWELVE DATA ───────────────────────────────────────────────────────────
def prev_levels(pair, interval):
    url = (f"https://api.twelvedata.com/time_series?symbol={pair}"
           f"&interval={interval}&outputsize=3&timezone={LEVEL_TIMEZONE}"
           f"&apikey={TWELVE_DATA_KEY}")
    try:
        v = requests.get(url, timeout=20).json().get("values")
        if not v or len(v) < 2:
            return None, None
        return float(v[1]["high"]), float(v[1]["low"])   # index 1 = last completed period
    except Exception as e:
        print(f"  {pair} {interval} level error: {e}")
        return None, None

def build_levels():
    global levels_cache, levels_day
    out = {}
    for p in PAIRS:
        pdh, pdl = prev_levels(p, "1day");  time.sleep(8)
        pwh, pwl = prev_levels(p, "1week"); time.sleep(8)
        out[p] = {"Previous Day High": pdh, "Previous Day Low": pdl,
                  "Previous Week High": pwh, "Previous Week Low": pwl}
    levels_cache = out
    levels_day = dt.datetime.now(ZoneInfo(LEVEL_TIMEZONE)).date()
    print("Levels rebuilt for", levels_day)

def last_closed_candle(pair):
    # Fetch recent 1h candles in UTC; return the most recent CLOSED one's high/low.
    url = (f"https://api.twelvedata.com/time_series?symbol={pair}"
           f"&interval=1h&outputsize=4&timezone=UTC&apikey={TWELVE_DATA_KEY}")
    try:
        v = requests.get(url, timeout=20).json().get("values")
        if not v or len(v) < 2:
            return None
        now = dt.datetime.now(dt.timezone.utc)
        forming = v[0]["datetime"][:13] == now.strftime("%Y-%m-%d %H")
        c = v[1] if forming else v[0]
        return float(c["high"]), float(c["low"])
    except Exception as e:
        print(f"  {pair} candle error: {e}")
        return None

def fmt(symbol, price):
    return f"{price:.{3 if 'JPY' in symbol else 5}f}"

# ─── TOUCH CHECK (against the closed candle's range) ───────────────────────
def check_pair(pair, c_high, c_low):
    for lname, lvl in levels_cache.get(pair, {}).items():
        if lvl is None:
            continue
        key = (pair, lname)

        # re-arm when the level rolls to a new day/week
        if armed_value.get(key) != lvl:
            armed_value[key] = lvl
            armed[key] = True

        in_range = c_low <= lvl <= c_high

        # re-arm once the candle is clear of the level by the buffer
        if not in_range and not armed.get(key, True):
            if min(abs(lvl - c_low), abs(lvl - c_high)) > lvl * REARM_PCT:
                armed[key] = True

        if in_range and armed.get(key, True):
            armed[key] = False
            up = "High" in lname
            arrow = "🟢⬆️" if up else "🔴⬇️"
            send_telegram(
                f"{arrow} <b>{pair.replace('/','')} reached {lname}</b>\n"
                f"Level {fmt(pair, lvl)}"
            )
            print(f"  ALERT {pair} {lname}")

# ─── ONE HOURLY PASS ───────────────────────────────────────────────────────
def run_check():
    if not market_open():
        print("Market closed — skipping.")
        return
    today = dt.datetime.now(ZoneInfo(LEVEL_TIMEZONE)).date()
    if today != levels_day or not levels_cache:
        build_levels()
    for p in PAIRS:
        c = last_closed_candle(p)
        if c:
            check_pair(p, c[0], c[1])
        time.sleep(2)

# ─── SCHEDULER: fire at HH:05 every hour ───────────────────────────────────
def seconds_to_next_check():
    now = dt.datetime.now(dt.timezone.utc)
    target = now.replace(minute=CHECK_MINUTE, second=0, microsecond=0)
    if now >= target:
        target += dt.timedelta(hours=1)
    return (target - now).total_seconds()

def loop():
    send_telegram("✅ <b>Hourly level-touch scanner is live.</b>\n"
                  "Checks every hour at :05 — pings if that hour touched a "
                  "Prev Day / Week High or Low.")
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
