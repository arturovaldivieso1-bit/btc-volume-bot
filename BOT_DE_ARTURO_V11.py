import requests
import pandas as pd
import time

print("BOT_DE_ARTURO V11 iniciado 🚀")

# =========================
# CONFIG
# =========================

SYMBOL = "BTCUSDT"
INTERVAL = "5m"
LIMIT = 200

TELEGRAM_TOKEN = "TU_TOKEN"
TELEGRAM_CHAT_ID = "TU_CHAT_ID"

MICRO_ZONE_FILTER = 0.004

HEARTBEAT_INTERVAL = 14400  # 4 horas
ultimo_heartbeat = time.time()

zona_activa = None

radar0_enviado=set()
radar1_enviado=set()
radar2_enviado=set()
radar3_enviado=set()
radar4_enviado=set()

# =========================
# TELEGRAM
# =========================

def enviar_telegram(msg):

    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload={
        "chat_id":TELEGRAM_CHAT_ID,
        "text":msg
    }

    try:
        requests.post(url,data=payload,timeout=10)
    except:
        print("Error enviando mensaje Telegram")

# =========================
# HEARTBEAT
# =========================

def heartbeat():

    global ultimo_heartbeat

    ahora=time.time()

    if ahora-ultimo_heartbeat > HEARTBEAT_INTERVAL:

        enviar_telegram(
"""💓 HEARTBEAT

BOT_DE_ARTURO V11 sigue activo
Monitoreando BTCUSDT
"""
        )

        ultimo_heartbeat=ahora

# =========================
# DATA
# =========================

def get_data():

    url="https://api.binance.com/api/v3/klines"

    params={
        "symbol":SYMBOL,
        "interval":INTERVAL,
        "limit":LIMIT
    }

    data=requests.get(url,params=params,timeout=10).json()

    df=pd.DataFrame(data,columns=[
        "timestamp","open","high","low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"
    ])

    df=df.astype(float)

    return df

# =========================
# RADAR 0 (IMPULSO)
# =========================

def radar0_impulso(df):

    if len(df)<30:
        return None

    last=df.iloc[-1]
    prev=df.iloc[-20:-1]

    vela_actual=abs(last["close"]-last["open"])
    vela_prom=(prev["high"]-prev["low"]).mean()

    vol_actual=last["volume"]
    vol_prom=prev["volume"].mean()

    high_rango=prev["high"].max()
    low_rango=prev["low"].min()

    impulso_alcista=(
        vela_actual>vela_prom*1.8 and
        vol_actual>vol_prom*1.7 and
        last["close"]>high_rango
    )

    impulso_bajista=(
        vela_actual>vela_prom*1.8 and
        vol_actual>vol_prom*1.7 and
        last["close"]<low_rango
    )

    if impulso_alcista:
        return "ALCISTA"

    if impulso_bajista:
        return "BAJISTA"

    return None

# =========================
# DETECTAR ZONAS
# =========================

def detect_zones(df):

    highs=df["high"]
    lows=df["low"]

    zones=[]

    for i in range(len(df)-5):

        h=highs[i]

        touches=((abs(highs-h)/h)<MICRO_ZONE_FILTER).sum()

        if touches>=4:

            zones.append({
                "type":"HIGH",
                "center":h,
                "touches":touches,
                "min":h*(1-MICRO_ZONE_FILTER),
                "max":h*(1+MICRO_ZONE_FILTER)
            })

        l=lows[i]

        touches=((abs(lows-l)/l)<MICRO_ZONE_FILTER).sum()

        if touches>=4:

            zones.append({
                "type":"LOW",
                "center":l,
                "touches":touches,
                "min":l*(1-MICRO_ZONE_FILTER),
                "max":l*(1+MICRO_ZONE_FILTER)
            })

    zones=sorted(zones,key=lambda z:z["touches"],reverse=True)

    return zones

# =========================
# SCORE
# =========================

def liquidity_score(z):

    score=0

    if z["touches"]>=4:
        score+=1

    if z["touches"]>=6:
        score+=1

    if z["touches"]>=8:
        score+=1

    return score

# =========================
# EVALUAR ZONA
# =========================

def evaluate(df):

    global zona_activa

    close=df.iloc[-1]["close"]

    zones=detect_zones(df)

    if zona_activa is None:

        for z in zones:

            score=liquidity_score(z)

            if score>=1:

                zona_activa=z
                break

    if zona_activa is None:
        return

    z=zona_activa

    level=round(z["center"],2)

    # RADAR 1

    if level not in radar1_enviado:

        radar1_enviado.add(level)

        enviar_telegram(
f"""💰 RADAR 1

Liquidez detectada

Zona: {level}
Toques: {z['touches']}
"""
        )

    dist=abs(close-z["center"])/z["center"]

    # RADAR 2

    if dist<0.002 and level not in radar2_enviado:

        radar2_enviado.add(level)

        enviar_telegram(
f"""🔎 RADAR 2

Precio acercándose a liquidez

Zona: {level}
Distancia: {round(dist*100,3)}%
"""
        )

    high=df.iloc[-1]["high"]
    low=df.iloc[-1]["low"]

    # RADAR 3 SWEEP

    if z["type"]=="HIGH":

        if high>z["max"] and close<z["center"] and level not in radar3_enviado:

            radar3_enviado.add(level)

            enviar_telegram(
f"""🔄 RADAR 3

Sweep de liquidez arriba

Zona: {level}
Posible reversión
"""
            )

    if z["type"]=="LOW":

        if low<z["min"] and close>z["center"] and level not in radar3_enviado:

            radar3_enviado.add(level)

            enviar_telegram(
f"""🔄 RADAR 3

Sweep de liquidez abajo

Zona: {level}
Posible reversión
"""
            )

    # RADAR 4 BREAKOUT

    if z["type"]=="HIGH":

        if close>z["max"] and level not in radar4_enviado:

            radar4_enviado.add(level)

            enviar_telegram(
f"""💥 RADAR 4

Breakout alcista

Zona rota: {level}
"""
            )

            zona_activa=None

    if z["type"]=="LOW":

        if close<z["min"] and level not in radar4_enviado:

            radar4_enviado.add(level)

            enviar_telegram(
f"""💥 RADAR 4

Breakout bajista

Zona rota: {level}
"""
            )

            zona_activa=None

# =========================
# LOOP PRINCIPAL
# =========================

while True:

    try:

        df=get_data()

        impulso=radar0_impulso(df)

        if impulso:

            candle_time=df.iloc[-1]["timestamp"]

            if candle_time not in radar0_enviado:

                radar0_enviado.add(candle_time)

                delta=df.iloc[-1]["close"]-df.iloc[-1]["open"]

                enviar_telegram(
f"""🚨 RADAR 0

Mercado despertó

Impulso: {impulso}
Δ precio: {round(delta,2)} USD
Volumen alto detectado
"""
                )

        evaluate(df)

        heartbeat()

    except Exception as e:

        print("error:",e)

    time.sleep(60)
