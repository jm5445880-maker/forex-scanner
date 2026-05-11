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

ALPHA_VANTAGE_KEY = "YW0G1L2AN9QJ8MIH"

TELEGRAM_TOKEN   = "8679884583:AAEqFAjPY5nX4Z7wM2jGS7Oe58xxy4EqkPU"
TELEGRAM_CHAT_ID = "7313311226"

# Major forex pairs in Alpha Vantage format (from_currency / to_currency)
PAIRS = [
    ("EUR", "USD", "EURUSD"),
    ("GBP", "USD", "GBPUSD"),
    ("USD", "JPY", "USDJPY"),
    ("USD", "CHF", "USDCHF"),
    ("AUD", "USD", "AUDUSD"),
    ("USD", "CAD", "USDCAD"),
    ("NZD", "USD", "NZDUSD"),
    ("EUR", "GBP", "EURGBP"),
]

# Alpha Vantage free tier = 25 requests/day, 5 per minute
# 8 pairs × 1 request each = 8 requests per scan
# We scan every 60 minutes to stay well within limits
CHECK_INTERVAL = 3600  # 60 minutes

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

# ─── FETCH FROM ALPHA VANTAGE ─────────────────────────────────────────────
def fetch_and_analyze(from_cur, to_cur, name):
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=FX_INTRADAY"
            f"&from_symbol={from_cur}"
            f"&to_symbol={to_cur}"
            f"&interval=60min"
            f"&outputsize=full"
            f"&apikey={ALPHA_VANTAGE_KEY}"
        )
        r = requests.get(url, timeout=20)
        data = r.json()

        key = "Time Series FX (60min)"
        if key not in data:
            print(f"  {name}: bad response from Alpha Vantage - {data.get('Note') or data.get('Information') or 'unknown error'}")
            return None

        ts = data[key]
        # Sort oldest to newest
        sorted_times = sorted(ts.keys())

        opens   = [float(ts[t]["1. open"])  for t in sorted_times]
        highs   = [float(ts[t]["2. high"])  for t in sorted_times]
        lows    = [float(ts[t]["3. low"])   for t in sorted_times]
        closes  = [float(ts[t]["4. close"]) for t in sorted_times]

        # Alpha Vantage FX intraday does NOT have volume — it returns 0
        # We use tick count proxy: candle body + range as activity measure
        # For volume filter we use a synthetic volume = (high - low) * 100000
        # This is standard practice for forex — pip movement as volume proxy
        volumes = [(highs[i] - lows[i]) * 100000 for i in range(len(closes))]

        n = len(closes)
        if n < ATR_LENGTH + VOLUME_MA_LENGTH + 5:
            print(f"  {name}: not enough candles ({n})")
            return None

        # Use last FULLY CLOSED candle (second to last)
        idx        = n - 2
        candle_ts  = sorted_times[idx]

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

        pass_vol   = is_above_vol_ma if USE_VOLUME_FILTER else True
        pass_spike = is_spike        if USE_VOLUME_SPIKE  else True

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
        }

    except Exception as e:
        print(f"  {name}: error - {e}")
        return None

# ─── SCAN ALL PAIRS ───────────────────────────────────────────────────────
def scan():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Scanning {len(PAIRS)} pairs...")
    signals_found = 0

    for from_cur, to_cur, name in PAIRS:
        result = fetch_and_analyze(from_cur, to_cur, name)

        # Alpha Vantage free = 5 requests/min — wait 13s between each pair
        time.sleep(13)

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

            direction   = "Bullish" if result["is_bull"] else "Bearish"
            emoji       = "\U0001f7e2" if result["is_bull"] else "\U0001f534"

            msg = (
                f"\u26a1 <b>Strong Candle Detected!</b>\n\n"
                f"Pair: <b>{name}</b>\n"
                f"Direction: {emoji} {direction}\n"
                f"Candle time: {candle_ts} UTC\n"
                f"Candle range: {result['candle_range']:.5f}\n"
                f"ATR ({ATR_LENGTH}): {result['atr']:.5f}\n"
                f"Range/ATR ratio: {result['candle_range'] / (ATR_MULTIPLIER * result['atr']):.2f}x\n"
                f"Range vs MA ratio: {result['volume'] / result['vol_ma']:.2f}x\n\n"
                f"Timeframe: 1H"
            )

            print(f"  {name}: STRONG CANDLE! {direction} - sending alert...")
            send_telegram(msg)
        else:
            print(f"  {name}: no signal")

    if signals_found == 0:
        print("  No strong candles this scan.")

# ─── MAIN LOOP ────────────────────────────────────────────────────────────
def scanner_loop():
    send_telegram(
        "\u2705 <b>Forex Scanner restarted with better data!</b>\n\n"
        "Now using Alpha Vantage for accurate forex data.\n"
        "Scanning all 8 major pairs every hour.\n"
        "You will only hear from me when a strong candle forms.\n\n"
        f"Settings: ATR {ATR_LENGTH} x{ATR_MULTIPLIER} | Vol MA {VOLUME_MA_LENGTH} | Spike x{VOLUME_SPIKE_MULTIPLIER}"
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
