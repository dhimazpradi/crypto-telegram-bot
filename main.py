import os
import ccxt
import pandas as pd
import requests
import time
import threading
from datetime import datetime, timezone

# =========================================================
# CONFIG
# =========================================================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

INTERVAL = 300
MAX_SIGNALS_PER_DAY = 3

PAIRS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "ADA/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "DOGE/USDT",
    "POL/USDT",
    "LTC/USDT"
]

exchange = ccxt.bitget({
    "enableRateLimit": True
})

daily_signals = {}
daily_lock = threading.Lock()

# =========================================================
# DAILY TRACKING
# =========================================================

def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def signals_today():
    day = today_utc()

    with daily_lock:
        return sum(1 for k in daily_signals if k.startswith(day))

def already_sent(pair, direction):
    key = f"{today_utc()}|{pair}|{direction}"

    with daily_lock:
        return key in daily_signals

def mark_sent(pair, direction):
    key = f"{today_utc()}|{pair}|{direction}"
    today = today_utc()

    with daily_lock:

        stale = [
            k for k in daily_signals
            if not k.startswith(today)
        ]

        for k in stale:
            del daily_signals[k]

        daily_signals[key] = True

# =========================================================
# TELEGRAM
# =========================================================

def send(msg, chat_id=None):

    try:
        target = chat_id if chat_id else CHAT_ID

        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

        response = requests.post(
            url,
            data={
                "chat_id": target,
                "text": msg,
                "parse_mode": "HTML"
            },
            timeout=10
        )

        print("====================================")
        print("TELEGRAM STATUS :", response.status_code)
        print("====================================")

        return response.status_code == 200

    except Exception as e:
        print(f"[SEND ERROR] {e}")
        return False

def get_updates(offset=None):

    try:
        url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

        params = {
            "timeout": 10,
            "allowed_updates": ["message"]
        }

        if offset:
            params["offset"] = offset

        response = requests.get(
            url,
            params=params,
            timeout=15
        )

        return response.json()

    except Exception as e:
        print(f"[GET UPDATE ERROR] {e}")
        return {"ok": False, "result": []}

# =========================================================
# DATA
# =========================================================

def get_data(symbol, tf, limit=200):

    ohlcv = exchange.fetch_ohlcv(
        symbol,
        timeframe=tf,
        limit=limit
    )

    df = pd.DataFrame(
        ohlcv,
        columns=[
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    return df

# =========================================================
# INDICATORS
# =========================================================

def ema(series, period):
    return series.ewm(
        span=period,
        adjust=False
    ).mean()

def atr(df, period=14):

    prev_close = df["close"].shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)

    return tr.rolling(period).mean()

def rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0).rolling(period).mean()

    loss = (-delta.clip(upper=0)).rolling(period).mean()

    rs = gain / loss.replace(0, 1e-10)

    return 100 - (100 / (1 + rs))

# =========================================================
# MARKET STRUCTURE
# =========================================================

def market_structure(df):

    highs = df["high"].tail(20).tolist()
    lows = df["low"].tail(20).tolist()

    recent_high = max(highs[-10:])
    previous_high = max(highs[:-10])

    recent_low = min(lows[-10:])
    previous_low = min(lows[:-10])

    # bullish structure
    if recent_high > previous_high and recent_low > previous_low:
        return "BULLISH"

    # bearish structure
    if recent_high < previous_high and recent_low < previous_low:
        return "BEARISH"

    return "RANGE"

# =========================================================
# HIGH PROBABILITY SIGNAL
# =========================================================

def check_signal(symbol):

    try:

        # =================================================
        # HTF - 1H STRUCTURE
        # =================================================

        df1h = get_data(symbol, "1h", 200)

        structure = market_structure(df1h)

        if structure == "RANGE":
            return None

        df1h["ema50"] = ema(df1h["close"], 50)

        htf_last = df1h.iloc[-1]

        htf_support = df1h.tail(20)["low"].min()
        htf_resistance = df1h.tail(20)["high"].max()

        # =================================================
        # LTF - 5M ENTRY
        # =================================================

        df5m = get_data(symbol, "5m", 120)

        df5m["ema50"] = ema(df5m["close"], 50)
        df5m["atr14"] = atr(df5m, 14)
        df5m["rsi14"] = rsi(df5m["close"], 14)

        last = df5m.iloc[-1]
        prev = df5m.iloc[-2]

        atr_value = last["atr14"]

        if pd.isna(atr_value) or atr_value <= 0:
            return None

        entry = float(last["close"])

        # =================================================
        # BUY SETUP
        # =================================================

        if structure == "BULLISH":

            # pullback area
            if entry > htf_last["ema50"] * 1.01:
                return None

            # RSI confirmation
            if not (35 <= last["rsi14"] <= 60):
                return None

            # liquidity sweep
            if last["low"] >= prev["low"]:
                return None

            # bullish rejection
            candle_body = abs(last["close"] - last["open"])

            lower_wick = min(last["open"], last["close"]) - last["low"]

            if lower_wick < candle_body:
                return None

            trend = "BUY"

            sl = htf_support - (atr_value * 0.5)

            risk = entry - sl

            if risk <= 0:
                return None

            tp1 = entry + (risk * 1.5)
            tp2 = min(
                entry + (risk * 2),
                htf_resistance
            )

            tp3 = entry + (risk * 3)

        # =================================================
        # SELL SETUP
        # =================================================

        elif structure == "BEARISH":

            # pullback area
            if entry < htf_last["ema50"] * 0.99:
                return None

            # RSI confirmation
            if not (40 <= last["rsi14"] <= 65):
                return None

            # liquidity sweep
            if last["high"] <= prev["high"]:
                return None

            # bearish rejection
            candle_body = abs(last["close"] - last["open"])

            upper_wick = last["high"] - max(last["open"], last["close"])

            if upper_wick < candle_body:
                return None

            trend = "SELL"

            sl = htf_resistance + (atr_value * 0.5)

            risk = sl - entry

            if risk <= 0:
                return None

            tp1 = entry - (risk * 1.5)

            tp2 = max(
                entry - (risk * 2),
                htf_support
            )

            tp3 = entry - (risk * 3)

        else:
            return None

        # =================================================
        # MINIMUM RR CHECK
        # =================================================

        rr = abs(tp2 - entry) / risk

        if rr < 2:
            return None

        return (
            trend,
            round(entry, 6),
            round(sl, 6),
            round(tp1, 6),
            round(tp2, 6),
            round(tp3, 6),
            round((risk / entry) * 100, 3),
            structure
        )

    except Exception as e:
        print(f"[SIGNAL ERROR] {symbol} : {e}")
        return None

# =========================================================
# SCAN & ALERT
# =========================================================

def scan_and_alert(force=False, chat_id=None):

    target_chat = chat_id if chat_id else CHAT_ID

    count = 0

    if not force and signals_today() >= MAX_SIGNALS_PER_DAY:
        return 0

    for pair in PAIRS:

        print(f"[SCAN] {pair}")

        signal = check_signal(pair)

        if not signal:
            continue

        (
            trend,
            entry,
            sl,
            tp1,
            tp2,
            tp3,
            risk_pct,
            structure
        ) = signal

        if not force and already_sent(pair, trend):
            continue

        if not force:
            mark_sent(pair, trend)

        emoji = "📈" if trend == "BUY" else "📉"

        msg = (
            f"{emoji} <b>HIGH PROBABILITY SIGNAL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>PAIR :</b> {pair}\n"
            f"<b>TYPE :</b> {trend}\n"
            f"<b>HTF STRUCTURE :</b> {structure}\n\n"

            f"<b>ENTRY :</b> {entry}\n"
            f"<b>SL :</b> {sl}\n\n"

            f"<b>TP1 :</b> {tp1} (1:1.5)\n"
            f"<b>TP2 :</b> {tp2} (1:2)\n"
            f"<b>TP3 :</b> {tp3} (1:3)\n\n"

            f"<b>Risk :</b> {risk_pct}%\n"
            f"<b>Signals Today :</b> {signals_today()+1}/{MAX_SIGNALS_PER_DAY}"
        )

        success = send(msg, chat_id=target_chat)

        if success:
            print(f"[SIGNAL SENT] {pair} {trend}")
            count += 1

    return count

# =========================================================
# TELEGRAM COMMANDS
# =========================================================

def handle_status(chat_id):

    send(
        f"📊 <b>BOT STATUS</b>\n\n"
        f"Signals Today : {signals_today()}/{MAX_SIGNALS_PER_DAY}\n"
        f"Pairs : {len(PAIRS)}\n"
        f"Scan Interval : {INTERVAL // 60} minutes",
        chat_id=chat_id
    )

def handle_help(chat_id):

    send(
        "🤖 <b>COMMAND LIST</b>\n\n"
        "/status - bot status\n"
        "/scan - instant scan\n"
        "/help - show commands",
        chat_id=chat_id
    )

def handle_scan(chat_id):

    send(
        "🔍 Running instant scan...",
        chat_id=chat_id
    )

    count = scan_and_alert(
        force=True,
        chat_id=chat_id
    )

    if count == 0:

        send(
            "❌ No valid setup found.",
            chat_id=chat_id
        )

    else:

        send(
            f"✅ {count} setup(s) found.",
            chat_id=chat_id
        )

# =========================================================
# COMMAND LISTENER
# =========================================================

def command_listener():

    offset = None

    while True:

        try:

            data = get_updates(offset)

            if data.get("ok"):

                for update in data.get("result", []):

                    offset = update["update_id"] + 1

                    msg = update.get("message", {})

                    text = msg.get("text", "").strip().lower()

                    chat_id = msg.get("chat", {}).get("id")

                    if not text:
                        continue

                    print(f"[COMMAND] {text}")

                    if text == "/status":
                        handle_status(chat_id)

                    elif text == "/scan":
                        handle_scan(chat_id)

                    elif text in ["/start", "/help"]:
                        handle_help(chat_id)

        except Exception as e:
            print(f"[COMMAND ERROR] {e}")

        time.sleep(3)

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    print("====================================")
    print("BOT STARTED")
    print("====================================")

    send("🚀 BOT ONLINE & RUNNING")

    threading.Thread(
        target=command_listener,
        daemon=True
    ).start()

    while True:

        print("====================================")
        print(f"[SCAN START] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("====================================")

        found = scan_and_alert()

        if found == 0:
            print("[INFO] No valid setup.")

        print(f"[NEXT SCAN] {INTERVAL // 60} minutes")

        time.sleep(INTERVAL)
