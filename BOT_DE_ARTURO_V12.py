import ccxt
import pandas as pd
import numpy as np
import time
import requests
import os
from datetime import datetime, timedelta

# ================================
# CONFIG
# ================================

SYMBOL = "BTC/USDT"
TF_EVENT = "5m"
TF_STRUCTURE = "1h"

LOOKBACK_LIQ = 168
LIQ_TOLERANCE = 0.002

IMPULSE_MULT = 1.6
VOL_MULT = 1.3

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6

# ================================
# FORMAT ALERTS
# ================================

def fmt(price):
    return f"{int(price):,}"

# ================================
# TELEGRAM
# ================================

def send(msg):

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg})
    except:
        print("Error enviando mensaje")

# ================================
# BINANCE
# ================================

exchange = ccxt.binance({
    "enableRateLimit": True
})

# ================================
# DATA
# ================================

def get_ohlc(tf, limit=200):

    ohlc = exchange.fetch_ohlcv(SYMBOL, timeframe=tf, limit=limit)

    df = pd.DataFrame(
        ohlc,
        columns=["time","open","high","low","close","volume"]
    )

    return df

# ================================
# LIQUIDITY DETECTION
# ================================

def cluster_levels(levels, tolerance):

    clusters = []

    for price in levels:

        placed = False

        for cluster in clusters:

            if abs(price - cluster["price"]) / cluster["price"] < tolerance:

                cluster["touches"] += 1
                cluster["price"] = (cluster["price"] + price) / 2

                placed = True
                break

        if not placed:

            clusters.append({
                "price": price,
                "touches": 1
            })

    return clusters


def get_liquidity(df, current_price):

    highs = cluster_levels(df["high"], LIQ_TOLERANCE)
    lows = cluster_levels(df["low"], LIQ_TOLERANCE)

    highs = sorted(highs, key=lambda x: x["price"])
    lows = sorted(lows, key=lambda x: x["price"])

    above = [h for h in highs if h["price"] > current_price][:2]
    below = [l for l in reversed(lows) if l["price"] < current_price][:2]

    return above, below

# ================================
# IMPULSE DETECTION
# ================================

def detect_impulse(df):

    last = df.iloc[-1]

    avg_range = (df["high"] - df["low"]).mean()
    avg_vol = df["volume"].mean()

    range_now = last["high"] - last["low"]

    if range_now > avg_range * IMPULSE_MULT and last["volume"] > avg_vol * VOL_MULT:

        prev_high = df["high"].iloc[-12:-1].max()
        prev_low = df["low"].iloc[-12:-1].min()

        if last["close"] > prev_high:
            return "bullish"

        if last["close"] < prev_low:
            return "bearish"

    return None

# ================================
# SWEEP DETECTION
# ================================

def detect_sweep(df, liq_above, liq_below):

    last = df.iloc[-2]
    confirm = df.iloc[-1]

    for lvl in liq_above:

        if last["high"] > lvl["price"] and last["close"] < lvl["price"]:

            if confirm["close"] < last["close"]:
                return "sweep_high", lvl["price"]

    for lvl in liq_below:

        if last["low"] < lvl["price"] and last["close"] > lvl["price"]:

            if confirm["close"] > last["close"]:
                return "sweep_low", lvl["price"]

    return None, None

# ================================
# BREAKOUT DETECTION
# ================================

def detect_breakout(df, liq_above, liq_below):

    last = df.iloc[-1]

    for lvl in liq_above:

        if last["close"] > lvl["price"] * 1.003:
            return "breakout_up"

    for lvl in liq_below:

        if last["close"] < lvl["price"] * 0.997:
            return "breakout_down"

    return None

# ================================
# STARTUP
# ================================

def startup():

    df = get_ohlc(TF_STRUCTURE, 200)
    price = df["close"].iloc[-1]

    above, below = get_liquidity(df, price)

    up = fmt(above[0]["price"]) if above else "N/A"
    down = fmt(below[0]["price"]) if below else "N/A"

    msg = f"""
🟢 BOT STOP HUNT ENGINE ONLINE

Activo: BTCUSDT
TF estructura: 1H
TF eventos: 5m

Precio actual: {fmt(price)}

🟢 Liquidez arriba: {up}
🔴 Liquidez abajo: {down}
"""

    send(msg)

# ================================
# VARIABLES DE CONTROL
# ================================

last_heartbeat = datetime.now()
last_event = datetime.now()

last_candle_time = None
last_sweep_level = None

# ================================
# START
# ================================

startup()

while True:

    try:

        df5 = get_ohlc(TF_EVENT, 200)

        current_candle = df5["time"].iloc[-1]

        if current_candle == last_candle_time:
            time.sleep(20)
            continue

        last_candle_time = current_candle

        df1 = get_ohlc(TF_STRUCTURE, 200)

        price = df5["close"].iloc[-1]

        liq_above, liq_below = get_liquidity(df1, price)

        impulse = detect_impulse(df5)
        sweep, sweep_level = detect_sweep(df5, liq_above, liq_below)
        breakout = detect_breakout(df5, liq_above, liq_below)

        # =====================
        # IMPULSE
        # =====================

        if impulse == "bullish":

            msg = f"""
⚡ IMPULSO ALCISTA DETECTADO

Precio: {fmt(price)}
"""

            send(msg)
            last_event = datetime.now()

        # =====================
        # SWEEP
        # =====================

        if sweep == "sweep_high":

            if sweep_level != last_sweep_level:

                msg = f"""
🚨 SWEEP DE LIQUIDEZ ARRIBA DETECTADO

Precio: {fmt(price)}

Dirección probable: 🔻 bajista
"""

                send(msg)

                last_sweep_level = sweep_level
                last_event = datetime.now()

        # =====================
        # BREAKOUT
        # =====================

        if breakout == "breakout_up":

            msg = f"""
📡 BREAKOUT ALCISTA CONFIRMADO

Precio: {fmt(price)}
"""

            send(msg)
            last_event = datetime.now()

        # =====================
        # HEARTBEAT
        # =====================

        if datetime.now() - last_heartbeat > timedelta(hours=HEARTBEAT_HOURS):

            send(f"💓 BOT ACTIVO\nPrecio BTC: {fmt(price)}")

            last_heartbeat = datetime.now()

        # =====================
        # NO EVENTS
        # =====================

        if datetime.now() - last_event > timedelta(hours=NO_EVENT_HOURS):

            send("⚠️ MERCADO SIN EVENTOS RELEVANTES (6H)")

            last_event = datetime.now()

        time.sleep(30)

    except Exception as e:

        send(f"⚠️ ERROR BOT: {str(e)}")
        time.sleep(60)
