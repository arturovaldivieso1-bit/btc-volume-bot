# -*- coding: utf-8 -*-
import requests
import pandas as pd
import time
import os
import numpy as np

print("BOT_DE_ARTURO V7 iniciado 🚀")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"
INTERVAL = "5m"

ultimo_radar1 = None
ultimo_radar2 = None
ultimo_radar3 = None
ultimo_radar4 = None
ultimo_radar5 = None  # Sweep Probability Engine


def enviar_mensaje(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})


def obtener_candles():
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 200}
    data = requests.get(url, params=params).json()
    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close", "volume",
        "_", "_", "_", "_", "_", "_"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def calcular_liquidity_score(df, lookback=50):
    """
    Calcula un score de liquidez para highs y lows recientes.
    """
    highs = df['high'].tail(lookback)
    lows = df['low'].tail(lookback)
    score_highs = {}
    score_lows = {}

    mean_high = highs.mean()
    mean_low = lows.mean()
    tolerance = df['close'].iloc[-1] * 0.001  # 0.1% tolerancia

    # Highs
    for h in highs.unique():
        touches = sum(highs == h)
        distance = abs(df['close'].iloc[-1] - h)
        visibility = touches
        score = min(9, int(touches + max(0, (tolerance - distance) * 1000) + visibility))
        score_highs[h] = score

    # Lows
    for l in lows.unique():
        touches = sum(lows == l)
        distance = abs(df['close'].iloc[-1] - l)
        visibility = touches
        score = min(9, int(touches + max(0, (tolerance - distance) * 1000) + visibility))
        score_lows[l] = score

    return score_highs, score_lows


def detectar_compresion(df, lookback=20):
    """
    Detecta compresión: lower highs / higher lows y reducción de volatilidad.
    """
    df_recent = df.tail(lookback)
    high_range = df_recent['high'].max()
    low_range = df_recent['low'].min()
    rango = high_range - low_range
    vol_sma = df_recent['volume'].mean()
    vol_actual = df_recent['volume'].iloc[-1]
    compresion = (rango < (df_recent['close'].iloc[-1] * 0.002)) and (vol_actual < vol_sma)
    return compresion, rango


def evaluar():
    global ultimo_radar1, ultimo_radar2, ultimo_radar3, ultimo_radar4, ultimo_radar5

    df = obtener_candles()
    vela = df.iloc[-1]

    open_actual = vela["open"]
    high_actual = vela["high"]
    low_actual = vela["low"]
    close_actual = vela["close"]
    volumen = vela["volume"]
    tiempo_actual = vela["time"]

    # =================================
    # RADAR 1: Sweep básico
    # =================================
    high_prev = df["high"].rolling(20).max().iloc[-2]
    low_prev = df["low"].rolling(20).min().iloc[-2]
    sweep_high = high_actual > high_prev and close_actual < high_prev
    sweep_low = low_actual < low_prev and close_actual > low_prev

    sma_vol = df["volume"].rolling(20).mean().iloc[-1]
    volumen_fuerte = volumen > sma_vol * 1.3

    if volumen_fuerte and ultimo_radar1 != tiempo_actual:
        if sweep_high:
            enviar_mensaje(f"⚠️ RADAR 1\nSweep HIGH detectado\nVolumen institucional\nBTC {INTERVAL}\nPrecio: {close_actual}")
            ultimo_radar1 = tiempo_actual
        elif sweep_low:
            enviar_mensaje(f"⚠️ RADAR 1\nSweep LOW detectado\nVolumen institucional\nBTC {INTERVAL}\nPrecio: {close_actual}")
            ultimo_radar1 = tiempo_actual

    # =================================
    # RADAR 2: Rechazo fuerte
    # =================================
    rango = high_actual - low_actual
    cuerpo = abs(close_actual - open_actual)
    wick_ratio = rango / cuerpo if cuerpo != 0 else 0
    rechazo_fuerte = wick_ratio > 2.5

    if volumen_fuerte and rechazo_fuerte and ultimo_radar2 != tiempo_actual:
        if sweep_high:
            enviar_mensaje(f"🚨 RADAR 2\nSweep HIGH confirmado\nRechazo fuerte\nBTC {INTERVAL}\nPrecio: {close_actual}")
            ultimo_radar2 = tiempo_actual
        elif sweep_low:
            enviar_mensaje(f"🚨 RADAR 2\nSweep LOW confirmado\nRechazo fuerte\nBTC {INTERVAL}\nPrecio: {close_actual}")
            ultimo_radar2 = tiempo_actual

    # =================================
    # RADAR 3: Liquidity Magnet Score
    # =================================
    score_highs, score_lows = calcular_liquidity_score(df)
    precio_actual = close_actual
    threshold = precio_actual * 0.001

    if ultimo_radar3 != tiempo_actual:
        for h, s in score_highs.items():
            if abs(precio_actual - h) < threshold and s >= 6:
                enviar_mensaje(f"🧲 RADAR 3\nImán de liquidez ARRIBA\nScore: {s}\nBTC {INTERVAL}\nPrecio actual: {precio_actual}\nZona liquidez: {h}")
                ultimo_radar3 = tiempo_actual
                break
        for l, s in score_lows.items():
            if abs(precio_actual - l) < threshold and s >= 6:
                enviar_mensaje(f"🧲 RADAR 3\nImán de liquidez ABAJO\nScore: {s}\nBTC {INTERVAL}\nPrecio actual: {precio_actual}\nZona liquidez: {l}")
                ultimo_radar3 = tiempo_actual
                break

    # =================================
    # RADAR 4: Pool de Liquidez (mejorado)
    # =================================
    tolerance = close_actual * 0.0007
    highs = df['high'].tail(30)
    lows = df['low'].tail(30)
    equal_highs = highs[(abs(highs - highs.mean()) < tolerance)]
    equal_lows = lows[(abs(lows - lows.mean()) < tolerance)]

    if ultimo_radar4 != tiempo_actual:
        if len(equal_highs) >= 3:
            enviar_mensaje(f"💰 RADAR 4\nPOOL DE LIQUIDEZ ARRIBA\nEqual Highs detectados\nBTC {INTERVAL}")
            ultimo_radar4 = tiempo_actual
        elif len(equal_lows) >= 3:
            enviar_mensaje(f"💰 RADAR 4\nPOOL DE LIQUIDEZ ABAJO\nEqual Lows detectados\nBTC {INTERVAL}")
            ultimo_radar4 = tiempo_actual

    # =================================
    # RADAR 5: Sweep Probability Engine (v7)
    # =================================
    compresion, rango_compresion = detectar_compresion(df)
    distancia_high = min([abs(precio_actual - h) for h in score_highs.keys()]) if score_highs else float('inf')
    distancia_low = min([abs(precio_actual - l) for l in score_lows.keys()]) if score_lows else float('inf')

    sweep_prob_high = 0
    sweep_prob_low = 0

    if score_highs:
        max_score_high = max(score_highs.values())
        sweep_prob_high = min(100, int(max_score_high * (1 + int(compresion)) * (1 + 1/distancia_high*1000)))
    if score_lows:
        max_score_low = max(score_lows.values())
        sweep_prob_low = min(100, int(max_score_low * (1 + int(compresion)) * (1 + 1/distancia_low*1000)))

    if ultimo_radar5 != tiempo_actual:
        if sweep_prob_high >= 60:
            enviar_mensaje(f"📊 RADAR 5\nSweep Probability HIGH: {sweep_prob_high}%\nBTC {INTERVAL}\nPrecio actual: {precio_actual}")
            ultimo_radar5 = tiempo_actual
        elif sweep_prob_low >= 60:
            enviar_mensaje(f"📊 RADAR 5\nSweep Probability LOW: {sweep_prob_low}%\nBTC {INTERVAL}\nPrecio actual: {precio_actual}")
            ultimo_radar5 = tiempo_actual


# =================================
# LOOP PRINCIPAL
# =================================
while True:
    try:
        evaluar()
    except Exception as e:
        print("Error:", e)
    time.sleep(60)
