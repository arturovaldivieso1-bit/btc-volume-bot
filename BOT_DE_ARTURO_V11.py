import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V11.3 iniciado 🚀")

TOKEN=os.getenv("TOKEN")
CHAT_ID=os.getenv("CHAT_ID")

SYMBOL="BTCUSDT"

TF_LIQUIDITY="1h"
TF_ENTRY="5m"

LOOKBACK=100
MIN_TOUCHES=4

CLUSTER_RANGE=0.002
PROXIMITY=0.0015

last_heartbeat=0
HEARTBEAT_INTERVAL=21600

zonas_reportadas=set()
zonas_proximidad=set()
zonas_sweep=set()
zonas_break=set()
zonas_magnet=set()


def send(msg):

    url=f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    requests.post(url,data={
        "chat_id":CHAT_ID,
        "text":msg
    })


def candles(interval,limit=200):

    url="https://api.binance.com/api/v3/klines"

    params={
        "symbol":SYMBOL,
        "interval":interval,
        "limit":limit
    }

    data=requests.get(url,params=params).json()

    df=pd.DataFrame(data)

    df=df[[1,2,3,4,5]]

    df.columns=["open","high","low","close","volume"]

    df=df.astype(float)

    return df


def cluster(prices):

    clusters=[]

    for p in sorted(prices):

        added=False

        for c in clusters:

            if abs(p-c["center"])/p<CLUSTER_RANGE:

                c["values"].append(p)
                c["center"]=sum(c["values"])/len(c["values"])
                added=True
                break

        if not added:

            clusters.append({
                "center":p,
                "values":[p]
            })

    return clusters


def detect_zones(df):

    highs=df["high"].tail(LOOKBACK).tolist()
    lows=df["low"].tail(LOOKBACK).tolist()

    ch=cluster(highs)
    cl=cluster(lows)

    zones=[]

    for c in ch:

        if len(c["values"])>=MIN_TOUCHES:

            zones.append({
                "type":"HIGH",
                "center":c["center"],
                "min":min(c["values"]),
                "max":max(c["values"]),
                "touches":len(c["values"])
            })

    for c in cl:

        if len(c["values"])>=MIN_TOUCHES:

            zones.append({
                "type":"LOW",
                "center":c["center"],
                "min":min(c["values"]),
                "max":max(c["values"]),
                "touches":len(c["values"])
            })

    return zones


def liquidity_score(z):

    score=0

    if z["touches"]>10:
        score+=3
    elif z["touches"]>6:
        score+=2
    else:
        score+=1

    spread=(z["max"]-z["min"])/z["center"]

    if spread<0.001:
        score+=3
    elif spread<0.002:
        score+=2
    else:
        score+=1

    return score


def magnet(df):

    r1=df.iloc[-1]["high"]-df.iloc[-1]["low"]
    r2=df.iloc[-2]["high"]-df.iloc[-2]["low"]
    r3=df.iloc[-3]["high"]-df.iloc[-3]["low"]

    v1=df.iloc[-1]["volume"]
    v2=df.iloc[-2]["volume"]
    v3=df.iloc[-3]["volume"]

    return r1<r2<r3 and v1<v2<v3


def sweep(df,z):

    v=df.iloc[-1]

    high=v["high"]
    low=v["low"]
    close=v["close"]

    vol=v["volume"]
    ma=df["volume"].rolling(20).mean().iloc[-1]

    if z["type"]=="HIGH":

        if high>z["max"] and close<z["center"] and vol>ma*1.5:
            return True

    if z["type"]=="LOW":

        if low<z["min"] and close>z["center"] and vol>ma*1.5:
            return True

    return False


def evaluate():

    global last_heartbeat

    df=candles(TF_LIQUIDITY)

    zones=detect_zones(df)

    if not zones:
        return

    price=df["close"].iloc[-1]

    zones=sorted(zones,key=lambda z:abs(z["center"]-price))

    now=time.time()

    if now-last_heartbeat>HEARTBEAT_INTERVAL:

        send(f"""
🫀 BOT_DE_ARTURO activo

Par: {SYMBOL}

Precio actual
{int(price)}

Zonas detectadas
{len(zones)}

Zona más cercana
{int(zones[0]['center'])}
""")

        last_heartbeat=now


    df5=candles(TF_ENTRY)

    for z in zones[:2]:

        score=liquidity_score(z)

        if score<4:
            continue

        level=int(z["center"])

        dist=abs(price-z["center"])/price*100

        direction="ALCISTA" if z["type"]=="HIGH" else "BAJISTA"


        if level not in zonas_reportadas:

            zonas_reportadas.add(level)

            send(f"""
💰 RADAR 1

Liquidez detectada {direction}

Zona
{int(z['center'])}

Rango
{int(z['min'])}-{int(z['max'])}

Score
{score}

Precio actual
{int(price)}
""")


        if magnet(df5) and level not in zonas_magnet:

            zonas_magnet.add(level)

            send(f"""
📡 RADAR 5

Liquidity magnet

Objetivo
{level}

Distancia
{dist:.2f} %

Precio actual
{int(price)}
""")


        if dist<PROXIMITY*100 and level not in zonas_proximidad:

            zonas_proximidad.add(level)

            send(f"""
🧲 RADAR 2

Precio acercándose a liquidez {direction}

Zona
{level}

Distancia
{dist:.2f} %

Precio actual
{int(price)}
""")


        if sweep(df5,z) and level not in zonas_sweep:

            zonas_sweep.add(level)

            send(f"""
🚨 RADAR 3

Sweep detectado

Zona barrida
{level}

Precio actual
{int(price)}

Posible reversión
""")


        if z["type"]=="LOW" and price<z["min"] and level not in zonas_break:

            zonas_break.add(level)

            send(f"""
📡 RADAR 4

Breakout BEARISH confirmado

Liquidez absorbida
{level}

Precio actual
{int(price)}
""")


while True:

    try:

        evaluate()

    except Exception as e:

        print(e)

    time.sleep(60)
