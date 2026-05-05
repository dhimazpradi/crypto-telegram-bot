import os
import ccxt
import pandas as pd
import requests
import time
import threading

# ─── CONFIG ───────────────────────────────────────────────
TOKEN   = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

INTERVAL = 300  # 5 minutes

PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOGE/USDT",
    "POL/USDT", "LTC/USDT"
]
# ──────────────────────────────────────────────────────────

exchange = ccxt.bitget()

last_signals      = {}
last_signals_lock = threading.Lock()
next_scan_at      = time.time()


# ─── TELEGRAM ─────────────────────────────────────────────

def send(msg, chat_id=None):
    target = chat_id or CHAT_ID
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id":    target,
            "text":       msg,
            "parse_mode": "HTML"
        }, timeout=10)
        data = r.json()
        if data.get("ok"):
            print(f"[SEND OK] msg_id={data['result']['message_id']}")
        else:
            print(f"[SEND FAIL] {data}")
    except Exception as e:
        print(f"[SEND ERROR] {e}")


def get_updates(offset=None):
    url    = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params = {"timeout": 10, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(url, params=params, timeout=15)
        return resp.json()
    except Exception:
        return {"ok": False, "result": []}


# ─── SIGNAL LOGIC ─────────────────────────────────────────

def get_data(symbol, tf="5m"):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    return df


def ema(series, period):
    return series.ewm(span=period).mean()


def check_signal(symbol):
    try:
        df1h = get_data(symbol, "1h")
        df5m = get_data(symbol, "5m")

        df1h["ema50"]  = ema(df1h["close"], 50)
        df1h["ema200"] = ema(df1h["close"], 200)

        if df1h.iloc[-1]["ema50"] > df1h.iloc[-1]["ema200"]:
            trend = "BUY"
        elif df1h.iloc[-1]["ema50"] < df1h.iloc[-1]["ema200"]:
            trend = "SELL"
        else:
            return None

        df5m["ema50"] = ema(df5m["close"], 50)
        last      = df5m.iloc[-1]
        prev      = df5m.iloc[-2]
        candle_ts = int(last["time"])

        # Must be at pullback zone
        if trend == "BUY"  and last["close"] > last["ema50"]: return None
        if trend == "SELL" and last["close"] < last["ema50"]: return None

        # Liquidity sweep (fake break)
        if trend == "BUY" and last["low"] < prev["low"]:
            entry = last["close"]
            sl    = last["low"]
            tp    = entry + (entry - sl) * 2
        elif trend == "SELL" and last["high"] > prev["high"]:
            entry = last["close"]
            sl    = last["high"]
            tp    = entry - (sl - entry) * 2
        else:
            return None

        return trend, entry, sl, tp, candle_ts

    except Exception as e:
        print(f"[ERROR] {symbol}: {e}")
        return None


def scan_and_alert(max_signals=3, chat_id=None):
    count = 0
    for pair in PAIRS:
        signal = check_signal(pair)
        if signal:
            trend, entry, sl, tp, candle_ts = signal
            key = f"{pair}-{trend}-{candle_ts}"

            with last_signals_lock:
                if key in last_signals:
                    continue
                if len(last_signals) > 200:
                    oldest = next(iter(last_signals))
                    del last_signals[oldest]
                last_signals[key] = True

            emoji = "📈" if trend == "BUY" else "📉"
            msg = (
                f"{emoji} <b>SIGNAL ALERT</b>\n\n"
                f"<b>PAIR:</b>  {pair}\n"
                f"<b>TYPE:</b>  {trend}\n\n"
                f"<b>ENTRY:</b> {round(entry, 4)}\n"
                f"<b>SL:</b>    {round(sl, 4)}\n"
                f"<b>TP:</b>    {round(tp, 4)}\n\n"
                f"<b>RR:</b> 1:2"
            )
            send(msg, chat_id=chat_id)
            print(f"  Signal: {pair} {trend} @ {round(entry, 4)}")
            count += 1
            if count >= max_signals:
                break
    return count


# ─── COMMAND HANDLERS ─────────────────────────────────────

def handle_status(chat_id):
    with last_signals_lock:
        active = list(last_signals.keys())
    secs = max(0, int(next_scan_at - time.time()))
    lines = "\n".join(f"  • {s}" for s in active) if active else "  No active signals yet."
    send(
        f"📊 <b>Bot Status</b>\n\n"
        f"<b>Active signals ({len(active)}):</b>\n{lines}\n\n"
        f"<b>Next scan in:</b> {secs // 60}m {secs % 60:02d}s\n"
        f"<b>Interval:</b> every 5 minutes\n"
        f"<b>Pairs watched:</b> {len(PAIRS)}",
        chat_id=chat_id
    )


def handle_help(chat_id):
    send(
        "🤖 <b>Crypto Signal Bot Commands</b>\n\n"
        "/status — Show active signals & next scan time\n"
        "/scan   — Trigger an instant scan now\n"
        "/pairs  — List all monitored pairs\n"
        "/reset  — Clear signal history\n"
        "/help   — Show this help message",
        chat_id=chat_id
    )


def handle_pairs(chat_id):
    lines = "\n".join(f"  • {p}" for p in PAIRS)
    send(f"📋 <b>Monitored Pairs ({len(PAIRS)})</b>\n\n{lines}", chat_id=chat_id)


def handle_reset(chat_id):
    with last_signals_lock:
        count = len(last_signals)
        last_signals.clear()
    send(
        f"♻️ Signal history cleared. {count} signal(s) removed.\n"
        "All pairs will be re-evaluated on the next scan.",
        chat_id=chat_id
    )


def handle_scan(chat_id):
    send("🔍 Scanning all pairs now...", chat_id=chat_id)
    count = scan_and_alert(chat_id=chat_id)
    if count == 0:
        send("✅ Scan complete. No new signals found right now.", chat_id=chat_id)
    else:
        send(f"✅ Scan complete. {count} signal(s) sent.", chat_id=chat_id)


# ─── COMMAND LISTENER (background thread) ─────────────────

def command_listener():
    offset = None
    print("[CMD] Command listener started.")
    while True:
        try:
            data = get_updates(offset=offset)
            if data.get("ok") and data.get("result"):
                for update in data["result"]:
                    offset  = update["update_id"] + 1
                    msg     = update.get("message", {})
                    text    = msg.get("text", "").strip().lower()
                    chat_id = msg.get("chat", {}).get("id")
                    if not text or not chat_id:
                        continue
                    cmd = text.split()[0]
                    print(f"[CMD] {cmd} from {chat_id}")
                    if   cmd in ("/status",):        handle_status(chat_id)
                    elif cmd in ("/help", "/start"): handle_help(chat_id)
                    elif cmd in ("/pairs",):         handle_pairs(chat_id)
                    elif cmd in ("/reset",):         handle_reset(chat_id)
                    elif cmd in ("/scan",):          handle_scan(chat_id)
        except Exception as e:
            print(f"[CMD ERROR] {e}")
            time.sleep(5)


# ─── MAIN ─────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[CONFIG] TOKEN  : {TOKEN[:15]}...")
    print(f"[CONFIG] CHAT_ID: {CHAT_ID}")
    print(f"[CONFIG] Pairs  : {len(PAIRS)}")
    print(f"[CONFIG] Every  : {INTERVAL}s ({INTERVAL // 60} min)")

    send(
        "🤖 <b>Crypto Signal Bot started!</b>\n\n"
        f"Monitoring <b>{len(PAIRS)} pairs</b> every <b>5 minutes</b>.\n"
        "Send /help to see available commands."
    )

    threading.Thread(target=command_listener, daemon=True).start()

    while True:
        next_scan_at = time.time() + INTERVAL
        print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Scanning {len(PAIRS)} pairs...")
        count = scan_and_alert()
        print("  No new signals." if count == 0 else f"  {count} signal(s) sent.")
        print(f"  Next scan in {INTERVAL // 60} min...")
        time.sleep(INTERVAL)
