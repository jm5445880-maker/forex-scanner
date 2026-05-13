import requests
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
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

# Ireland timezone — handles DST automatically forever
IRELAND_TZ = ZoneInfo("Europe/Dublin")

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

CHECK_INTERVAL = 3600  # 60 minutes

# ─── SESSION OPEN TIMES (Irish local hour) ────────────────────────────────
SESSION_OPENS = {
    0:  ("\U0001f30f", "Asia Session Open",     "Tokyo & Sydney markets now open."),
    8:  ("\U0001f3e6", "London Session Open",   "London market now open. Highest liquidity session."),
    13: ("\U0001f5fd", "New York Session Open", "New York market now open. High volatility expected."),
}

# ─── STATE TRACKING ───────────────────────────────────────────────────────
last_alerted      = {}   # last candle timestamp alerted per pair
last_session_day  = {}   # last date session message was sent per hour
fail_count        = {p.replace("/",""): 0 for p in PAIRS}   # consecutive fail count
fail_warned       = {p.replace("/",""): False for p in PAIRS}  # whether first warning was sent

# ─── TELEGRAM ─────────────────────────────────────────────────────────────
def send_telegram(msg):
    for attempt in range(3):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML"
            }, timeout=10)
            if r.ok:
                return True
        except Exception as e:
            print(f"  Telegram error (attempt {attempt+1}): {e}")
            time.sleep(3)
    return False

# ─── FAILURE STATUS HELPER ────────────────────────────────────────────────
def get_failing_pairs():
    """Returns list of (name, fail_count) for all currently failing pairs."""
    return [(name, count) for name, count in fail_count.items() if count >= 3]

def get_health_summary():
    """Returns a health status string for use in session messages."""
    failing = get_failing_pairs()
    total   = len(PAIRS)
    if not failing:
        return f"\u2705 All {total} pairs scanning normally."
    else:
        healthy = total - len(failing)
        lines   = [f"\u26a0\ufe0f Pair issues detected:"]
        for name, count in failing:
            lines.append(f"  - {name}: failed {count} scans in a row")
        lines.append(f"\n{healthy} of {total} pairs scanning normally.")
        return "\n".join(lines)

# ─── SESSION OPEN CHECKER ─────────────────────────────────────────────────
def check_session_opens():
    now_irish = datetime.now(IRELAND_TZ)
    hour      = now_irish.hour
    today     = now_irish.date()

    if hour in SESSION_OPENS:
        if last_session_day.get(hour) != today:
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
            print(f"  Session message sent: {title}")

# ─── FAILURE WARNING ──────────────────────────────────────────────────────
def check_failure_warning(name):
    """
    Called after a pair fails.
    Sends a standalone warning only the FIRST time a pair hits 3 failures.
    If other pairs are also already failing, includes them in the message.
    """
    count = fail_count[name]

    # Only trigger at exactly 3 — and only if we haven't warned yet for this pair
    if count == 3 and not fail_warned[name]:
        fail_warned[name] = True

        # Build message
        msg_lines = [
            f"\u26a0\ufe0f <b>Scanner Warning</b>\n",
            f"<b>{name}</b> has failed 3 scans in a row.",
            f"Data could not be fetched from Twelve Data.",
            f"You may be missing signals on this pair.",
        ]

        # Include other already-failing pairs (excluding the current one)
        other_failing = [(n, c) for n, c in get_failing_pairs() if n != name]
        if other_failing:
            msg_lines.append(f"\n\u26a0\ufe0f Also still failing:")
            for n, c in other_failing:
                msg_lines.append(f"  - {n}: {c} scans failed in a row")

        # How many healthy
        total_failing = len(get_failing_pairs())
        healthy = len(PAIRS) - total_failing
        msg_lines.append(f"\n{healthy} of {len(PAIRS)} pairs scanning normally.")

        send_telegram("\n".join(msg_lines))
        print(f"  Failure warning sent for {name}")

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

# ─── FETCH AND ANALYZE ONE PAIR ───────────────────────────────────────────
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
        r       = requests.get(url, timeout=20)
        data    = r.json()

        if data.get("status") == "error":
            print(f"  {name}: API error - {data.get('message')}")
            return None

        values = data.get("values")
        if not values or len(values) < ATR_LENGTH + VOLUME_MA_LENGTH + 5:
            print(f"  {name}: not enough data")
            return None

        # Reverse to oldest first
        values     = list(reversed(values))
        timestamps = [v["datetime"] for v in values]
        opens      = [float(v["open"])  for v in values]
        highs      = [float(v["high"])  for v in values]
        lows       = [float(v["low"])   for v in values]
        closes     = [float(v["close"]) for v in values]

        # Real tick volume
        volumes = []
        for v in values:
            raw = v.get("volume")
            try:
                volumes.append(float(raw) if raw is not None else 0.0)
            except Exception:
                volumes.append(0.0)

        n         = len(closes)
        idx       = n - 2
        candle_ts = timestamps[idx]

        # ── STALE CANDLE GUARD ───────────────────────────────────────────
        try:
            from datetime import timezone
            candle_dt = datetime.strptime(candle_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - candle_dt).total_seconds() / 3600
            if age_hours > 2:
                print(f"  {name}: candle too old ({age_hours:.1f}h) — skipping")
                return None
        except Exception:
            pass

        # ── CALCULATIONS ─────────────────────────────────────────────────
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

        # ── RULE 1: STRICT ATR HARD BLOCK ────────────────────────────────
        if atr_now == 0:
            return None
        atr_ratio = candle_range / atr_now
        if atr_ratio < ATR_MULTIPLIER:
            print(f"  {name}: ATR {atr_ratio:.2f}x < {ATR_MULTIPLIER}x — blocked")
            return None

        # ── RULE 2 & 3: VOLUME FILTERS ───────────────────────────────────
        is_above_vol_ma = (volume_now > vol_ma)      if USE_VOLUME_FILTER else True
        vol_ratio       = (volume_now / vol_ma)      if vol_ma > 0        else 0
        is_spike        = (vol_ratio >= VOLUME_SPIKE_MULTIPLIER) if USE_VOLUME_SPIKE else True

        is_strong = is_above_vol_ma and is_spike
        is_bull   = closes[idx] > opens[idx]

        print(f"  {name}: ATR {atr_ratio:.2f}x ✓ | Vol {vol_ratio:.2f}x | Strong={is_strong}")

        return {
            "is_strong":  is_strong,
            "is_bull":    is_bull,
            "atr":        atr_now,
            "atr_ratio":  atr_ratio,
            "vol_ma":     vol_ma,
            "vol_ratio":  vol_ratio,
            "candle_ts":  candle_ts,
            "name":       name,
        }

    except Exception as e:
        print(f"  {name}: unexpected error — {e}")
        return None

# ─── SCAN ALL PAIRS ───────────────────────────────────────────────────────
def scan():
    from datetime import timezone
    print(f"\n[{datetime.now(IRELAND_TZ).strftime('%Y-%m-%d %H:%M')} Irish] Scanning {len(PAIRS)} pairs...")
    signals_found = 0

    for pair in PAIRS:
        name = pair.replace("/", "")
        result = None
        try:
            result = fetch_and_analyze(pair)
        except Exception as e:
            print(f"  {name}: crashed during fetch — {e}")

        # Wait between requests (Twelve Data rate limit)
        time.sleep(9)

        if result is None:
            # Count the failure
            fail_count[name] += 1
            print(f"  {name}: fail count now {fail_count[name]}")
            check_failure_warning(name)
            continue

        # Pair succeeded — reset failure tracking
        if fail_count[name] > 0:
            print(f"  {name}: recovered after {fail_count[name]} failures")
        fail_count[name]  = 0
        fail_warned[name] = False

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

# ─── MAIN LOOP WITH FULL CRASH RECOVERY ───────────────────────────────────
def scanner_loop():
    send_telegram(
        "\u2705 <b>Forex Scanner — fully rebuilt!</b>\n\n"
        "What's new:\n"
        "- Full crash recovery (never dies on errors)\n"
        "- Smart pair failure tracking & warnings\n"
        "- Session open messages with health status\n"
        "- Auto DST handling for Ireland — forever\n"
        "- Strict ATR hard block (1.4x minimum)\n"
        "- Real tick volume from Twelve Data\n"
        "- 7 majors only, EURGBP removed\n\n"
        f"Settings: ATR {ATR_LENGTH} x{ATR_MULTIPLIER} | "
        f"Vol MA {VOLUME_MA_LENGTH} | Spike x{VOLUME_SPIKE_MULTIPLIER}"
    )

    while True:
        try:
            check_session_opens()
            scan()
        except Exception as e:
            print(f"\n  LOOP CRASH: {e} — recovering in 5 minutes...")
            try:
                send_telegram(
                    f"\u26a0\ufe0f Scanner hit an unexpected error but recovered automatically.\n"
                    f"<code>{str(e)[:200]}</code>"
                )
            except Exception:
                pass
            time.sleep(300)
            continue

        print(f"  Next scan in 60 minutes...\n")
        time.sleep(CHECK_INTERVAL)

# ─── STARTUP ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    thread = threading.Thread(target=scanner_loop, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=10000)
