# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V10 iniciado 🚀")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"

INTERVAL_MACRO = "1h"
INTERVAL_ENTRY = "5m"

LOOKBACK = 100
MIN_TOUCHES = 4

CLUSTER_RANGE = 0.002
PROXIMITY = 0.0015
ZONA_EQUIVALENTE = 0.001


zona_actual = None
zona_alertada_proximidad = False
zona_consumida = False


# =========================
# TELEGRAM
# =========================

def enviar(msg):

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": msg
    })


# =========================
# DATOS
# =========================

def obtener_candles(interval, limit=200):

    url = "https://api.binance.com/api/v3/klines"

    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "limit": limit
    }

    data = requests.get(url, params=params).json()

    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "_","_","_","_","_","_"
    ])

    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)

    return df


# =========================
# CLUSTER
# =========================

def cluster(lista):

    clusters = []

    for p in sorted(lista):

        agregado = False

        for c in clusters:

            if abs(p - c["centro"]) / p < CLUSTER_RANGE:

                c["valores"].append(p)
                c["centro"] = sum(c["valores"]) / len(c["valores"])
                agregado = True
                break

        if not agregado:

            clusters.append({
                "centro": p,
                "valores": [p]
            })

    return clusters


# =========================
# DETECTAR ZONAS
# =========================

def detectar_zonas(df):

    highs = df["high"].tail(LOOKBACK).tolist()
    lows = df["low"].tail(LOOKBACK).tolist()

    clusters_high = cluster(highs)
    clusters_low = cluster(lows)

    zonas = []

    for c in clusters_high:

        if len(c["valores"]) >= MIN_TOUCHES:

            zonas.append({
                "tipo":"HIGH",
                "centro":c["centro"],
                "max":max(c["valores"]),
                "min":min(c["valores"]),
                "toques":len(c["valores"])
            })

    for c in clusters_low:

        if len(c["valores"]) >= MIN_TOUCHES:

            zonas.append({
                "tipo":"LOW",
                "centro":c["centro"],
                "max":max(c["valores"]),
                "min":min(c["valores"]),
                "toques":len(c["valores"])
            })

    zonas = sorted(zonas, key=lambda x: x["toques"], reverse=True)

    return zonas


# =========================
# MISMA ZONA
# =========================

def misma_zona(z1, z2):

    if z1 is None or z2 is None:
        return False

    return abs(z1["centro"] - z2["centro"]) / z1["centro"] < ZONA_EQUIVALENTE


# =========================
# SWEEP
# =========================

def sweep(df, zona):

    vela = df.iloc[-1]

    high = vela["high"]
    low = vela["low"]
    close = vela["close"]

    volumen = vela["volume"]
    vol_ma = df["volume"].rolling(20).mean().iloc[-1]

    rechazo = False

    if zona["tipo"] == "HIGH":

        if high > zona["max"] and close < zona["centro"]:
            rechazo = True

    if zona["tipo"] == "LOW":

        if low < zona["min"] and close > zona["centro"]:
            rechazo = True

    if rechazo and volumen > vol_ma * 1.5:
        return True

    return False


# =========================
# BREAKOUT
# =========================

def breakout(precio, zona):

    global zona_consumida

    if zona_consumida:
        return None

    if zona["tipo"] == "HIGH":

        if precio > zona["max"] * 1.003:
            return "UP"

    if zona["tipo"] == "LOW":

        if precio < zona["min"] * 0.997:
            return "DOWN"

    return None


# =========================
# EVALUAR
# =========================

def evaluar():

    global zona_actual
    global zona_alertada_proximidad
    global zona_consumida

    df_macro = obtener_candles(INTERVAL_MACRO)

    zonas = detectar_zonas(df_macro)

    if not zonas:
        return

    zona = zonas[0]

    precio = df_macro["close"].iloc[-1]

    if not misma_zona(zona_actual, zona):

        zona_actual = zona
        zona_alertada_proximidad = False
        zona_consumida = False

        tipo = "🟢 HIGH" if zona["tipo"]=="HIGH" else "🔴 LOW"

        centro = int(zona["centro"])
        zmin = int(zona["min"])
        zmax = int(zona["max"])
        precio_i = int(precio)

        distancia = int(abs(precio - zona["centro"]))

        enviar(f"""
💰 RADAR 1

Zona liquidez {tipo}
{centro} ({zmin}-{zmax})

Precio actual {precio_i}
Distancia {distancia}$
""")


    distancia = abs(precio - zona["centro"]) / precio

    if distancia < PROXIMITY and not zona_alertada_proximidad:

        zona_alertada_proximidad = True

        enviar(f"""
🧲 RADAR 2

Precio cerca de liquidez

{int(zona['centro'])} ({int(zona['min'])}-{int(zona['max'])})

Precio actual {int(precio)}
""")


    df_entry = obtener_candles(INTERVAL_ENTRY)

    if sweep(df_entry, zona):

        enviar(f"""
🚨 RADAR 3

Sweep detectado

Zona {int(zona['centro'])}
Posible reversión
""")


    b = breakout(df_entry["close"].iloc[-1], zona)

    if b:

        zona_consumida = True

        direccion = "🟢 BULLISH" if b=="UP" else "🔴 BEARISH"

        enviar(f"""
📡 RADAR 4

Breakout confirmado {direccion}

Liquidez del nivel
{int(zona['centro'])} absorbida

Precio actual
{int(df_entry['close'].iloc[-1])}
""")


while True:

    try:
        evaluar()

    except Exception as e:
        print(e)

    time.sleep(60)
