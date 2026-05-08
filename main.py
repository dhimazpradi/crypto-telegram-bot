import os
import ccxt
import pandas as pd
import requests
import time
import threading
from datetime import datetime, timezone

# ─── CONFIG ───────────────────────────────────────────────

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

INTERVAL = 300  # 5 menit
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
    'enableRateLimit': True
})

daily_signals = {}
daily_signals_lock = threading.Lock()
next_scan_at = time.time()

# ─── DAILY TRACKING ───────────────────────────────────────

def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def signals_today():
    day = today_utc()

    with daily_signals_lock:
        return sum(1 for k in daily_signals if k.startswith(day))

def already_sent(pair, trend):
    key = f"{today_utc()}|{pair}|{trend}"

    with daily_signals_lock:
        return key in daily_signals

def mark_sent(pair, trend):
    key = f"{today_utc()}|{pair}|{trend}"
    today = today_utc()

    with daily_signals_lock:
        stale = [k for k in daily_signals if not k.startswith(today)]

        for k in stale:
            del daily_signals[k]

        daily_signals[key] = True

# ─── TELEGRAM ─────────────────────────────────────────────

def send(msg, chat_id=None):
    try:
        target_chat = chat_id if chat_id else CHAT_ID

        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

        response = requests.post(
            url,
            data={
                "chat_id": target_chat,
                "text": msg,
                "parse_mode": "HTML"
            },
            timeout=10
        )

        data = response.json()

        print("========== TELEGRAM DEBUG ==========")
        print("STATUS :", response.status_code)
        print("RESPONSE :", data)
        print("====================================")

        return data.get("ok", False)

    except Exception as e:
        print(f"[SEND ERROR] {e}")
        return False

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

    params = {
        "timeout": 10,
        "allowed_updates": ["message"]
    }

    if offset:
        params["offset"] = offset

    try:
        response = requests.get(url, params=params, timeout=15)
        return response.json()

    except Exception as e:
        print(f"[GET UPDATES ERROR] {e}")
        return {"ok": False, "result": []}

# ─── INDICATORS ───────────────────────────────────────────

def get_data(symbol, tf="5m"):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=120)

    df = pd.DataFrame(
        ohlcv,
        columns=["time", "open", "high", "low", "close", "volume"]
    )

    return df

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_atr(df, period=14):
    prev_close = df["close"].shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)

    return tr.rolling(period).mean()

def calc_rsi(series, period=14):
    delta = series.diff()

    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()

    rs = gain / loss.replace(0, 1e-10)

    return 100 - (100 / (1 + rs))

# ─── HIGH PROBABILITY SIGNAL ──────────────────────────────

def check_signal(symbol):
    try:
        # =========================
        # 1H BIGGER PICTURE
        # =========================

        df1h = get_data(symbol, "1h")

        df1h["ema50"] = calc_ema(df1h["close"], 50)
        df1h["ema200"] = calc_ema(df1h["close"], 200)

        e50 = df1h.iloc[-1]["ema50"]
        e200 = df1h.iloc[-1]["ema200"]

        separation = abs(e50 - e200) / e200

        if e50 > e200 and separation > 0.002:
            trend = "BUY"

        elif e50 < e200 and separation > 0.002:
            trend = "SELL"

        else:
            return None

        # =========================
        # 5M ENTRY
        # =========================

        df5m = get_data(symbol, "5m")

        df5m["ema50"] = calc_ema(df5m["close"], 50)
        df5m["atr14"] = calc_atr(df5m, 14)
        df5m["rsi14"] = calc_rsi(df5m["close"], 14)
        df5m["vol_ma20"] = df5m["volume"].rolling(20).mean()

        last = df5m.iloc[-1]
        prev = df5m.iloc[-2]

        atr = last["atr14"]

        if pd.isna(atr) or atr == 0:
            return None

        # =========================
        # PULLBACK FILTER
        # =========================

        distance = abs(last["close"] - last["ema50"]) / last["close"]

        if distance > 0.004:
            return None

        # =========================
        # RSI FILTER
        # =========================

        rsi = last["rsi14"]

        if pd.isna(rsi):
            return None

        if trend == "BUY":
            if not (35 <= rsi <= 60):
                return None

        if trend == "SELL":
            if not (40 <= rsi <= 65):
                return None

        # =========================
        # VOLUME FILTER
        # =========================

        vol_avg = last["vol_ma20"]

        if pd.isna(vol_avg) or vol_avg == 0:
            return None

        if last["volume"] < 1.05 * vol_avg:
            return None

        # =========================
        # LIQUIDITY SWEEP
        # =========================

        if trend == "BUY":
            if last["low"] >= prev["low"]:
                return None

        if trend == "SELL":
            if last["high"] <= prev["high"]:
                return None

        # =========================
        # ENTRY
        # =========================

        entry = float(last["close"])

        lookback = df5m.iloc[-6:-1]

        if trend == "BUY":
            sl = float(lookback["low"].min() - (0.5 * atr))
            risk = entry - sl

        else:
            sl = float(lookback["high"].max() + (0.5 * atr))
            risk = sl - entry

        if risk <= 0:
            return None

        # MINIMUM RISK SIZE
        if (risk / entry) < 0.0015:
            return None

        # =========================
        # TP
        # =========================

        if trend == "BUY":
            tp1 = entry + (risk * 1.5)
            tp2 = entry + (risk * 2)
            tp3 = entry + (risk * 3)

        else:
            tp1 = entry - (risk * 1.5)
            tp2 = entry - (risk * 2)
            tp3 = entry - (risk * 3)

        return (
            trend,
            round(entry, 6),
            round(sl, 6),
            round(tp1, 6),
            round(tp2, 6),
            round(tp3, 6),
            round((risk / entry) * 100, 3)
        )

    except Exception as e:
        print(f"[CHECK SIGNAL ERROR] {symbol}: {e}")
        return None

# ─── SCAN & ALERT ─────────────────────────────────────────

def scan_and_alert(force=False, chat_id=None):
    target_chat = chat_id if chat_id else CHAT_ID

    count = 0

    if not force and signals_today() >= MAX_SIGNALS_PER_DAY:
        print("[INFO] Daily limit reached.")
        return 0

    for pair in PAIRS:

        print(f"[SCAN] {pair}")

        if not force and signals_today() >= MAX_SIGNALS_PER_DAY:
            break

        signal = check_signal(pair)

        if not signal:
            continue

        trend, entry, sl, tp1, tp2, tp3, risk_pct = signal

        if not force and already_sent(pair, trend):
            print(f"[SKIP] {pair} {trend} already sent today")
            continue

        if not force:
            mark_sent(pair, trend)

        emoji = "📈" if trend == "BUY" else "📉"

        msg = (
            f"{emoji} <b>HIGH PROBABILITY SIGNAL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>PAIR :</b> {pair}\n"
            f"<b>TYPE :</b> {trend}\n\n"

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
            print(f"[ALERT SENT] {pair} {trend}")
            count += 1

        else:
            print(f"[FAILED ALERT] {pair}")

    return count

# ─── COMMANDS ─────────────────────────────────────────────

def handle_status(chat_id):
    send(
        f"📊 <b>BOT STATUS</b>\n\n"
        f"Signals today: {signals_today()}/{MAX_SIGNALS_PER_DAY}\n"
        f"Pairs watched: {len(PAIRS)}\n"
        f"Interval: {INTERVAL // 60} min",
        chat_id=chat_id
    )

def handle_help(chat_id):
    send(
        "🤖 <b>COMMAND LIST</b>\n\n"
        "/status - bot status\n"
        "/scan - instant scan\n"
        "/pairs - monitored pairs\n"
        "/help - command list",
        chat_id=chat_id
    )

def handle_pairs(chat_id):
    pair_text = "\n".join([f"• {p}" for p in PAIRS])

    send(
        f"📋 <b>WATCHLIST</b>\n\n{pair_text}",
        chat_id=chat_id
    )

def handle_scan(chat_id):
    send("🔍 Running instant scan...", chat_id=chat_id)

    count = scan_and_alert(force=True, chat_id=chat_id)

    if count == 0:
        send("❌ No setup found.", chat_id=chat_id)

    else:
        send(f"✅ {count} signal(s) found.", chat_id=chat_id)

# ─── COMMAND LISTENER ─────────────────────────────────────

def command_listener():
    offset = None

    print("[COMMAND LISTENER STARTED]")

    while True:
        try:
            data = get_updates(offset=offset)

            if data.get("ok") and data.get("result"):

                for update in data["result"]:

                    offset = update["update_id"] + 1

                    msg = update.get("message", {})

                    text = msg.get("text", "").strip().lower()

                    chat_id = msg.get("chat", {}).get("id")

                    if not text or not chat_id:
                        continue

                    print(f"[COMMAND] {text}")

                    cmd = text.split()[0]

                    if cmd == "/status":
                        handle_status(chat_id)

                    elif cmd in ["/help", "/start"]:
                        handle_help(chat_id)

                    elif cmd == "/pairs":
                        handle_pairs(chat_id)

                    elif cmd == "/scan":
                        handle_scan(chat_id)

        except Exception as e:
            print(f"[COMMAND ERROR] {e}")

        time.sleep(3)

# ─── MAIN ─────────────────────────────────────────────────

if __name__ == "__main__":

    print("====================================")
    print("BOT STARTING...")
    print("====================================")

    print(f"TOKEN : {TOKEN[:10]}...")
    print(f"CHAT ID : {CHAT_ID}")

    # TEST MESSAGE
    send("🚀 BOT ONLINE & RUNNING")

    # COMMAND THREAD
    threading.Thread(
        target=command_listener,
        daemon=True
    ).start()

    while True:

        next_scan_at = time.time() + INTERVAL

        print("====================================")
        print(f"[SCAN START] {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("====================================")

        today_count = signals_today()

        print(f"[TODAY SIGNALS] {today_count}/{MAX_SIGNALS_PER_DAY}")

        if today_count >= MAX_SIGNALS_PER_DAY:
            print("[INFO] Daily limit reached.")

        else:
            found = scan_and_alert()

            if found == 0:
                print("[INFO] No valid setup found.")

        print(f"[NEXT SCAN] {INTERVAL // 60} minutes")
        print("====================================")

        time.sleep(INTERVAL)
