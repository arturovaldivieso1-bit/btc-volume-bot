# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V12 iniciado 🚀")

# =========================
# CONFIG
# =========================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"

TF_ESTRUCTURA = "1h"
TF_ENTRADA = "5m"

LOOKBACK = 80
MIN_TOUCHES = 3

CLUSTER_RANGE = 0.0018
PROXIMIDAD = 0.0025
ZONA_EQUIVALENTE = 0.001

# estados
zona_actual = None
zona_alertada = False
zona_consumida = False

ultimo_radar0 = None

# =========================
# TELEGRAM
# =========================

def enviar(msg):

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:
        requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": msg
        }, timeout=10)

    except:
        print("Error enviando a Telegram")


# =========================
# DATOS BINANCE
# =========================

def obtener_candles(interval, limit=200):

    url = "https://api.binance.com/api/v3/klines"

    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "limit": limit
    }

    try:

        data = requests.get(url, params=params, timeout=10).json()

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "_","_","_","_","_","_"
        ])

        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)

        return df

    except:
        return None


# =========================
# CLUSTER DE PRECIOS
# =========================

def cluster_precios(lista):

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
# DETECTAR ZONAS LIQUIDEZ
# =========================

def detectar_zonas(df):

    highs = df["high"].tail(LOOKBACK).tolist()
    lows = df["low"].tail(LOOKBACK).tolist()

    clusters_high = cluster_precios(highs)
    clusters_low = cluster_precios(lows)

    zonas = []

    for c in clusters_high:

        if len(c["valores"]) >= MIN_TOUCHES:

            zonas.append({
                "tipo": "HIGH",
                "centro": c["centro"],
                "toques": len(c["valores"])
            })

    for c in clusters_low:

        if len(c["valores"]) >= MIN_TOUCHES:

            zonas.append({
                "tipo": "LOW",
                "centro": c["centro"],
                "toques": len(c["valores"])
            })

    return zonas


# =========================
# ZONA MAS CERCANA
# =========================

def zona_mas_cercana(zonas, precio):

    if not zonas:
        return None

    zonas = sorted(zonas, key=lambda z: abs(z["centro"] - precio))

    return zonas[0]


# =========================
# RADAR 0 IMPULSO
# =========================

def detectar_impulso(df):

    global ultimo_radar0

    last = df.iloc[-1]

    cuerpo = abs(last["close"] - last["open"])

    rango_prom = (df["high"] - df["low"]).tail(20).mean()
    vol_prom = df["volume"].tail(20).mean()

    vol_actual = last["volume"]

    direccion = None

    if cuerpo > rango_prom * 1.5 and vol_actual > vol_prom * 1.5:

        if last["close"] > last["open"]:
            direccion = "ALCISTA"

        else:
            direccion = "BAJISTA"

    if direccion and direccion != ultimo_radar0:

        ultimo_radar0 = direccion

        precio = last["close"]

        msg = f"""
🚨 RADAR 0 — IMPULSO

BTCUSDT

Precio actual: {precio:.0f}

Dirección: {direccion}
Volumen alto detectado

TF señal: 5m
"""

        enviar(msg)


# =========================
# RADAR 3 SWEEP
# =========================

def sweep(df, zona):

    vela = df.iloc[-1]

    high = vela["high"]
    low = vela["low"]
    close = vela["close"]
    open_ = vela["open"]

    rango = high - low
    cuerpo = abs(close - open_)

    if rango == 0:
        return False

    mecha = cuerpo / rango < 0.4

    if zona["tipo"] == "HIGH":

        if high > zona["centro"] and close < zona["centro"] and mecha:
            return True

    if zona["tipo"] == "LOW":

        if low < zona["centro"] and close > zona["centro"] and mecha:
            return True

    return False


# =========================
# LOOP PRINCIPAL
# =========================

while True:

    try:

        df_estructura = obtener_candles(TF_ESTRUCTURA, 200)
        df_entrada = obtener_candles(TF_ENTRADA, 200)

        if df_estructura is None or df_entrada is None:
            time.sleep(30)
            continue

        precio = df_entrada.iloc[-1]["close"]

        # radar 0
        detectar_impulso(df_entrada)

        zonas = detectar_zonas(df_estructura)

        zona = zona_mas_cercana(zonas, precio)

        if zona:

            distancia = abs(precio - zona["centro"]) / precio

            # RADAR 1 liquidez
            if zona_actual is None:

                zona_actual = zona

                msg = f"""
💰 RADAR 1 — LIQUIDEZ

BTCUSDT

Precio actual: {precio:.0f}

Zona liquidez: {zona["centro"]:.0f}
Toques: {zona["toques"]}

TF estructura: 1h
"""

                enviar(msg)

            # RADAR 2 proximidad
            if distancia < PROXIMIDAD and not zona_alertada:

                zona_alertada = True

                msg = f"""
🔎 RADAR 2 — PROXIMIDAD

BTCUSDT

Precio actual: {precio:.0f}

Zona liquidez: {zona["centro"]:.0f}
Distancia: {distancia*100:.2f}%

Posible sweep cercano
"""

                enviar(msg)

            # RADAR 3 sweep
            if sweep(df_entrada, zona) and not zona_consumida:

                zona_consumida = True

                msg = f"""
🔄 RADAR 3 — SWEEP

BTCUSDT

Precio actual: {precio:.0f}

Zona barrida: {zona["centro"]:.0f}

Posible reversión
"""

                enviar(msg)

            # RADAR 4 breakout
            if zona_consumida:

                if zona["tipo"] == "HIGH" and precio > zona["centro"]:

                    msg = f"""
💥 RADAR 4 — BREAKOUT

BTCUSDT

Precio actual: {precio:.0f}

Zona rota: {zona["centro"]:.0f}
Confirmación alcista
"""

                    enviar(msg)

                    zona_actual = None
                    zona_alertada = False
                    zona_consumida = False

                if zona["tipo"] == "LOW" and precio < zona["centro"]:

                    msg = f"""
💥 RADAR 4 — BREAKOUT

BTCUSDT

Precio actual: {precio:.0f}

Zona rota: {zona["centro"]:.0f}
Confirmación bajista
"""

                    enviar(msg)

                    zona_actual = None
                    zona_alertada = False
                    zona_consumida = False

        time.sleep(30)

    except Exception as e:

        print("Error loop:", e)
        time.sleep(60)
