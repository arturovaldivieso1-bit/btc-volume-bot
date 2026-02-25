import requests
import pandas as pd
import time
import os

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"
INTERVAL = "5m"

ultimo_alertado = None

def enviar_mensaje(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

def obtener_candles():
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 50}
    data = requests.get(url, params=params).json()

    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "_","_","_","_","_","_"
    ])

    df["volume"] = df["volume"].astype(float)
    df["close"] = df["close"].astype(float)

    return df

def evaluar():
    global ultimo_alertado

    df = obtener_candles()
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    volumen_actual = df["volume"].iloc[-1]
    sma_actual = df["vol_sma20"].iloc[-1]
    precio_actual = df["close"].iloc[-1]
    tiempo_actual = df["time"].iloc[-1]

    if volumen_actual > sma_actual * 1.2:
        if ultimo_alertado != tiempo_actual:
            enviar_mensaje(
                f"🚨 BTC 5m\n"
                f"Volumen rompió SMA20 x1.2\n"
                f"Precio: {precio_actual}"
            )
            ultimo_alertado = tiempo_actual

while True:
    evaluar()
    time.sleep(60)
