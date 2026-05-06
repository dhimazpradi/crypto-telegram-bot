import os
import ccxt
import pandas as pd
import requests
import time
import threading
from datetime import datetime, timezone

# ─── CONFIG ───────────────────────────────────────────────
import os
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

INTERVAL            = 300   # scan every 5 minutes
MAX_SIGNALS_PER_DAY = 3     # max high-quality trades per day

PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOGE/USDT",
    "POL/USDT", "LTC/USDT"
]
# ──────────────────────────────────────────────────────────

exchange = ccxt.bitget()

# Signals sent today — key = "YYYY-MM-DD|PAIR|DIRECTION"
daily_signals      = {}
daily_signals_lock = threading.Lock()
next_scan_at       = time.time()


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
    key   = f"{today_utc()}|{pair}|{trend}"
    today = today_utc()
    with daily_signals_lock:
        stale = [k for k in daily_signals if not k.startswith(today)]
        for k in stale:
            del daily_signals[k]
        daily_signals[key] = True


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


# ─── INDICATORS ───────────────────────────────────────────

def get_data(symbol, tf="5m"):
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","volume"])
    return df

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_atr(df, period=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


# ─── HIGH-PROBABILITY SIGNAL CHECK ────────────────────────

def check_signal(symbol):
    """
    Returns a signal tuple only when ALL filters pass:
      1. 1H EMA 50/200 trend with >0.3% separation
      2. 5M price in clean pullback zone (within 2×ATR of EMA50)
      3. RSI in confirmation range
      4. Volume spike > 1.2× 20-period average
      5. Liquidity sweep (fake break of prev candle's low/high)
      6. SL distance at least 0.2% of price
      7. Actual room for 1:2 RR based on recent structure
    """
    try:
        df1h = get_data(symbol, "1h")
        df5m = get_data(symbol, "5m")

        # ── FILTER 1: 1H trend + strength ────────────────────
        df1h["ema50"]  = calc_ema(df1h["close"], 50)
        df1h["ema200"] = calc_ema(df1h["close"], 200)

        e50  = df1h.iloc[-1]["ema50"]
        e200 = df1h.iloc[-1]["ema200"]
        sep  = abs(e50 - e200) / e200   # must be > 0.3%

        if e50 > e200 and sep > 0.003:
            trend = "BUY"
        elif e50 < e200 and sep > 0.003:
            trend = "SELL"
        else:
            return None   # weak or unclear trend

        # ── FILTER 2: 5M indicators ───────────────────────────
        df5m["ema50"]    = calc_ema(df5m["close"], 50)
        df5m["atr14"]    = calc_atr(df5m, 14)
        df5m["vol_ma20"] = df5m["volume"].rolling(20).mean()
        df5m["rsi14"]    = calc_rsi(df5m["close"], 14)

        last    = df5m.iloc[-1]
        prev    = df5m.iloc[-2]
        atr_val = last["atr14"]

        if pd.isna(atr_val) or atr_val == 0:
            return None

        # ── FILTER 3: Clean pullback zone ─────────────────────
        # Price must be near EMA50 (within 2×ATR) and on correct side
        if abs(last["close"] - last["ema50"]) > 2.0 * atr_val:
            return None
        if trend == "BUY"  and last["close"] > last["ema50"]: return None
        if trend == "SELL" and last["close"] < last["ema50"]: return None

        # ── FILTER 4: RSI confirmation ─────────────────────────
        rsi = last["rsi14"]
        if pd.isna(rsi): return None
        if trend == "BUY"  and not (30 <= rsi <= 55): return None
        if trend == "SELL" and not (45 <= rsi <= 70): return None

        # ── FILTER 5: Volume spike ─────────────────────────────
        vol_avg = last["vol_ma20"]
        if pd.isna(vol_avg) or vol_avg == 0: return None
        if last["volume"] < 1.2 * vol_avg:   return None

        # ── FILTER 6: Liquidity sweep ──────────────────────────
        if trend == "BUY"  and last["low"]  >= prev["low"]:  return None
        if trend == "SELL" and last["high"] <= prev["high"]: return None

        # ── Entry ─────────────────────────────────────────────
        entry = last["close"]

        # ── SL: swing structure + ATR buffer ──────────────────
        lookback = df5m.iloc[-6:-1]   # 5 candles before current

        if trend == "BUY":
            sl   = lookback["low"].min() - (0.5 * atr_val)
            dist = entry - sl
        else:
            sl   = lookback["high"].max() + (0.5 * atr_val)
            dist = sl - entry

        # ── FILTER 7: SL distance minimum ─────────────────────
        if dist <= 0 or (dist / entry) < 0.002:
            return None

        # ── FILTER 8: Enough room for 1:2 RR ──────────────────
        if trend == "BUY":
            max_reward = df5m.iloc[-20:]["high"].max() - entry
        else:
            max_reward = entry - df5m.iloc[-20:]["low"].min()

        if max_reward / dist < 2.0:
            return None   # structure blocks reward → skip

        # ── TP levels ─────────────────────────────────────────
        if trend == "BUY":
            tp1 = round(entry + dist * 1.5, 6)   # 1:1.5 — scale out
            tp2 = round(entry + dist * 2.0, 6)   # 1:2   — main
            tp3 = round(entry + dist * 3.0, 6)   # 1:3   — extended
        else:
            tp1 = round(entry - dist * 1.5, 6)
            tp2 = round(entry - dist * 2.0, 6)
            tp3 = round(entry - dist * 3.0, 6)

        sl       = round(sl,    6)
        entry    = round(entry, 6)
        risk_pct = round(dist / entry * 100, 3)

        return trend, entry, sl, tp1, tp2, tp3, risk_pct

    except Exception as e:
        print(f"[ERROR] {symbol}: {e}")
        return None


# ─── SCAN & ALERT ─────────────────────────────────────────

def scan_and_alert(force=False, chat_id=None):
    """
    Scan all pairs. Only alerts on high-probability setups.
    force=True: bypass daily limit (used by /scan command).
    """
    sent_to = chat_id or CHAT_ID
    count   = 0

    if not force and signals_today() >= MAX_SIGNALS_PER_DAY:
        print(f"  Daily limit reached ({MAX_SIGNALS_PER_DAY}). Skipping.")
        return 0

    for pair in PAIRS:
        if not force and signals_today() >= MAX_SIGNALS_PER_DAY:
            break

        signal = check_signal(pair)
        if not signal:
            continue

        trend, entry, sl, tp1, tp2, tp3, risk_pct = signal

        if not force and already_sent(pair, trend):
            continue

        if not force:
            mark_sent(pair, trend)

        cnt     = signals_today()
        emoji   = "📈" if trend == "BUY" else "📉"
        sl_note = "below swing low" if trend == "BUY" else "above swing high"

        msg = (
            f"{emoji} <b>HIGH PROBABILITY SIGNAL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>PAIR  :</b> {pair}\n"
            f"<b>TYPE  :</b> {trend}\n\n"
            f"<b>ENTRY :</b> {entry}\n"
            f"<b>SL    :</b> {sl}  <i>({sl_note})</i>\n\n"
            f"<b>TP1   :</b> {tp1}  <i>(1:1.5 — scale out)</i>\n"
            f"<b>TP2   :</b> {tp2}  <i>(1:2   — main target)</i>\n"
            f"<b>TP3   :</b> {tp3}  <i>(1:3   — extended)</i>\n\n"
            f"<b>Risk  :</b> {risk_pct}% of entry\n"
            f"<b>Trades today:</b> {cnt}/{MAX_SIGNALS_PER_DAY}"
        )
        send(msg, chat_id=sent_to)
        print(f"  ✅ {pair} {trend} | Entry={entry} SL={sl} TP2={tp2} ({cnt}/{MAX_SIGNALS_PER_DAY} today)")
        count += 1

    return count


# ─── COMMAND HANDLERS ─────────────────────────────────────

def handle_status(chat_id):
    today = today_utc()
    cnt   = signals_today()
    secs  = max(0, int(next_scan_at - time.time()))
    with daily_signals_lock:
        keys = [k for k in daily_signals if k.startswith(today)]
    lines = "\n".join(f"  • {k.split('|')[1]} {k.split('|')[2]}" for k in keys) or "  None yet."
    send(
        f"📊 <b>Bot Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Signals today:</b> {cnt}/{MAX_SIGNALS_PER_DAY}\n"
        f"{lines}\n\n"
        f"<b>Next scan in:</b> {secs // 60}m {secs % 60:02d}s\n"
        f"<b>Pairs watched:</b> {len(PAIRS)}",
        chat_id=chat_id
    )

def handle_help(chat_id):
    send(
        "🤖 <b>Crypto Signal Bot Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "/status — Today's signals & next scan time\n"
        "/scan   — Force an instant scan right now\n"
        "/pairs  — List all monitored pairs\n"
        "/reset  — Reset today's signal history\n"
        "/help   — Show this message",
        chat_id=chat_id
    )

def handle_pairs(chat_id):
    lines = "\n".join(f"  • {p}" for p in PAIRS)
    send(f"📋 <b>Monitored Pairs ({len(PAIRS)})</b>\n\n{lines}", chat_id=chat_id)

def handle_reset(chat_id):
    today = today_utc()
    with daily_signals_lock:
        keys = [k for k in list(daily_signals) if k.startswith(today)]
        for k in keys:
            del daily_signals[k]
    send(f"♻️ Today's signal history cleared ({len(keys)} removed). Fresh start!", chat_id=chat_id)

def handle_scan(chat_id):
    send("🔍 Running instant scan (ignoring daily limit)...", chat_id=chat_id)
    count = scan_and_alert(force=True, chat_id=chat_id)
    if count == 0:
        send("✅ Scan done. No high-probability setups found right now.", chat_id=chat_id)
    else:
        send(f"✅ Scan done. {count} signal(s) sent.", chat_id=chat_id)


# ─── COMMAND LISTENER ─────────────────────────────────────

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
    print(f"[CONFIG] Max/day: {MAX_SIGNALS_PER_DAY}")

    send(
        "🤖 <b>Crypto Signal Bot started!</b>\n\n"
        f"Scanning <b>{len(PAIRS)} pairs</b> every <b>5 min</b>.\n"
        f"Max <b>{MAX_SIGNALS_PER_DAY} high-probability signals/day</b>.\n"
        "Min RR: <b>1:2</b>  •  Filters: EMA trend + RSI + Volume + Sweep\n\n"
        "Send /help to see available commands."
    )

    threading.Thread(target=command_listener, daemon=True).start()

    while True:
        next_scan_at = time.time() + INTERVAL
        cnt = signals_today()
        print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Scanning... ({cnt}/{MAX_SIGNALS_PER_DAY} today)")

        if cnt >= MAX_SIGNALS_PER_DAY:
            print("  Daily limit reached. Waiting.")
        else:
            found = scan_and_alert()
            if found == 0:
                print("  No high-probability setups this round.")

        print(f"  Next scan in {INTERVAL // 60} min...")
        time.sleep(INTERVAL)
