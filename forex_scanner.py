import requests
import time
import threading
from datetime import datetime
from flask import Flask

# ─── FLASK APP (keeps Render alive) ──────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return "Forex Scanner is running."

# ─── YOUR SETTINGS ────────────────────────────────────────────────────────
ATR_LENGTH = 14
ATR_MULTIPLIER = 1.4
USE_VOLUME_FILTER = True
VOLUME_MA_LENGTH = 20
USE_VOLUME_SPIKE = True
VOLUME_SPIKE_MULTIPLIER = 1.4

TELEGRAM_TOKEN = "8679884583:AAEqFAjPY5nX4Z7wM2jGS7Oe58xxy4EqkPU"
TELEGRAM_CHAT_ID = "7313311226"

PAIRS = [
    ("EURUSD=X", "EURUSD"),
    ("GBPUSD=X", "GBPUSD"),
    ("USDJPY=X", "USDJPY"),
    ("USDCHF=X", "USDCHF"),
    ("AUDUSD=X", "AUDUSD"),
    ("USDCAD=X", "USDCAD"),
    ("NZDUSD=X", "NZDUSD"),
    ("EURGBP=X", "EURGBP"),
]

CHECK_INTERVAL = 300  # every 5 minutes

# ─── TRACKING (no duplicate alerts for same candle) ───────────────────────
last_alerted = {}

# ─── FUNCTIONS ────────────────────────────────────────────────────────────

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


def fetch_and_analyze(symbol, name):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1h&range=30d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        d = r.json()["chart"]["result"][0]
        timestamps = d["timestamp"]
        q = d["indicators"]["quote"][0]

        opens   = q["open"]
        highs   = q["high"]
        lows    = q["low"]
        closes  = q["close"]
        volumes = q["volume"]

        clean = [
            (timestamps[i], opens[i], highs[i], lows[i], closes[i], volumes[i] or 0)
            for i in range(len(closes))
            if opens[i] and highs[i] and lows[i] and closes[i]
        ]

        if len(clean) < ATR_LENGTH + VOLUME_MA_LENGTH + 5:
            print(f"  {name}: not enough data")
            return None

        ts_list = [x[0] for x in clean]
        o_list  = [x[1] for x in clean]
        h_list  = [x[2] for x in clean]
        l_list  = [x[3] for x in clean]
        c_list  = [x[4] for x in clean]
        v_list  = [x[5] for x in clean]

        idx       = len(c_list) - 2
        candle_ts = ts_list[idx]

        atr_vals = calc_atr(h_list, l_list, c_list, ATR_LENGTH)
        atr_idx  = len(atr_vals) - 2

        if atr_idx < 0:
            return None

        vol_window = v_list[idx - VOLUME_MA_LENGTH + 1: idx + 1]
        vol_ma     = sum(vol_window) / VOLUME_MA_LENGTH

        candle_range = h_list[idx] - l_list[idx]
        atr_now      = atr_vals[atr_idx]
        volume_now   = v_list[idx]

        is_strong_atr   = candle_range >= ATR_MULTIPLIER * atr_now
        is_above_vol_ma = volume_now > vol_ma
        is_spike        = volume_now >= vol_ma * VOLUME_SPIKE_MULTIPLIER

        pass_vol   = is_above_vol_ma if USE_VOLUME_FILTER else True
        pass_spike = is_spike if USE_VOLUME_SPIKE else True

        is_strong = is_strong_atr and pass_vol and pass_spike
        is_bull   = c_list[idx] > o_list[idx]

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
        print(f"  {name}: fetch error - {e}")
        return None


def scan():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Scanning {len(PAIRS)} pairs...")
    signals_found = 0

    for symbol, name in PAIRS:
        result = fetch_and_analyze(symbol, name)
        if result is None:
            continue

        if result["is_strong"]:
            candle_ts = result["candle_ts"]

            if last_alerted.get(name) == candle_ts:
                print(f"  {name}: strong candle (already alerted)")
                continue

            last_alerted[name] = candle_ts
            signals_found += 1

            direction   = "Bullish" if result["is_bull"] else "Bearish"
            emoji       = "\U0001f7e2" if result["is_bull"] else "\U0001f534"
            candle_time = datetime.utcfromtimestamp(candle_ts).strftime('%Y-%m-%d %H:%M UTC')

            msg = (
                f"\u26a1 <b>Strong Candle Detected!</b>\n\n"
                f"Pair: <b>{name}</b>\n"
                f"Direction: {emoji} {direction}\n"
                f"Candle time: {candle_time}\n"
                f"Candle range: {result['candle_range']:.5f}\n"
                f"ATR ({ATR_LENGTH}): {result['atr']:.5f}\n"
                f"Range/ATR ratio: {result['candle_range'] / (ATR_MULTIPLIER * result['atr']):.2f}x\n"
                f"Volume vs MA: {result['volume'] / result['vol_ma']:.2f}x\n\n"
                f"Timeframe: 1H"
            )

            print(f"  {name}: STRONG CANDLE! {direction} - sending alert...")
            send_telegram(msg)
        else:
            print(f"  {name}: no signal")

    if signals_found == 0:
        print("  No strong candles this scan.")


def scanner_loop():
    send_telegram(
        "\u2705 <b>Forex Scanner is now running 24/7!</b>\n\n"
        "I will message you automatically whenever a strong candle forms on any major pair (1H).\n"
        "If nothing forms, you won't hear from me.\n\n"
        f"Settings: ATR {ATR_LENGTH} x{ATR_MULTIPLIER} | Vol MA {VOLUME_MA_LENGTH} | Spike x{VOLUME_SPIKE_MULTIPLIER}"
    )
    while True:
        scan()
        print(f"  Next scan in {CHECK_INTERVAL // 60} minutes...\n")
        time.sleep(CHECK_INTERVAL)


# ─── STARTUP ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    thread = threading.Thread(target=scanner_loop, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=10000)
