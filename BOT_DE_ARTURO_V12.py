import requests
import pandas as pd
import numpy as np
import time
import os
from datetime import datetime, timedelta, timezone

# -------------------- CONFIG --------------------
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise ValueError("Debes configurar TOKEN y CHAT_ID")

SYMBOL = "BTCUSDT"
INTERVAL = "5m"

HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6

IMPULSE_RANGE = 1.3           # % de rango del candle
IMPULSE_VOLUME = 1.1          # relación de volumen sobre media

APPROACH_DISTANCE = 0.004     # 0.4%
CRITICAL_DISTANCE = 0.0015    # 0.15%
SWEEP_MIN = 0.001             # 0.1%

# -------------------- ESTADO --------------------
last_impulse = None
last_sweep = None
last_breakout = None
last_event_time = datetime.now(timezone.utc)
last_heartbeat = datetime.now(timezone.utc)

alerted_liquidity = set()

print("===================================")
print("🚀 BOT DE LIQUIDEZ BTC INICIADO")
print("Radares activos:")
print("0 Impulso")
print("1 Aproximación Liquidez")
print("2 Zona Crítica")
print("3 Sweep")
print("4 Breakout")
print("Heartbeat cada 4 horas")
print("===================================")

# -------------------- FUNCIONES --------------------
def send(msg: str):
    """Envía un mensaje a Telegram."""
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje: {e}")

def fmt(n):
    """Formatea número con comas."""
    return f"{int(n):,}"

def get_price():
    """Obtiene el precio actual de Binance."""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
        r = requests.get(url, timeout=10).json()
        return float(r["price"])
    except:
        return None

def get_klines(limit=200):
    """Obtiene los últimos candles."""
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit={limit}"
        data = requests.get(url, timeout=10).json()

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "ct","q","n","tbb","tbq","ignore"
        ])

        df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)

        return df

    except Exception as e:
        print("Error klines:", e)
        return pd.DataFrame()

# -------------------- DETECCIÓN DE NIVELES --------------------
def find_liquidity(df):

    highs, lows = [], []

    if len(df) < 10:
        return highs, lows

    for i in range(3, len(df)-3):

        h = df["high"][i]

        if all(h > df["high"][i+j] for j in [-2,-1,1,2]):

            touches = sum(abs(df["high"]-h)/h < 0.0008)

            highs.append({
                "price": h,
                "touches": touches
            })

        l = df["low"][i]

        if all(l < df["low"][i+j] for j in [-2,-1,1,2]):

            touches = sum(abs(df["low"]-l)/l < 0.0008)

            lows.append({
                "price": l,
                "touches": touches
            })

    highs = sorted(highs, key=lambda x: -x["touches"])[:5]
    lows = sorted(lows, key=lambda x: -x["touches"])[:5]

    return highs, lows

# -------------------- RADARES --------------------
def radar_impulse(df):

    global last_impulse, last_event_time

    if df.empty:
        return

    candle = df.iloc[-1]

    vol_ma = df["volume"].rolling(20).mean().iloc[-1]

    if pd.isna(vol_ma):
        return

    range_val = (candle["high"]-candle["low"]) / candle["close"]

    vol = candle["volume"] / vol_ma

    if range_val > IMPULSE_RANGE/100 and vol > IMPULSE_VOLUME:

        if last_impulse is None or (datetime.now(timezone.utc)-last_impulse).total_seconds() > 900:

            price = get_price()

            send(f"""⚡ RADAR 0 — IMPULSO
Hora UTC: {datetime.now(timezone.utc).strftime("%H:%M")}
Precio: {fmt(price)}
Movimiento anómalo detectado""")

            print("⚡ Impulso detectado")

            last_impulse = datetime.now(timezone.utc)
            last_event_time = datetime.now(timezone.utc)

def radar_approach(price, levels):

    global last_event_time

    for lvl in levels:

        dist = abs(price - lvl["price"]) / lvl["price"]

        if dist < APPROACH_DISTANCE:

            key = ("approach", round(lvl["price"]))

            if key not in alerted_liquidity:

                send(f"""📡 RADAR 1 — APROXIMACIÓN
Hora UTC: {datetime.now(timezone.utc).strftime("%H:%M")}
Precio: {fmt(price)}
Liquidez: {fmt(lvl["price"])}
Distancia: {fmt(abs(price-lvl["price"]))}""")

                print("📡 Aproximación liquidez")

                alerted_liquidity.add(key)
                last_event_time = datetime.now(timezone.utc)

def radar_critical(price, levels):

    global last_event_time

    for lvl in levels:

        dist = abs(price - lvl["price"]) / lvl["price"]

        if dist < CRITICAL_DISTANCE:

            key = ("critical", round(lvl["price"]))

            if key not in alerted_liquidity:

                send(f"""⚠️ RADAR 2 — ZONA CRÍTICA
Hora UTC: {datetime.now(timezone.utc).strftime("%H:%M")}
Precio: {fmt(price)}
Liquidez: {fmt(lvl["price"])}
Barrido probable""")

                print("⚠️ Zona crítica")

                alerted_liquidity.add(key)
                last_event_time = datetime.now(timezone.utc)

def radar_sweep(df, levels):

    global last_event_time

    candle = df.iloc[-1]

    for lvl in levels:

        if candle["high"] > lvl["price"]*(1+SWEEP_MIN) and candle["close"] < lvl["price"]:

            key = ("sweep", round(lvl["price"]))

            if key not in alerted_liquidity:

                send(f"""🚨 RADAR 3 — SWEEP
Hora UTC: {datetime.now(timezone.utc).strftime("%H:%M")}
Nivel barrido: {fmt(lvl["price"])}
High sweep: {fmt(candle['high'])}
Precio actual: {fmt(candle['close'])}
Dirección probable: 🔻""")

                print("🚨 Sweep detectado")

                alerted_liquidity.add(key)
                last_event_time = datetime.now(timezone.utc)

def radar_breakout(df, levels):

    global last_event_time

    candle = df.iloc[-1]

    for lvl in levels:

        if candle["close"] > lvl["price"]*(1+SWEEP_MIN):

            key = ("break", round(lvl["price"]))

            if key not in alerted_liquidity:

                send(f"""📡 RADAR 4 — BREAKOUT
Hora UTC: {datetime.now(timezone.utc).strftime("%H:%M")}
Nivel roto: {fmt(lvl["price"])}
Precio actual: {fmt(candle['close'])}
Continuación probable: 🔺""")

                print("📡 Breakout detectado")

                alerted_liquidity.add(key)
                last_event_time = datetime.now(timezone.utc)

# -------------------- HEARTBEAT & NO EVENT --------------------
def heartbeat():

    global last_heartbeat

    if (datetime.now(timezone.utc) - last_heartbeat) > timedelta(hours=HEARTBEAT_HOURS):

        price = get_price()

        send(f"""💓 BOT ACTIVO
Hora UTC: {datetime.now(timezone.utc).strftime("%H:%M")}
Precio BTC: {fmt(price)}""")

        print("💓 Heartbeat enviado")

        last_heartbeat = datetime.now(timezone.utc)

def no_events():

    global last_event_time

    if (datetime.now(timezone.utc) - last_event_time) > timedelta(hours=NO_EVENT_HOURS):

        price = get_price()

        send(f"""🟡 SIN EVENTOS
Hora UTC: {datetime.now(timezone.utc).strftime("%H:%M")}
Precio BTC: {fmt(price)}""")

        print("🟡 Sin eventos")

        last_event_time = datetime.now(timezone.utc)

# -------------------- MAIN --------------------
send("🤖 BOT BTC INICIADO")

while True:

    try:

        df = get_klines()

        if df.empty:
            time.sleep(60)
            continue

        price = get_price()

        highs, lows = find_liquidity(df)

        radar_impulse(df)
        radar_approach(price, highs + lows)
        radar_critical(price, highs + lows)
        radar_sweep(df, highs)
        radar_breakout(df, highs)

        heartbeat()
        no_events()

        time.sleep(60)

    except Exception as e:

        print("ERROR BOT:", e)

        send(f"⚠️ ERROR BOT\n{e}")

        time.sleep(120)
