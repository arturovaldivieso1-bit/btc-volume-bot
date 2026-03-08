import requests
import pandas as pd
import time
import os

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL="BTCUSDT"

TF_STRUCTURE="4h"
TF_LIQUIDITY="1h"
TF_ENTRY="5m"

LOOKBACK=120
MIN_TOUCHES=4

CLUSTER_RANGE=0.002
PROXIMITY=0.0015
ZONE_EQ=0.001

zona_actual=None
zona_consumida=False
zona_proximidad=False


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

    zones=sorted(zones,key=lambda x:x["touches"],reverse=True)

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

    if r1<r2<r3 and v1<v2<v3:

        return True

    return False


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


def breakout(price,z):

    if z["type"]=="HIGH":

        if price>z["max"]*1.003:
            return "UP"

    if z["type"]=="LOW":

        if price<z["min"]*0.997:
            return "DOWN"

    return None


def evaluate():

    global zona_actual,zona_consumida,zona_proximidad

    df=candles(TF_LIQUIDITY)

    zones=detect_zones(df)

    if not zones:
        return

    z=zones[0]

    score=liquidity_score(z)

    if score<6:
        return

    price=df["close"].iloc[-1]

    if zona_actual is None:

        zona_actual=z

        t="🟢 HIGH" if z["type"]=="HIGH" else "🔴 LOW"

        send(f"""

💰 RADAR 1

Zona liquidez {t}

{int(z["center"])} ({int(z["min"])}-{int(z["max"])})

Score {score}

Precio actual {int(price)}

""")

    df5=candles(TF_ENTRY)

    if magnet(df5):

        send(f"""

📡 RADAR 5

Liquidity magnet detectado

Objetivo

{int(z["center"])}

""")

    dist=abs(price-z["center"])/price

    if dist<PROXIMITY and not zona_proximidad:

        zona_proximidad=True

        send(f"""

🧲 RADAR 2

Precio cerca de liquidez

{int(z["center"])} ({int(z["min"])}-{int(z["max"])})

Precio actual {int(price)}

""")

    if sweep(df5,z):

        send(f"""

🚨 RADAR 3

Sweep detectado

Zona {int(z["center"])}

Posible reversión

""")

    b=breakout(df5.iloc[-1]["close"],z)

    if b and not zona_consumida:

        zona_consumida=True

        d="🟢 BULLISH" if b=="UP" else "🔴 BEARISH"

        send(f"""

📡 RADAR 4

Breakout confirmado {d}

Liquidez del nivel
{int(z["center"])} absorbida

Precio actual
{int(df5.iloc[-1]["close"])}

""")

while True:

    try:

        evaluate()

    except Exception as e:

        print(e)

    time.sleep(60)
