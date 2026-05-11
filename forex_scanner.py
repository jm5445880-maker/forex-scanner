import requests
import time
import threading
from datetime import datetime
from flask import Flask

# ─── FLASK APP (keeps Render alive) ───────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return "Forex Scanner is running 24/7."

# ─── YOUR SETTINGS ────────────────────────────────────────────────────────
ATR_LENGTH               = 14
ATR_MULTIPLIER           = 1.4
USE_VOLUME_FILTER        = True
VOLUME_MA_LENGTH         = 20
USE_VOLUME_SPIKE         = True
VOLUME_SPIKE_MULTIPLIER  = 1.4

TWELVE_DATA_KEY  = "9d4be446a0c141ab95608b044b8da5c4"
TELEGRAM_TOKEN   = "8679884583:AAEqFAjPY5nX4Z7wM2jGS7Oe58xxy4EqkPU"
TELEGRAM_CHAT_ID = "7313311226"

# All 8 major forex pairs
PAIRS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "AUD/USD",
    "USD/CAD",
    "NZD/USD",
    "EUR/GBP",
]

# Scan every 60 minutes
# 8 pairs x 1 request = 8 requests per scan
# 24 scans x 8 = 192 requests per day (well within 800 limit)
CHECK_INTERVAL = 3600

# ─── DUPLICATE ALERT PREVENTION ───────────────────────────────────────────
last_alerted = {}

# ─── TELEGRAM ─────────────────────────────────────────────────────────────
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"  Telegram error: {e}")

# ─── ATR CALCULATION ──────────────────────────────────────────────────────
def calc_atr(highs, lows, closes, period):
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)
    if len(trs) < period:
        return []
    atr = sum(trs[:period]) / period
    atr_vals = [atr]
    for t in trs[period:]:
        atr = (atr * (period - 1) + t) / period
        atr_vals.append(atr)
    return atr_vals

# ─── FETCH FROM TWELVE DATA ───────────────────────────────────────────────
def fetch_and_analyze(pair):
    name = pair.replace("/", "")
    try:
        url = (
            f"https://api.twelvedata.com/time_series"
            f"?symbol={pair}"
            f"&interval=1h"
            f"&outputsize=100"
            f"&apikey={TWELVE_DATA_KEY}"
        )
        r = requests.get(url, timeout=20)
        data = r.json()

        if data.get("status") == "error":
            print(f"  {name}: API error - {data.get('message')}")
            return None

        values = data.get("values")
        if not values or len(values) < ATR_LENGTH + VOLUME_MA_LENGTH + 5:
            print(f"  {name}: not enough data")
            return None

        # Twelve Data returns newest first — reverse to oldest first
        values = list(reversed(values))

        timestamps = [v["datetime"] for v in values]
        opens      = [float(v["open"])   for v in values]
        highs      = [float(v["high"])   for v in values]
        lows       = [float(v["low"])    for v in values]
        closes     = [float(v["close"])  for v in values]

        # Twelve Data provides real volume for forex
        # If volume is missing or zero, fall back to pip range proxy
        raw_volumes = []
        for v in values:
            vol = v.get("volume")
            if vol and float(vol) > 0:
                raw_volumes.append(float(vol))
            else:
                raw_volumes.append((float(v["high"]) - float(v["low"])) * 100000)
        volumes = raw_volumes

        n = len(closes)

        # Use last FULLY CLOSED candle (second to last)
        idx       = n - 2
        candle_ts = timestamps[idx]

        atr_vals = calc_atr(highs, lows, closes, ATR_LENGTH)
        atr_idx  = len(atr_vals) - 2

        if atr_idx < 0:
            return None

        vol_window = volumes[idx - VOLUME_MA_LENGTH + 1: idx + 1]
        vol_ma     = sum(vol_window) / VOLUME_MA_LENGTH

        candle_range = highs[idx] - lows[idx]
        atr_now      = atr_vals[atr_idx]
        volume_now   = volumes[idx]

        is_strong_atr   = candle_range >= ATR_MULTIPLIER * atr_now
        is_above_vol_ma = volume_now > vol_ma
        is_spike        = volume_now >= vol_ma * VOLUME_SPIKE_MULTIPLIER

        pass_vol   = is_above_vol_ma if USE_VOLUME_FILTER  else True
        pass_spike = is_spike        if USE_VOLUME_SPIKE   else True

        is_strong = is_strong_atr and pass_vol and pass_spike
        is_bull   = closes[idx] > opens[idx]

        return {
            "is_strong":    is_strong,
            "is_bull":      is_bull,
            "candle_range": candle_range,
            "atr":          atr_now,
            "volume":       volume_now,
            "vol_ma":       vol_ma,
            "candle_ts":    candle_ts,
            "name":         name,
        }

    except Exception as e:
        print(f"  {name}: error - {e}")
        return None

# ─── SCAN ALL PAIRS ───────────────────────────────────────────────────────
def scan():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Scanning {len(PAIRS)} pairs...")
    signals_found = 0

    for pair in PAIRS:
        name   = pair.replace("/", "")
        result = fetch_and_analyze(pair)

        # Twelve Data allows 8 requests/minute — wait 8 seconds between each
        time.sleep(8)

        if result is None:
            continue

        if result["is_strong"]:
            candle_ts = result["candle_ts"]

            # Skip if already alerted for this exact candle
            if last_alerted.get(name) == candle_ts:
                print(f"  {name}: strong candle (already alerted)")
                continue

            last_alerted[name] = candle_ts
            signals_found += 1

            direction = "Bullish" if result["is_bull"] else "Bearish"
            emoji     = "\U0001f7e2" if result["is_bull"] else "\U0001f534"

            msg = (
                f"\u26a1 <b>Strong Candle Detected!</b>\n\n"
                f"Pair: <b>{name}</b>\n"
                f"Direction: {emoji} {direction}\n"
                f"Candle time: {candle_ts} UTC\n"
                f"Candle range: {result['candle_range']:.5f}\n"
                f"ATR ({ATR_LENGTH}): {result['atr']:.5f}\n"
                f"Range/ATR ratio: {result['candle_range'] / (ATR_MULTIPLIER * result['atr']):.2f}x\n"
                f"Volume vs MA: {result['volume'] / result['vol_ma']:.2f}x\n\n"
                f"Timeframe: 1H"
            )

            print(f"  {name}: STRONG CANDLE! {direction} - sending Telegram alert...")
            send_telegram(msg)
        else:
            print(f"  {name}: no signal")

    if signals_found == 0:
        print("  No strong candles this scan.")

# ─── MAIN LOOP ────────────────────────────────────────────────────────────
def scanner_loop():
    send_telegram(
        "\u2705 <b>Forex Scanner is now running 24/7!</b>\n\n"
        "Powered by Twelve Data — proper forex data.\n"
        "Scanning all 8 major pairs every hour.\n"
        "You will only hear from me when a strong candle forms.\n\n"
        f"Settings: ATR {ATR_LENGTH} x{ATR_MULTIPLIER} | "
        f"Vol MA {VOLUME_MA_LENGTH} | Spike x{VOLUME_SPIKE_MULTIPLIER}"
    )
    while True:
        scan()
        print(f"  Next scan in 60 minutes...\n")
        time.sleep(CHECK_INTERVAL)

# ─── STARTUP ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    thread = threading.Thread(target=scanner_loop, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=10000)
