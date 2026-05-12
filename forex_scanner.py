import requests
import time
import threading
from datetime import datetime, timezone, timedelta
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

# 7 true major forex pairs only
PAIRS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "AUD/USD",
    "USD/CAD",
    "NZD/USD",
]

# Scan every 60 minutes
# 7 pairs x 1 request = 7 requests per scan
# 24 scans x 7 = 168 requests per day (well within 800 daily limit)
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

        # ── REAL TICK VOLUME ─────────────────────────────────────────────
        # Twelve Data provides tick volume for forex — same concept as
        # TradingView broker tick volume. Use it directly, no proxies.
        volumes = []
        for v in values:
            raw = v.get("volume")
            if raw is not None:
                try:
                    volumes.append(float(raw))
                except Exception:
                    volumes.append(0.0)
            else:
                volumes.append(0.0)

        n = len(closes)

        # ── STALE CANDLE GUARD ───────────────────────────────────────────
        # Use last fully closed candle = index n-2 (n-1 is still forming)
        idx       = n - 2
        candle_ts = timestamps[idx]  # format: "2026-05-12 10:00:00"

        try:
            candle_dt = datetime.strptime(candle_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            now_utc   = datetime.now(timezone.utc)
            age_hours = (now_utc - candle_dt).total_seconds() / 3600
            if age_hours > 2:
                print(f"  {name}: candle too old ({age_hours:.1f}h), skipping")
                return None
        except Exception:
            pass

        # ── CALCULATIONS ─────────────────────────────────────────────────
        atr_vals = calc_atr(highs, lows, closes, ATR_LENGTH)
        atr_idx  = len(atr_vals) - 2

        if atr_idx < 0:
            return None

        vol_window = volumes[idx - VOLUME_MA_LENGTH + 1: idx + 1]
        vol_ma     = sum(vol_window) / VOLUME_MA_LENGTH

        candle_range = highs[idx] - lows[idx]
        atr_now      = atr_vals[atr_idx]
        volume_now   = volumes[idx]

        # ── STRICT ATR HARD BLOCK ─────────────────────────────────────────
        # Rule 1: candle range must be >= 1.4 x ATR
        # If this fails, stop immediately — do not check anything else
        atr_ratio = candle_range / atr_now if atr_now > 0 else 0
        if atr_ratio < ATR_MULTIPLIER:
            print(f"  {name}: ATR ratio {atr_ratio:.2f}x < {ATR_MULTIPLIER}x — blocked")
            return None

        # ── VOLUME CHECKS ─────────────────────────────────────────────────
        # Rule 2: volume must be above 20-period MA
        is_above_vol_ma = volume_now > vol_ma if USE_VOLUME_FILTER else True

        # Rule 3: volume must be >= 1.4 x MA (spike)
        vol_ratio  = volume_now / vol_ma if vol_ma > 0 else 0
        is_spike   = vol_ratio >= VOLUME_SPIKE_MULTIPLIER if USE_VOLUME_SPIKE else True

        # All rules must pass
        is_strong = is_above_vol_ma and is_spike
        is_bull   = closes[idx] > opens[idx]

        print(f"  {name}: ATR ratio={atr_ratio:.2f}x ✓ | "
              f"Vol ratio={vol_ratio:.2f}x | "
              f"Strong={is_strong}")

        return {
            "is_strong":   is_strong,
            "is_bull":     is_bull,
            "atr":         atr_now,
            "atr_ratio":   atr_ratio,
            "vol_ma":      vol_ma,
            "vol_ratio":   vol_ratio,
            "candle_ts":   candle_ts,
            "name":        name,
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

        # Twelve Data: 8 requests/minute max — wait 9 seconds between each
        time.sleep(9)

        if result is None:
            continue

        if result["is_strong"]:
            candle_ts = result["candle_ts"]

            # Skip if already alerted for this exact candle
            if last_alerted.get(name) == candle_ts:
                print(f"  {name}: already alerted for this candle")
                continue

            last_alerted[name] = candle_ts
            signals_found += 1

            direction = "Bullish" if result["is_bull"] else "Bearish"
            emoji     = "\U0001f7e2" if result["is_bull"] else "\U0001f534"

            # ── CANDLE OPEN TIME — UTC and Irish time ─────────────────────
            try:
                open_dt    = datetime.strptime(candle_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                irish_dt   = open_dt + timedelta(hours=1)  # Ireland = UTC+1 (IST)
                time_str   = (f"{open_dt.strftime('%Y-%m-%d %H:%M')} UTC "
                              f"({irish_dt.strftime('%H:%M')} Irish)")
            except Exception:
                time_str = candle_ts + " UTC"

            # ── CLEAN MESSAGE — 4 values only ─────────────────────────────
            msg = (
                f"\u26a1 <b>Strong Candle Detected!</b>\n\n"
                f"Pair: <b>{name}</b>\n"
                f"Direction: {emoji} {direction}\n"
                f"Candle open: {time_str}\n\n"
                f"ATR ({ATR_LENGTH}): {result['atr']:.5f}\n"
                f"ATR ratio: {result['atr_ratio']:.2f}x\n"
                f"Volume MA ({VOLUME_MA_LENGTH}): {result['vol_ma']:.0f}\n"
                f"Volume spike ratio: {result['vol_ratio']:.2f}x\n\n"
                f"Timeframe: 1H"
            )

            print(f"  {name}: STRONG CANDLE! {direction} — sending alert...")
            send_telegram(msg)
        else:
            print(f"  {name}: volume filters failed — no alert")

    if signals_found == 0:
        print("  No strong candles this scan.")

# ─── MAIN LOOP ────────────────────────────────────────────────────────────
def scanner_loop():
    send_telegram(
        "\u2705 <b>Forex Scanner — clean build deployed!</b>\n\n"
        "Changes applied:\n"
        "- Strict ATR hard block (1.4x minimum, no exceptions)\n"
        "- Real tick volume from Twelve Data (no more pip proxy)\n"
        "- Candle open time in UTC + Irish time\n"
        "- Message shows 4 clean values only\n"
        "- EURGBP removed\n"
        "- Stale candle guard active\n\n"
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
