# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V8 iniciado 🚀")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"

INTERVAL_ENTRY = "5m"
INTERVAL_MACRO = "1h"

ultimo_radar1 = None
ultimo_radar2 = None
ultimo_radar3 = None

liquidity_level = None


# =========================
# TELEGRAM
# =========================

def enviar_mensaje(msg):

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": msg
        }
    )


# =========================
# OBTENER DATOS
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
# DETECTAR LIQUIDEZ (1H)
# =========================

def detectar_liquidez(df):

    tolerance = df["close"].iloc[-1] * 0.001

    highs = df["high"].tail(80)
    lows = df["low"].tail(80)

    for h in highs:

        touches = sum(abs(highs - h) < tolerance)

        if touches >= 3:
            return "HIGH", h, touches

    for l in lows:

        touches = sum(abs(lows - l) < tolerance)

        if touches >= 3:
            return "LOW", l, touches

    return None, None, None


# =========================
# DETECTAR SWEEP (5m)
# =========================

def detectar_sweep(df, level, tipo):

    vela = df.iloc[-1]

    open_actual = vela["open"]
    high_actual = vela["high"]
    low_actual = vela["low"]
    close_actual = vela["close"]
    volumen = vela["volume"]

    sma_vol = df["volume"].rolling(20).mean().iloc[-1]

    volumen_fuerte = volumen > sma_vol * 1.5

    rango = high_actual - low_actual
    cuerpo = abs(close_actual - open_actual)

    wick_ratio = rango / cuerpo if cuerpo != 0 else 0

    rechazo = wick_ratio > 2

    if tipo == "HIGH":

        sweep = high_actual > level and close_actual < level

        if sweep and volumen_fuerte and rechazo:
            return True

    if tipo == "LOW":

        sweep = low_actual < level and close_actual > level

        if sweep and volumen_fuerte and rechazo:
            return True

    return False


# =========================
# EVALUAR MERCADO
# =========================

def evaluar():

    global liquidity_level
    global ultimo_radar1
    global ultimo_radar2
    global ultimo_radar3

    # 1H macro
    df_macro = obtener_candles(INTERVAL_MACRO)

    tipo, nivel, toques = detectar_liquidez(df_macro)

    precio_actual = df_macro["close"].iloc[-1]

    if nivel is not None:

        liquidity_level = nivel

        if ultimo_radar1 != nivel:

            enviar_mensaje(
f"""💰 RADAR 1

LIQUIDEZ DETECTADA

Tipo: {tipo}
Nivel: {round(nivel,2)}
Toques: {toques}

BTC 1H"""
            )

            ultimo_radar1 = nivel


    # precio cerca
    if liquidity_level is not None:

        distancia = abs(precio_actual - liquidity_level)

        if distancia < precio_actual * 0.0015:

            if ultimo_radar2 != liquidity_level:

                enviar_mensaje(
f"""🧲 RADAR 2

PRECIO CERCA DE LIQUIDEZ

Nivel: {round(liquidity_level,2)}
Precio actual: {round(precio_actual,2)}

BTC"""
                )

                ultimo_radar2 = liquidity_level


    # entrada 5m
    df_entry = obtener_candles(INTERVAL_ENTRY)

    if liquidity_level is not None and tipo is not None:

        sweep = detectar_sweep(df_entry, liquidity_level, tipo)

        if sweep:

            if ultimo_radar3 != df_entry["time"].iloc[-1]:

                enviar_mensaje(
f"""🚨 RADAR 3

SWEEP CONFIRMADO

Liquidez barrida: {round(liquidity_level,2)}
Precio actual: {round(df_entry['close'].iloc[-1],2)}

Volumen institucional
Rechazo fuerte

POSIBLE SETUP"""
                )

                ultimo_radar3 = df_entry["time"].iloc[-1]


# =========================
# LOOP
# =========================

while True:

    try:

        evaluar()

    except Exception as e:

        print("Error:", e)

    time.sleep(60)
