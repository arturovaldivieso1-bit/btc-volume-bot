import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V4 iniciado 🚀")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"
INTERVAL = "5m"

ultimo_radar1 = None
ultimo_radar2 = None


def enviar_mensaje(msg):

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": msg
        }
    )


def obtener_candles():

    url = "https://api.binance.com/api/v3/klines"

    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "limit": 100
    }

    data = requests.get(url, params=params).json()

    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "_","_","_","_","_","_"
    ])

    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)

    return df


def evaluar():

    global ultimo_radar1
    global ultimo_radar2

    df = obtener_candles()

    df["vol_sma20"] = df["volume"].rolling(20).mean()

    high_prev = df["high"].rolling(20).max().iloc[-2]
    low_prev = df["low"].rolling(20).min().iloc[-2]

    vela = df.iloc[-1]

    open_actual = vela["open"]
    high_actual = vela["high"]
    low_actual = vela["low"]
    close_actual = vela["close"]
    volumen = vela["volume"]
    tiempo_actual = vela["time"]

    sma_vol = df["vol_sma20"].iloc[-1]

    volumen_fuerte = volumen > sma_vol * 1.3

    sweep_high = high_actual > high_prev and close_actual < high_prev
    sweep_low = low_actual < low_prev and close_actual > low_prev

    rango = high_actual - low_actual
    cuerpo = abs(close_actual - open_actual)

    wick_ratio = rango / cuerpo if cuerpo != 0 else 0

    rechazo_fuerte = wick_ratio > 2.5

    # RADAR 1
    if volumen_fuerte and ultimo_radar1 != tiempo_actual:

        if sweep_high:

            enviar_mensaje(
f"""⚠️ RADAR 1

Sweep HIGH detectado

Volumen institucional
BTC {INTERVAL}

Precio: {close_actual}
"""
            )

            ultimo_radar1 = tiempo_actual

        elif sweep_low:

            enviar_mensaje(
f"""⚠️ RADAR 1

Sweep LOW detectado

Volumen institucional
BTC {INTERVAL}

Precio: {close_actual}
"""
            )

            ultimo_radar1 = tiempo_actual

    # RADAR 2
    if volumen_fuerte and rechazo_fuerte and ultimo_radar2 != tiempo_actual:

        if sweep_high:

            enviar_mensaje(
f"""🚨 RADAR 2

Sweep HIGH confirmado
Rechazo fuerte

BTC {INTERVAL}

Precio: {close_actual}
"""
            )

            ultimo_radar2 = tiempo_actual

        elif sweep_low:

            enviar_mensaje(
f"""🚨 RADAR 2

Sweep LOW confirmado
Rechazo fuerte

BTC {INTERVAL}

Precio: {close_actual}
"""
            )

            ultimo_radar2 = tiempo_actual


while True:

    try:

        evaluar()

    except Exception as e:

        print("Error:", e)

    time.sleep(60)
