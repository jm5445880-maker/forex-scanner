import os
import requests
import time
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask

# ─────────────────────────────────────────────────────────────────────────
# FLASK (background thread)
# ─────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return "Forex Scanner is running 24/7."

def run_flask():
    app.run(host="0.0.0.0", port=10000)

# ─────────────────────────────────────────────────────────────────────────
# SECRETS — loaded from environment variables (set these in Render)
# ─────────────────────────────────────────────────────────────────────────
TWELVE_DATA_KEY  = os.environ.get("TWELVE_DATA_KEY")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ─────────────────────────────────────────────────────────────────────────
# YOUR SETTINGS
# ─────────────────────────────────────────────────────────────────────────
ATR_LENGTH               = 14
ATR_MULTIPLIER           = 1.4
USE_VOLUME_FILTER        = True
VOLUME_MA_LENGTH         = 20
USE_VOLUME_SPIKE         = True
VOLUME_SPIKE_MULTIPLIER  = 1.4

IRELAND_TZ = ZoneInfo("Europe/Dublin")

PAIRS = [
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "AUD/USD",
    "USD/CAD",
    "NZD/USD",
]

PAIR_DELAY     = 20
CHECK_INTERVAL = 3600

# ─────────────────────────────────────────────────────────────────────────
# SESSION OPENS
# ─────────────────────────────────────────────────────────────────────────
SESSION_OPENS = {
    0:  ("\U0001f30f", "Asia Session Open",     "Tokyo & Sydney markets now open."),
    8:  ("\U0001f3e6", "London Session Open",   "London market now open. Highest liquidity session."),
    13: ("\U0001f5fd", "New York Session Open", "New York market now open. High volatility expected."),
}

# ─────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────
last_alerted     = {}
last_session_day = {}
fail_count       = {p.replace("/", ""): 0     for p in PAIRS}
fail_warned      = {p.replace("/", ""): False  for p in PAIRS}
last_scan_time   = {"ts": time.time()}

# ─────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────
def send_telegram(msg):
    for attempt in range(3):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            r   = requests.post(url, json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       msg,
                "parse_mode": "HTML"
            }, timeout=10)
            if r.ok:
                return True
        except Exception as e:
            print(f"  Telegram error (attempt {attempt+1}): {e}")
            time.sleep(3)
    return False

# ─────────────────────────────────────────────────────────────────────────
# HEALTH HELPERS
# ─────────────────────────────────────────────────────────────────────────
def get_failing_pairs():
    return [(n, c) for n, c in fail_count.items() if c >= 3]

def get_health_summary():
    failing = get_failing_pairs()
    total   = len(PAIRS)
    if not failing:
        return f"\u2705 All {total} pairs scanning normally."
    healthy = total - len(failing)
    lines   = ["\u26a0\ufe0f Pair issues detected:"]
    for name, count in failing:
        lines.append(f"  - {name}: failed {count} scans in a row")
    lines.append(f"\n{healthy} of {total} pairs scanning normally.")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────
# SESSION CHECKER (independent 60s loop)
# ─────────────────────────────────────────────────────────────────────────
def session_checker_loop():
    while True:
        try:
            now_irish = datetime.now(IRELAND_TZ)
            hour      = now_irish.hour
            today     = now_irish.date()
            if hour in SESSION_OPENS and last_session_day.get(hour) != today:
                last_session_day[hour] = today
                emoji, title, desc = SESSION_OPENS[hour]
                health = get_health_summary()
                msg = (
                    f"{emoji} <b>{title}</b>\n\n"
                    f"{desc}\n"
                    f"Scanner is active. Watching {len(PAIRS)} major pairs on 1H.\n"
                    f"<i>{now_irish.strftime('%H:%M')} Irish time</i>\n\n"
                    f"{health}"
                )
                send_telegram(msg)
                print(f"  [{now_irish.strftime('%H:%M')}] Session message sent: {title}")
        except Exception as e:
            print(f"  Session checker error: {e}")
        time.sleep(60)

# ─────────────────────────────────────────────────────────────────────────
# WATCHDOG
# ─────────────────────────────────────────────────────────────────────────
def watchdog_loop():
    time.sleep(600)
    while True:
        try:
            age = time.time() - last_scan_time["ts"]
            if age > 10800:
                print(f"  WATCHDOG: no scan for {age/3600:.1f}h — killing process")
                try:
                    send_telegram(
                        "\u26a0\ufe0f <b>Watchdog triggered.</b>\n"
                        "Scanner stopped unexpectedly. Restarting now."
                    )
                except Exception:
                    pass
                os._exit(1)
        except Exception as e:
            print(f"  Watchdog error: {e}")
        time.sleep(600)

# ─────────────────────────────────────────────────────────────────────────
# FAILURE WARNING
# ─────────────────────────────────────────────────────────────────────────
def check_failure_warning(name):
    count = fail_count[name]
    if count == 3 and not fail_warned[name]:
        fail_warned[name] = True
        msg_lines = [
            f"\u26a0\ufe0f <b>Scanner Warning</b>\n",
            f"<b>{name}</b> has failed 3 scans in a row.",
            f"Data could not be fetched from Twelve Data.",
            f"You may be missing signals on this pair.",
        ]
        other_failing = [(n, c) for n, c in get_failing_pairs() if n != name]
        if other_failing:
            msg_lines.append("\n\u26a0\ufe0f Also still failing:")
            for n, c in other_failing:
                msg_lines.append(f"  - {n}: {c} scans failed in a row")
        healthy = len(PAIRS) - len(get_failing_pairs())
        msg_lines.append(f"\n{healthy} of {len(PAIRS)} pairs scanning normally.")
        send_telegram("\n".join(msg_lines))
        print(f"  Failure warning sent for {name}")

# ─────────────────────────────────────────────────────────────────────────
# ATR
# ─────────────────────────────────────────────────────────────────────────
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
    vals = [atr]
    for t in trs[period:]:
        atr = (atr * (period - 1) + t) / period
        vals.append(atr)
    return vals

# ─────────────────────────────────────────────────────────────────────────
# FETCH ONE PAIR
# ─────────────────────────────────────────────────────────────────────────
def fetch_pair(pair):
    name = pair.replace("/", "")
    url  = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol={pair}&interval=1h&outputsize=100"
        f"&apikey={TWELVE_DATA_KEY}"
    )
    try:
        r    = requests.get(url, timeout=20)
        data = r.json()

        if data.get("status") == "error":
            msg  = data.get("message", "").lower()
            code = data.get("code", 0)
            if "too many" in msg or "rate limit" in msg or code == 429:
                print(f"  {name}: rate limit hit")
                return "RATE_LIMIT"
            print(f"  {name}: API error — {data.get('message')}")
            return None

        values = data.get("values")

        # Fix: validate values is actually a list
        if not isinstance(values, list) or len(values) < ATR_LENGTH + VOLUME_MA_LENGTH + 5:
            print(f"  {name}: not enough data or invalid response")
            return None

        return values

    except Exception as e:
        print(f"  {name}: fetch error — {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────
# ANALYZE ONE PAIR
# ─────────────────────────────────────────────────────────────────────────
def analyze(pair, values):
    name = pair.replace("/", "")
    try:
        values     = list(reversed(values))
        timestamps = [v["datetime"] for v in values]
        opens      = [float(v["open"])  for v in values]
        highs      = [float(v["high"])  for v in values]
        lows       = [float(v["low"])   for v in values]
        closes     = [float(v["close"]) for v in values]

        # Real tick volume — handle null/zero gracefully
        volumes = []
        volume_available = False
        for v in values:
            raw = v.get("volume")
            try:
                val = float(raw) if raw is not None else 0.0
            except Exception:
                val = 0.0
            volumes.append(val)
            if val > 0:
                volume_available = True

        n         = len(closes)
        idx       = n - 2
        candle_ts = timestamps[idx]

        # Fix: stale candle guard with explicit ValueError catch
        try:
            candle_dt = datetime.strptime(candle_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - candle_dt).total_seconds() / 3600
            if age_hours > 2:
                print(f"  {name}: candle too old ({age_hours:.1f}h) — skipping")
                return None
        except ValueError as e:
            print(f"  {name}: timestamp format error '{candle_ts}' — {e} — skipping")
            return None

        atr_vals = calc_atr(highs, lows, closes, ATR_LENGTH)
        atr_idx  = len(atr_vals) - 2
        if atr_idx < 0:
            return None

        vol_window = volumes[idx - VOLUME_MA_LENGTH + 1: idx + 1]
        if len(vol_window) < VOLUME_MA_LENGTH:
            return None

        vol_ma       = sum(vol_window) / VOLUME_MA_LENGTH
        candle_range = highs[idx] - lows[idx]
        atr_now      = atr_vals[atr_idx]
        volume_now   = volumes[idx]

        if atr_now == 0:
            return None

        # Rule 1 — strict ATR hard block
        atr_ratio = candle_range / atr_now
        if atr_ratio < ATR_MULTIPLIER:
            print(f"  {name}: ATR {atr_ratio:.2f}x < {ATR_MULTIPLIER}x — blocked")
            return None

        # Rule 2 — volume above MA
        # Fix: if volume data is unavailable from Twelve Data, skip volume filter
        if USE_VOLUME_FILTER:
            if not volume_available or vol_ma == 0:
                print(f"  {name}: volume data unavailable — skipping volume filter")
                is_above_vol_ma = True
            else:
                is_above_vol_ma = volume_now > vol_ma
        else:
            is_above_vol_ma = True

        # Rule 3 — volume spike
        if USE_VOLUME_SPIKE:
            if not volume_available or vol_ma == 0:
                print(f"  {name}: volume data unavailable — skipping spike filter")
                is_spike  = True
                vol_ratio = 0.0
            else:
                vol_ratio = volume_now / vol_ma
                is_spike  = vol_ratio >= VOLUME_SPIKE_MULTIPLIER
        else:
            vol_ratio = 0.0
            is_spike  = True

        is_strong = is_above_vol_ma and is_spike
        is_bull   = closes[idx] > opens[idx]

        print(f"  {name}: ATR {atr_ratio:.2f}x ✓ | "
              f"Vol {'N/A' if not volume_available else f'{vol_ratio:.2f}x'} | "
              f"Strong={is_strong}")

        return {
            "is_strong":        is_strong,
            "is_bull":          is_bull,
            "atr":              atr_now,
            "atr_ratio":        atr_ratio,
            "vol_ma":           vol_ma,
            "vol_ratio":        vol_ratio,
            "volume_available": volume_available,
            "candle_ts":        candle_ts,
            "name":             name,
        }

    except Exception as e:
        print(f"  {name}: analyze error — {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────
# SCAN ALL PAIRS
# ─────────────────────────────────────────────────────────────────────────
def scan():
    now_irish = datetime.now(IRELAND_TZ)
    print(f"\n[{now_irish.strftime('%Y-%m-%d %H:%M')} Irish] Scanning {len(PAIRS)} pairs...")
    signals_found = 0

    for pair in PAIRS:
        name   = pair.replace("/", "")
        values = None

        try:
            values = fetch_pair(pair)
            if values == "RATE_LIMIT":
                print(f"  {name}: waiting 60s then retrying...")
                time.sleep(60)
                values = fetch_pair(pair)
                if values == "RATE_LIMIT":
                    print(f"  {name}: still rate limited — skipping")
                    values = None
        except Exception as e:
            print(f"  {name}: fetch crashed — {e}")
            values = None

        time.sleep(PAIR_DELAY)

        if not isinstance(values, list):
            fail_count[name] += 1
            print(f"  {name}: fail count = {fail_count[name]}")
            check_failure_warning(name)
            continue

        if fail_count[name] > 0:
            print(f"  {name}: recovered after {fail_count[name]} failures")
        fail_count[name]  = 0
        fail_warned[name] = False

        result = None
        try:
            result = analyze(pair, values)
        except Exception as e:
            print(f"  {name}: analyze crashed — {e}")

        if result is None:
            continue

        if result["is_strong"]:
            candle_ts = result["candle_ts"]

            if last_alerted.get(name) == candle_ts:
                print(f"  {name}: already alerted for this candle")
                continue

            last_alerted[name] = candle_ts
            signals_found += 1

            direction = "Bullish" if result["is_bull"] else "Bearish"
            emoji     = "\U0001f7e2" if result["is_bull"] else "\U0001f534"

            try:
                open_utc   = datetime.strptime(candle_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                open_irish = open_utc.astimezone(IRELAND_TZ)
                time_str   = (f"{open_utc.strftime('%Y-%m-%d %H:%M')} UTC "
                              f"({open_irish.strftime('%H:%M')} Irish)")
            except Exception:
                time_str = candle_ts + " UTC"

            # Volume line — show N/A if data unavailable
            if result["volume_available"] and result["vol_ma"] > 0:
                vol_line = f"Volume spike ratio: {result['vol_ratio']:.2f}x"
            else:
                vol_line = "Volume spike ratio: N/A (no tick data)"

            vol_ma_str = "N/A" if not result["volume_available"] else str(int(result["vol_ma"]))

            msg = (
                f"\u26a1 <b>Strong Candle Detected!</b>\n\n"
                f"Pair: <b>{name}</b>\n"
                f"Direction: {emoji} {direction}\n"
                f"Candle open: {time_str}\n\n"
                f"ATR ({ATR_LENGTH}): {result['atr']:.5f}\n"
                f"ATR ratio: {result['atr_ratio']:.2f}x\n"
                f"Volume MA ({VOLUME_MA_LENGTH}): {vol_ma_str}\n"
                f"{vol_line}\n\n"
                f"Timeframe: 1H"
            )

            print(f"  {name}: STRONG CANDLE! {direction} — alert sent")
            send_telegram(msg)
        else:
            print(f"  {name}: volume filters failed — no alert")

    if signals_found == 0:
        print("  No strong candles this scan.")

# ─────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":

    # Validate env vars on startup
    missing = []
    if not TWELVE_DATA_KEY:  missing.append("TWELVE_DATA_KEY")
    if not TELEGRAM_TOKEN:   missing.append("TELEGRAM_TOKEN")
    if not TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
    if missing:
        print(f"FATAL: Missing environment variables: {', '.join(missing)}")
        print("Set these in Render → Environment before deploying.")
        exit(1)

    # Background threads
    threading.Thread(target=run_flask,            daemon=True).start()
    threading.Thread(target=session_checker_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop,        daemon=True).start()

    send_telegram(
        "\u2705 <b>Forex Scanner — security + stability update!</b>\n\n"
        "Changes:\n"
        "- Secrets moved to environment variables\n"
        "- Volume null handled — filters skip gracefully if no tick data\n"
        "- Stale candle timestamp errors now visible\n"
        "- API response validation improved\n"
        "- All previous fixes retained\n\n"
        f"Settings: ATR {ATR_LENGTH} x{ATR_MULTIPLIER} | "
        f"Vol MA {VOLUME_MA_LENGTH} | Spike x{VOLUME_SPIKE_MULTIPLIER}"
    )

    while True:
        try:
            scan()
            last_scan_time["ts"] = time.time()
        except Exception as e:
            print(f"\n  SCAN ERROR: {e}")
            try:
                send_telegram(
                    f"\u26a0\ufe0f Scanner error — recovering.\n"
                    f"<code>{str(e)[:200]}</code>"
                )
            except Exception:
                pass
            time.sleep(300)
            continue

        print(f"  Next scan in 60 minutes...\n")
        time.sleep(CHECK_INTERVAL)
