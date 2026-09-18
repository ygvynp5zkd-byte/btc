"""
BTC -> Altcoin Lag Scanner (Telegram Bot Edition)
==================================================
Ports the exact logic of the "BTC → Altcoin Lag Scanner V3.0" Pine Script
to Python, using Bybit's free public REST API (no API key needed for
market data) and sending Telegram messages on new BUY signals.

NOTE: Binance's public API (api.binance.com) returns HTTP 451 for requests
coming from US-based IPs, which includes GitHub Actions runners (hosted on
US Azure datacenters). Bybit's public market-data endpoint does not apply
this restriction, and uses the same symbol naming (e.g. BTCUSDT), so it's
used here instead.

Designed to be run every 15 minutes by a free scheduler (GitHub Actions).
State (scanOn, open trades, win/loss, previous buy flags) is persisted to
state.json so it behaves like Pine's `var` variables across runs.
"""

import json
import os
import time
import urllib.request
import urllib.parse

# ============================================================
# SETTINGS  (mirrors the Pine Script inputs / defaults)
# ============================================================

BTC_SYMBOL = "BTCUSDT"
TIMEFRAME = "15"           # Bybit interval in minutes (string)
BTC_THRESHOLD = 0.30       # BTC Trigger %
BTC_LOOKBACK = 1           # BTC Move Lookback (in closed candles)

CORR_LEN = 100
CORR_MIN = 0.70

MIN_LAG = 0.10
MAX_RATIO = 0.85
CATCH_MIN = 0.05
ACCEL_MIN = 0.03

USE_VOLUME = True
VOLUME_LEN = 20
VOLUME_MULT = 1.10

REQUIRED_SCORE = 60
W_LAG = 25
W_CATCH = 25
W_ACCEL = 20
W_MOMENTUM = 15
W_VOLUME = 15

TP_PERCENT = 1.20
SL_PERCENT = 1.00

COINS = {
    "ETH": "ETHUSDT",
    "BNB": "BNBUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "ADA": "ADAUSDT",
    "DOGE": "DOGEUSDT",
    "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT",
    "DOT": "DOTUSDT",
    "LTC": "LTCUSDT",
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

NEEDED_CANDLES = max(CORR_LEN, VOLUME_LEN) + 10


# ============================================================
# BYBIT DATA (free public endpoint, not geo-blocked, no key required)
# ============================================================

def fetch_klines(symbol, interval=TIMEFRAME, limit=NEEDED_CANDLES):
    url = ("https://api.bybit.com/v5/market/kline?" +
           urllib.parse.urlencode({
               "category": "spot",
               "symbol": symbol,
               "interval": interval,
               "limit": limit,
           }))
    with urllib.request.urlopen(url, timeout=15) as resp:
        payload = json.loads(resp.read().decode())

    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error for {symbol}: {payload.get('retMsg')}")

    # Bybit returns rows as [startTime, open, high, low, close, volume, turnover],
    # newest first. Sort ascending by time so downstream logic (which expects
    # oldest -> newest, matching Pine's close[n] indexing) works the same way.
    rows = payload["result"]["list"]
    rows.sort(key=lambda r: int(r[0]))

    interval_ms = int(interval) * 60_000
    now_ms = int(time.time() * 1000)

    # Drop the currently-forming (unclosed) candle so we only ever act on
    # confirmed data, mirroring `confirmedOnly = true` in the Pine script.
    if rows and (int(rows[-1][0]) + interval_ms) > now_ms:
        rows = rows[:-1]

    closes = [float(r[4]) for r in rows]
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    volumes = [float(r[5]) for r in rows]
    return closes, highs, lows, volumes


def pct_returns(closes):
    """Return list of (close[i]-close[i-1])/close[i-1] for i=1..len-1."""
    out = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        out.append((closes[i] - prev) / prev if prev != 0 else 0.0)
    return out


def pearson_correlation(a, b):
    n = min(len(a), len(b))
    if n < 2:
        return None
    a = a[-n:]
    b = b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    if var_a == 0 or var_b == 0:
        return None
    return cov / ((var_a ** 0.5) * (var_b ** 0.5))


def sma(values, length):
    if len(values) < length:
        return None
    return sum(values[-length:]) / length


# ============================================================
# STATE (persists scanOn + per-coin trade memory between runs)
# ============================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "scan_on": False,
        "coins": {
            name: {
                "open": False, "entry": None, "tp": None, "sl": None,
                "win": 0, "loss": 0, "prev_buy": False,
            } for name in COINS
        },
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram token/chat id not set, skipping send. Message was:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }).encode()
    try:
        with urllib.request.urlopen(url, data=payload, timeout=15) as resp:
            resp.read()
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")


# ============================================================
# MAIN LOGIC
# ============================================================

def main():
    state = load_state()

    # ---- BTC ----
    btc_closes, btc_highs, btc_lows, btc_vols = fetch_klines(BTC_SYMBOL)
    if len(btc_closes) < BTC_LOOKBACK + 2:
        print("Not enough BTC data yet.")
        return

    btc_close = btc_closes[-1]
    btc_prev = btc_closes[-1 - BTC_LOOKBACK]
    btc_move = (btc_close - btc_prev) / btc_prev * 100 if btc_prev != 0 else None
    btc_up = btc_move is not None and btc_move >= BTC_THRESHOLD
    btc_down = btc_move is not None and btc_move <= -BTC_THRESHOLD

    if btc_up:
        state["scan_on"] = True
    if btc_down:
        state["scan_on"] = False

    scan_on = state["scan_on"]

    btc_returns_full = pct_returns(btc_closes)
    btc_ret = btc_returns_full[-1]
    btc_ret_prev = btc_returns_full[-2]
    btc_corr_series = btc_returns_full[-CORR_LEN:]

    new_signals = []

    for name, symbol in COINS.items():
        coin_state = state["coins"][name]

        closes, highs, lows, vols = fetch_klines(symbol)
        if len(closes) < max(BTC_LOOKBACK, VOLUME_LEN) + 3:
            continue

        c = closes[-1]
        prev = closes[-1 - BTC_LOOKBACK]
        move = (c - prev) / prev * 100 if prev != 0 else None

        returns_full = pct_returns(closes)
        ret = returns_full[-1]
        ret_prev = returns_full[-2]
        alt_corr_series = returns_full[-CORR_LEN:]

        corr = pearson_correlation(ret_series := alt_corr_series, btc_corr_series)
        corr_ok = corr is not None and corr >= CORR_MIN

        ratio = (abs(move) / abs(btc_move)) if (btc_move and abs(btc_move) > 0 and move is not None) else None
        lag = (abs(btc_move) - abs(move)) if (btc_move is not None and move is not None) else None

        lag_ok = lag is not None and lag >= MIN_LAG and ratio is not None and ratio <= MAX_RATIO
        catch_ok = (abs(ret) >= CATCH_MIN and abs(ret) > abs(ret_prev)
                    and abs(ret) >= abs(btc_ret) * 0.50)
        acceleration = abs(ret) - abs(ret_prev)
        accel_ok = acceleration >= ACCEL_MIN
        momentum_ok = (btc_up and move is not None and move >= 0.05)

        vol_avg = sma(vols, VOLUME_LEN)
        vol_ok = (not USE_VOLUME) or (vol_avg is not None and vols[-1] >= vol_avg * VOLUME_MULT)

        same_direction = btc_up and ret > 0
        gate = scan_on and same_direction and corr_ok

        score = 0
        score += W_LAG if lag_ok else 0
        score += W_CATCH if catch_ok else 0
        score += W_ACCEL if accel_ok else 0
        score += W_MOMENTUM if momentum_ok else 0
        score += W_VOLUME if vol_ok else 0

        candidate = gate and score >= REQUIRED_SCORE

        # ---- trade management (mirrors Pine: exit check first, then entry) ----
        was_open = coin_state["open"]
        exited_this_bar = False
        h_last, l_last = highs[-1], lows[-1]

        if was_open:
            if l_last <= coin_state["sl"]:
                coin_state["open"] = False
                coin_state["loss"] += 1
                exited_this_bar = True
            elif h_last >= coin_state["tp"]:
                coin_state["open"] = False
                coin_state["win"] += 1
                exited_this_bar = True

        if (not coin_state["open"]) and candidate and not exited_this_bar:
            coin_state["open"] = True
            coin_state["entry"] = c
            coin_state["tp"] = c * (1 + TP_PERCENT / 100)
            coin_state["sl"] = c * (1 - SL_PERCENT / 100)

        # ---- new-signal detection (mirrors newBuy = candidate and not candidate[1]) ----
        new_buy = candidate and not coin_state["prev_buy"]
        if new_buy:
            new_signals.append({
                "name": name, "price": c,
                "tp": coin_state["tp"], "sl": coin_state["sl"], "score": score,
            })
        coin_state["prev_buy"] = candidate

    save_state(state)

    if new_signals:
        lines = [f"🚀 <b>BTC → Altcoin BUY Signal</b>",
                  f"BTC move: {btc_move:.2f}% | Scan: {'ON' if scan_on else 'OFF'}", ""]
        for s in new_signals:
            lines.append(
                f"<b>{s['name']}</b>  Price: {s['price']:.6g}  "
                f"TP: {s['tp']:.6g}  SL: {s['sl']:.6g}  Score: {s['score']}"
            )
        send_telegram("\n".join(lines))
        print("Sent alert:\n" + "\n".join(lines))
    else:
        print(f"No new signals. scan_on={scan_on} btc_move={btc_move}")


if __name__ == "__main__":
    main()
