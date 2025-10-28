import os
import time
import requests
from datetime import datetime, timedelta, time as dtime
from apscheduler.schedulers.background import BackgroundScheduler

# ================= CONFIG =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TD_API_KEY = os.getenv("TD_API_KEY")

SYMBOL_XAU = "XAU/USD"
SYMBOL_BTC = "BTC/USD"

# Pakistan Time = UTC+5
NY_SESSION_START_PK = dtime(hour=17, minute=0)

XAU_SL_PIPS = 20
BTC_SL_USD = 350
RR = 4
WICK_RATIO_THRESHOLD = 0.4
LOOKBACK_CANDLES = 6


# ================= HELPERS =================
def send_telegram_message(text: str):
    """Send message to Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[DEBUG] Telegram not configured. Message:", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print("[SEND]", r.status_code)
    except Exception as e:
        print("[ERROR] Telegram:", e)


def twelvedata_get_series(symbol, interval="15min", outputsize=100):
    base = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "apikey": TD_API_KEY,
    }
    r = requests.get(base, params=params, timeout=12)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error: {data}")
    return list(reversed(data["values"]))


def parse_candles(raw):
    out = []
    for c in raw:
        out.append(
            {
                "datetime": datetime.fromisoformat(c["datetime"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": float(c.get("volume") or 0),
            }
        )
    return out


# ================= DETECTION =================
def detect_sweep_signal(candles, lookback=LOOKBACK_CANDLES):
    if len(candles) < lookback + 2:
        return {"signal": False, "reason": "not_enough_data"}

    window = candles[-(lookback + 1) :]

    for i in range(1, len(window) - 1):
        c = window[i]
        prev, nxt = window[i - 1], window[i + 1]

        # LONG sweep
        if c["low"] < prev["low"] and c["low"] < nxt["low"]:
            lower_wick = (c["open"] - c["low"]) if c["open"] > c["close"] else (c["close"] - c["low"])
            total = c["high"] - c["low"]
            if lower_wick / total > WICK_RATIO_THRESHOLD and nxt["close"] > nxt["open"]:
                return {"signal": True, "side": "LONG", "sweep": c, "confirm": nxt}

        # SHORT sweep
        if c["high"] > prev["high"] and c["high"] > nxt["high"]:
            upper_wick = (c["high"] - c["close"]) if c["open"] < c["close"] else (c["high"] - c["open"])
            total = c["high"] - c["low"]
            if upper_wick / total > WICK_RATIO_THRESHOLD and nxt["close"] < nxt["open"]:
                return {"signal": True, "side": "SHORT", "sweep": c, "confirm": nxt}

    return {"signal": False, "reason": "no_pattern"}


def compute_liquidity_zones(candles):
    lows = [c["low"] for c in candles]
    highs = [c["high"] for c in candles]
    return {"low": min(lows), "high": max(highs), "last": candles[-1]["close"]}


# ================= TRADE BUILDER =================
def build_trade_plan(symbol, detection):
    sweep, confirm = detection["sweep"], detection["confirm"]
    side = detection["side"]

    if "XAU" in symbol:
        pip_value = 0.01
        sl_distance = XAU_SL_PIPS * pip_value
    else:
        sl_distance = BTC_SL_USD

    if side == "LONG":
        sl = sweep["low"] - sl_distance
        entry = max(confirm["open"], (confirm["close"] + sweep["low"]) / 2)
        rr = entry - sl
        tp = entry + rr * RR
    else:
        sl = sweep["high"] + sl_distance
        entry = min(confirm["open"], (confirm["close"] + sweep["high"]) / 2)
        rr = sl - entry
        tp = entry - rr * RR

    return {
        "side": side,
        "entry": round(entry, 3),
        "sl": round(sl, 3),
        "tp": round(tp, 3),
        "logic": f"Sweep+Confirm ({side}) detected on 15m",
    }


# ================= ANALYSIS =================
def analyze_symbol(symbol):
    try:
        raw = twelvedata_get_series(symbol)
        candles = parse_candles(raw)
        detect = detect_sweep_signal(candles)
        liq = compute_liquidity_zones(candles[-96:])
        res = {"symbol": symbol, "liquidity": liq, "detection": detect}
        if detect["signal"]:
            res["plan"] = build_trade_plan(symbol, detect)
        return res
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


def format_message(analysis):
    if "error" in analysis:
        return f"⚠️ {analysis['symbol']} error: {analysis['error']}"
    if not analysis.get("plan"):
        liq = analysis["liquidity"]
        return (
            f"ℹ️ <b>{analysis['symbol']}</b>\nNo sweep found.\n"
            f"Liquidity: Low {liq['low']}, High {liq['high']}, Last {liq['last']}"
        )
    p = analysis["plan"]
    return (
        f"<b>{analysis['symbol']} Trade Plan</b>\n"
        f"Side: {p['side']}\nEntry: <code>{p['entry']}</code>\n"
        f"SL: <code>{p['sl']}</code>\nTP: <code>{p['tp']}</code>\n"
        f"Logic: {p['logic']}\n---"
    )


# ================= JOBS =================
def job_pre_alert():
    send_telegram_message("🕒 <b>Pre-NY Alert</b>\nScanning XAU & BTC...")
    for sym in [SYMBOL_XAU, SYMBOL_BTC]:
        res = analyze_symbol(sym)
        send_telegram_message(format_message(res))


def job_post_open():
    send_telegram_message("🕒 <b>NY Post-Open Alert</b>\nScanning 15m patterns...")
    for sym in [SYMBOL_XAU, SYMBOL_BTC]:
        res = analyze_symbol(sym)
        send_telegram_message(format_message(res))


def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(job_pre_alert, "cron", hour=11, minute=55)  # PK 16:55 = UTC 11:55
    sched.add_job(job_post_open, "cron", hour=12, minute=5)   # PK 17:05 = UTC 12:05
    sched.start()
    print("Scheduler running...")

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()


# ================= MAIN =================
if __name__ == "__main__":
    print("🚀 Liquidity Matrix Bot Started on Render")
    start_scheduler()
