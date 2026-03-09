import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V10.1 iniciado 🚀")

TOKEN=os.getenv("TOKEN")
CHAT_ID=os.getenv("CHAT_ID")

SYMBOL="BTCUSDT"

TF_LIQUIDITY="1h"
TF_ENTRY="5m"

LOOKBACK=100
MIN_TOUCHES=4

CLUSTER_RANGE=0.0015
PROXIMITY=0.0015

HEARTBEAT_INTERVAL=21600
last_heartbeat=0

zonas_r1=set()
zonas_r2=set()
zonas_r3=set()
zonas_r4=set()


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

            if abs(p-c["center"])/p < CLUSTER_RANGE:

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

Par {SYMBOL}

Precio actual
{int(price)}

Zonas detectadas
{len(zones)}
""")

        last_heartbeat=now


    df5=candles(TF_ENTRY)
    close5=df5.iloc[-1]["close"]

    for z in zones[:2]:

        score=liquidity_score(z)

        print("Zona:",int(z["center"]),"Score:",score,"Touches:",z["touches"])

        if score<3:
            continue

        level=int(z["center"])

        dist=abs(price-z["center"])/price*100

        if price<z["center"]:
            side="🟢 HIGH"
        else:
            side="🔴 LOW"


        if level not in zonas_r1:

            zonas_r1.add(level)

            send(f"""
💰 RADAR 1

Liquidez detectada {side}

Zona
{level}

Rango
{int(z['min'])}-{int(z['max'])}

Score
{score}

Precio actual
{int(price)}
""")


        if dist<PROXIMITY*100 and level not in zonas_r2:

            zonas_r2.add(level)

            send(f"""
🔎 RADAR 2

Precio acercándose a liquidez {side}

Zona
{level}

Distancia
{dist:.2f} %

Precio actual
{int(price)}
""")


        if sweep(df5,z) and level not in zonas_r3:

            zonas_r3.add(level)

            send(f"""
🔄 RADAR 3

Sweep detectado {side}

Zona barrida
{level}

Precio actual
{int(price)}

Posible reversión
""")


        if z["type"]=="LOW" and close5<z["min"] and level not in zonas_r4:

            zonas_r4.add(level)

            send(f"""
💥 RADAR 4

Breakout confirmado 🔴 LOW

Liquidez absorbida
{level}

Precio actual
{int(price)}
""")


        if z["type"]=="HIGH" and close5>z["max"] and level not in zonas_r4:

            zonas_r4.add(level)

            send(f"""
💥 RADAR 4

Breakout confirmado 🟢 HIGH

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
