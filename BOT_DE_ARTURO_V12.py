import requests
import pandas as pd
import numpy as np
import time
import os
from datetime import datetime, timedelta, UTC

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"
INTERVAL = "5m"

HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6

IMPULSE_RANGE = 1.3
IMPULSE_VOLUME = 1.1

APPROACH_DISTANCE = 0.004
CRITICAL_DISTANCE = 0.0015
SWEEP_MIN = 0.001

last_impulse = None
last_sweep = None
last_breakout = None

last_event_time = datetime.now(UTC)          # <-- CORREGIDO: antes era datetime.now(datetime.UTC)
last_heartbeat = datetime.now(UTC)

alerted_liquidity = set()


def send(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})


def fmt(n):
    return f"{int(n):,}"


def get_price():
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
    r = requests.get(url).json()
    return float(r["price"])


def get_klines(limit=200):
    url = f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit={limit}"
    data = requests.get(url).json()

    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "ct","q","n","tbb","tbq","ignore"
    ])

    df["open"]=df["open"].astype(float)
    df["high"]=df["high"].astype(float)
    df["low"]=df["low"].astype(float)
    df["close"]=df["close"].astype(float)
    df["volume"]=df["volume"].astype(float)

    return df


def find_liquidity(df):

    highs=[]
    lows=[]

    for i in range(3,len(df)-3):

        h=df["high"][i]

        if h>df["high"][i-1] and h>df["high"][i-2] and h>df["high"][i+1] and h>df["high"][i+2]:

            touches=sum(abs(df["high"]-h)/h<0.0008)

            highs.append({
                "price":h,
                "touches":touches
            })

        l=df["low"][i]

        if l<df["low"][i-1] and l<df["low"][i-2] and l<df["low"][i+1] and l<df["low"][i+2]:

            touches=sum(abs(df["low"]-l)/l<0.0008)

            lows.append({
                "price":l,
                "touches":touches
            })

    highs=sorted(highs,key=lambda x:-x["touches"])
    lows=sorted(lows,key=lambda x:-x["touches"])

    return highs[:5],lows[:5]


def radar_impulse(df):

    global last_impulse,last_event_time

    r=df.iloc[-1]

    range_val=(r["high"]-r["low"])/r["close"]

    vol=r["volume"]/df["volume"].rolling(20).mean().iloc[-1]

    if range_val>IMPULSE_RANGE/100 and vol>IMPULSE_VOLUME:

        if last_impulse is None or (datetime.utcnow()-last_impulse).seconds>900:

            price=get_price()

            send(
f"""⚡ RADAR 0 — IMPULSO

Hora UTC: {datetime.now(UTC).strftime("%H:%M")}

Precio: {fmt(price)}

Movimiento anómalo detectado"""
            )

            last_impulse=datetime.utcnow()
            last_event_time=datetime.utcnow()


def radar_approach(price,levels):

    global last_event_time

    for lvl in levels:

        dist=abs(price-lvl["price"])/lvl["price"]

        if dist<APPROACH_DISTANCE:

            key=("approach",round(lvl["price"]))

            if key not in alerted_liquidity:

                send(
f"""📡 RADAR 1 — APROXIMACIÓN

Hora UTC: {datetime.utcnow().strftime("%H:%M")}

Precio: {fmt(price)}
Liquidez: {fmt(lvl["price"])}

Distancia: {fmt(abs(price-lvl["price"]))}"""
                )

                alerted_liquidity.add(key)
                last_event_time=datetime.utcnow()


def radar_critical(price,levels):

    global last_event_time

    for lvl in levels:

        dist=abs(price-lvl["price"])/lvl["price"]

        if dist<CRITICAL_DISTANCE:

            key=("critical",round(lvl["price"]))

            if key not in alerted_liquidity:

                send(
f"""⚠️ RADAR 2 — ZONA CRÍTICA

Hora UTC: {datetime.utcnow().strftime("%H:%M")}

Precio: {fmt(price)}
Liquidez: {fmt(lvl["price"])}

Barrido probable"""
                )

                alerted_liquidity.add(key)
                last_event_time=datetime.utcnow()


def radar_sweep(df,levels):

    global last_sweep,last_event_time

    candle=df.iloc[-1]

    for lvl in levels:

        if candle["high"]>lvl["price"]*(1+SWEEP_MIN) and candle["close"]<lvl["price"]:

            key=("sweep",round(lvl["price"]))

            if key not in alerted_liquidity:

                send(
f"""🚨 RADAR 3 — SWEEP

Hora UTC: {datetime.utcnow().strftime("%H:%M")}

Nivel barrido: {fmt(lvl["price"])}
High sweep: {fmt(candle["high"])}

Precio actual: {fmt(candle["close"])}

Dirección probable: 🔻"""
                )

                alerted_liquidity.add(key)
                last_event_time=datetime.utcnow()


def radar_breakout(df,levels):

    global last_breakout,last_event_time

    candle=df.iloc[-1]

    for lvl in levels:

        if candle["close"]>lvl["price"]*(1+SWEEP_MIN):

            key=("break",round(lvl["price"]))

            if key not in alerted_liquidity:

                send(
f"""📡 RADAR 4 — BREAKOUT

Hora UTC: {datetime.utcnow().strftime("%H:%M")}

Nivel roto: {fmt(lvl["price"])}

Precio actual: {fmt(candle["close"])}

Continuación probable: 🔺"""
                )

                alerted_liquidity.add(key)
                last_event_time=datetime.utcnow()


def heartbeat():

    global last_heartbeat

    if (datetime.now(UTC) - last_heartbeat) > timedelta(hours=HEARTBEAT_HOURS):

        price=get_price()

        send(
f"""💓 BOT ACTIVO

Hora UTC: {datetime.utcnow().strftime("%H:%M")}

Precio BTC: {fmt(price)}"""
        )

        last_heartbeat=datetime.utcnow()


def no_events():

    global last_event_time

    if (datetime.utcnow()-last_event_time)>timedelta(hours=NO_EVENT_HOURS):

        price=get_price()

        send(
f"""🟡 SIN EVENTOS

Hora UTC: {datetime.utcnow().strftime("%H:%M")}

Precio BTC: {fmt(price)}"""
        )

        last_event_time=datetime.utcnow()


send("🤖 BOT BTC INICIADO")

while True:

    try:

        df=get_klines()

        price=get_price()

        highs,lows=find_liquidity(df)

        radar_impulse(df)

        radar_approach(price,highs+lows)

        radar_critical(price,highs+lows)

        radar_sweep(df,highs)

        radar_breakout(df,highs)

        heartbeat()

        no_events()

        time.sleep(60)

    except Exception as e:

        send(f"⚠️ ERROR BOT\n{str(e)}")

        time.sleep(120)
