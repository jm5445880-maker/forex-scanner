import requests
import time
import threading
from datetime import datetime, timezone
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

# 20 seconds between pairs = ~3 requests/min, well under 8/min limit
PAIR_DELAY     = 20
CHECK_INTERVAL = 3600  # scan every 60 minutes

# ─── SESSION OPEN TIMES (Irish local hour) ────────────────────────────────
SESSION_OPENS = {
    0:  ("\U0001f30f", "Asia Session Open",     "Tokyo & Sydney markets now open."),
    8:  ("\U0001f3e6", "London Session Open",   "London market now open. Highest liquidity session."),
    13: ("\U0001f5fd", "New York Session Open", "New York market now open. High volatility expected."),
}

# ─── STATE TRACKING ───────────────────────────────────────────────────────
last_alerted     = {}
last_session_day = {}
fail_count       = {p.replace("/", ""): 0     for p in PAIRS}
fail_warned      = {p.replace("/", ""): False  for p in PAIRS}

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

# ─── HEALTH SUMMARY ───────────────────────────────────────────────────────
def get_failing_pairs():
    return [(name, count) for name, count in fail_count.items() if count >= 3]

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
            msg_lines.append(f"\n\u26a0\ufe0f Also still failing:")
            for n, c in other_failing:
                msg_lines.append(f"  - {n}: {c} scans failed in a row")
        healthy = len(PAIRS) - len(get_failing_pairs())
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

# ─── FETCH ONE PAIR (with rate limit detection and auto-retry) ────────────
def fetch_pair(pair):
    """
    Fetches data for one pair from Twelve Data.
    Returns: parsed values list, or None on real error, or "RATE_LIMIT" string
    """
    name = pair.replace("/", "")
    url  = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol={pair}"
        f"&interval=1h"
        f"&outputsize=100"
        f"&apikey={TWELVE_DATA_KEY}"
    )
    try:
        r    = requests.get(url, timeout=20)
        data = r.json()

        if data.get("status") == "error":
            msg = data.get("message", "").lower()
            code = data.get("code", 0)
            # Detect rate limit specifically
            if "too many" in msg or "rate limit" in msg or code == 429:
                print(f"  {name}: rate limit hit — will retry after 60s")
                return "RATE_LIMIT"
            print(f"  {name}: API error — {data.get('message')}")
            return None

        values = data.get("values")
        if not values or len(values) < ATR_LENGTH + VOLUME_MA_LENGTH + 5:
            print(f"  {name}: not enough data ({len(values) if values else 0} candles)")
            return None

        return values

    except Exception as e:
        print(f"  {name}: fetch error — {e}")
        return None

# ─── ANALYZE ONE PAIR ─────────────────────────────────────────────────────
def analyze(pair, values):
    name = pair.replace("/", "")
    try:
        values     = list(reversed(values))
        timestamps = [v["datetime"] for v in values]
        opens      = [float(v["open"])  for v in values]
        highs      = [float(v["high"])  for v in values]
        lows       = [float(v["low"])   for v in values]
        closes     = [float(v["close"]) for v in values]

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

        # Stale candle guard
        try:
            candle_dt = datetime.strptime(candle_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - candle_dt).total_seconds() / 3600
            if age_hours > 2:
                print(f"  {name}: candle too old ({age_hours:.1f}h) — skipping")
                return None
        except Exception:
            pass

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

        # Rule 1: strict ATR hard block
        atr_ratio = candle_range / atr_now
        if atr_ratio < ATR_MULTIPLIER:
            print(f"  {name}: ATR {atr_ratio:.2f}x < {ATR_MULTIPLIER}x — blocked")
            return None

        # Rule 2: volume above MA
        is_above_vol_ma = (volume_now > vol_ma) if USE_VOLUME_FILTER else True

        # Rule 3: volume spike
        vol_ratio = (volume_now / vol_ma) if vol_ma > 0 else 0
        is_spike  = (vol_ratio >= VOLUME_SPIKE_MULTIPLIER) if USE_VOLUME_SPIKE else True

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
        print(f"  {name}: analyze error — {e}")
        return None

# ─── SCAN ALL PAIRS ───────────────────────────────────────────────────────
def scan():
    print(f"\n[{datetime.now(IRELAND_TZ).strftime('%Y-%m-%d %H:%M')} Irish] Scanning {len(PAIRS)} pairs...")
    signals_found = 0

    for pair in PAIRS:
        name   = pair.replace("/", "")
        values = None

        try:
            values = fetch_pair(pair)

            # If rate limited — wait 60s and retry ONCE
            if values == "RATE_LIMIT":
                print(f"  {name}: waiting 60s then retrying...")
                time.sleep(60)
                values = fetch_pair(pair)
                if values == "RATE_LIMIT":
                    print(f"  {name}: still rate limited after retry — skipping")
                    values = None

        except Exception as e:
            print(f"  {name}: unexpected fetch crash — {e}")
            values = None

        # Wait 20s before next pair regardless
        time.sleep(PAIR_DELAY)

        if values is None or values == "RATE_LIMIT":
            fail_count[name] += 1
            print(f"  {name}: fail count = {fail_count[name]}")
            check_failure_warning(name)
            continue

        # Pair fetched successfully — reset failure state
        if fail_count[name] > 0:
            print(f"  {name}: recovered after {fail_count[name]} failures")
        fail_count[name]  = 0
        fail_warned[name] = False

        result = None
        try:
            result = analyze(pair, values)
        except Exception as e:
            print(f"  {name}: analyze crash — {e}")

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
        "\u2705 <b>Forex Scanner — rate limit fix deployed!</b>\n\n"
        "Changes:\n"
        "- 20s between pairs (safe rate limit buffer)\n"
        "- Rate limit detected and auto-retried\n"
        "- Real errors vs rate limits handled separately\n"
        "- All previous fixes retained\n\n"
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
