# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V10 iniciado 🚀")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"

INTERVAL_ENTRY = "5m"
INTERVAL_MACRO = "1h"

LOOKBACK = 100
MIN_TOUCHES = 4

CLUSTER_RANGE = 0.002
PROXIMITY = 0.0015


ultimo_radar1 = None
ultimo_radar2 = None
ultimo_radar3 = None
ultimo_radar4 = None

zona_dominante = None
tipo_zona = None


# =========================
# TELEGRAM
# =========================

def enviar_mensaje(msg):

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
# CLUSTER DE PRECIOS
# =========================

def cluster_precios(lista):

    clusters = []

    for p in sorted(lista):

        agregado = False

        for c in clusters:

            if abs(p - c["precio"]) / p < CLUSTER_RANGE:

                c["precios"].append(p)
                c["precio"] = sum(c["precios"]) / len(c["precios"])
                agregado = True
                break

        if not agregado:

            clusters.append({
                "precio": p,
                "precios": [p]
            })

    return clusters


# =========================
# RADAR 1
# DETECTAR ZONAS
# =========================

def detectar_zonas(df):

    highs = df["high"].tail(LOOKBACK).tolist()
    lows = df["low"].tail(LOOKBACK).tolist()

    clusters_high = cluster_precios(highs)
    clusters_low = cluster_precios(lows)

    zonas = []

    for c in clusters_high:

        if len(c["precios"]) >= MIN_TOUCHES:

            zonas.append({
                "tipo": "HIGH",
                "nivel": c["precio"],
                "toques": len(c["precios"])
            })

    for c in clusters_low:

        if len(c["precios"]) >= MIN_TOUCHES:

            zonas.append({
                "tipo": "LOW",
                "nivel": c["precio"],
                "toques": len(c["precios"])
            })

    zonas = sorted(zonas, key=lambda x: x["toques"], reverse=True)

    return zonas


# =========================
# RADAR 3
# SWEEP
# =========================

def detectar_sweep(df, nivel, tipo):

    vela = df.iloc[-1]

    high = vela["high"]
    low = vela["low"]
    close = vela["close"]

    volumen = vela["volume"]

    sma_vol = df["volume"].rolling(20).mean().iloc[-1]

    volumen_fuerte = volumen > sma_vol * 1.5

    rango = high - low
    cuerpo = abs(close - vela["open"])

    rechazo = (rango / cuerpo) > 2 if cuerpo != 0 else False

    if tipo == "HIGH":

        if high > nivel and close < nivel and volumen_fuerte and rechazo:
            return True

    if tipo == "LOW":

        if low < nivel and close > nivel and volumen_fuerte and rechazo:
            return True

    return False


# =========================
# RADAR 4
# BREAKOUT
# =========================

def detectar_breakout(precio, nivel, tipo):

    if tipo == "HIGH":

        if precio > nivel * 1.003:
            return "UP"

    if tipo == "LOW":

        if precio < nivel * 0.997:
            return "DOWN"

    return None


# =========================
# EVALUAR
# =========================

def evaluar():

    global zona_dominante
    global tipo_zona

    global ultimo_radar1
    global ultimo_radar2
    global ultimo_radar3
    global ultimo_radar4

    df_macro = obtener_candles(INTERVAL_MACRO)

    zonas = detectar_zonas(df_macro)

    if not zonas:
        return

    zona = zonas[0]

    nivel = zona["nivel"]
    tipo = zona["tipo"]
    toques = zona["toques"]

    precio_actual = df_macro["close"].iloc[-1]

    distancia = abs(precio_actual - nivel) / precio_actual

    if ultimo_radar1 != nivel:

        sesgo = "ALCISTA" if nivel > precio_actual else "BAJISTA"

        enviar_mensaje(
f"""💰 RADAR 1

ZONA DE LIQUIDEZ DETECTADA

Tipo: {tipo}
Nivel: {round(nivel,2)}
Toques: {toques}

Sesgo probable: {sesgo}

BTC 1H"""
        )

        ultimo_radar1 = nivel

        zona_dominante = nivel
        tipo_zona = tipo


    if distancia < PROXIMITY:

        if ultimo_radar2 != nivel:

            enviar_mensaje(
f"""🧲 RADAR 2

PRECIO CERCA DE LIQUIDEZ

Nivel: {round(nivel,2)}
Precio: {round(precio_actual,2)}

BTC"""
            )

            ultimo_radar2 = nivel


    df_entry = obtener_candles(INTERVAL_ENTRY)

    if detectar_sweep(df_entry, nivel, tipo):

        if ultimo_radar3 != df_entry["time"].iloc[-1]:

            enviar_mensaje(
f"""🚨 RADAR 3

SWEEP CONFIRMADO

Liquidez barrida: {round(nivel,2)}

Rechazo institucional
POSIBLE REVERSIÓN"""
            )

            ultimo_radar3 = df_entry["time"].iloc[-1]


    breakout = detectar_breakout(df_entry["close"].iloc[-1], nivel, tipo)

    if breakout:

        if ultimo_radar4 != df_entry["time"].iloc[-1]:

            direccion = "ALCISTA 🚀" if breakout == "UP" else "BAJISTA 📉"

            enviar_mensaje(
f"""📡 RADAR 4

BREAKOUT CONFIRMADO

Liquidez absorbida
Dirección: {direccion}

Nivel: {round(nivel,2)}
Precio actual: {round(df_entry['close'].iloc[-1],2)}"""
            )

            ultimo_radar4 = df_entry["time"].iloc[-1]


# =========================
# LOOP
# =========================

while True:

    try:

        evaluar()

    except Exception as e:

        print("Error:", e)

    time.sleep(60)
