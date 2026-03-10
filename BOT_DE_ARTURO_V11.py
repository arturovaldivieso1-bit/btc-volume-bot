# -*- coding: utf-8 -*-
import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V12 Corriendo")

# =========================
# CONFIG
# =========================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"

INTERVAL_MACRO = "1h"
INTERVAL_ENTRY = "5m"

LOOKBACK = 100
MIN_TOUCHES = 3
CLUSTER_RANGE = 0.002
PROXIMITY = 0.0025
ZONA_EQUIVALENTE = 0.001

HEARTBEAT_INTERVAL = 21600

zona_actual = None
zona_alertada_proximidad = False
zona_consumida = False

radar0_enviado = set()
ultimo_heartbeat = 0


# =========================
# TELEGRAM
# =========================
def enviar(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Error Telegram: {e}")


# =========================
# DATOS
# =========================
def obtener_candles(interval, limit=200):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": interval, "limit": limit}

    try:
        data = requests.get(url, params=params, timeout=10).json()

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "_","_","_","_","_","_"
        ])

        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)

        return df

    except Exception as e:
        print(f"Error obteniendo datos {interval}: {e}")
        return None


# =========================
# RADAR 0
# =========================
def radar0_impulso(df):

    if len(df) < 30:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-20:-1]

    vela_actual = abs(last["close"] - last["open"])
    vela_prom = (prev["high"] - prev["low"]).mean()

    vol_actual = last["volume"]
    vol_prom = prev["volume"].mean()

    high_rango = prev["high"].max()
    low_rango = prev["low"].min()

    if (
        vela_actual > vela_prom * 1.6 and
        vol_actual > vol_prom * 1.5 and
        last["close"] > high_rango
    ):
        return "🟢 ALCISTA"

    if (
        vela_actual > vela_prom * 1.6 and
        vol_actual > vol_prom * 1.5 and
        last["close"] < low_rango
    ):
        return "🔴 BAJISTA"

    return None


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
                "tipo": "HIGH",
                "centro": c["centro"],
                "max": max(c["valores"]),
                "min": min(c["valores"]),
                "toques": len(c["valores"])
            })

    for c in clusters_low:
        if len(c["valores"]) >= MIN_TOUCHES:
            zonas.append({
                "tipo": "LOW",
                "centro": c["centro"],
                "max": max(c["valores"]),
                "min": min(c["valores"]),
                "toques": len(c["valores"])
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
    open_ = vela["open"]
    volumen = vela["volume"]

    ventana = 20

    if len(df) >= ventana:
        vol_ma = df["volume"].iloc[-ventana:].mean()
    else:
        vol_ma = volumen

    rango = high - low
    cuerpo = abs(close - open_)

    mecha_larga = (rango > 0) and (cuerpo / rango < 0.4)

    if zona["tipo"] == "HIGH":

        if high > zona["max"] and close < zona["centro"] and mecha_larga:

            if volumen > vol_ma * 1.3:
                return True

    if zona["tipo"] == "LOW":

        if low < zona["min"] and close > zona["centro"] and mecha_larga:

            if volumen > vol_ma * 1.3:
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

        if precio > zona["max"] * 1.001:
            return "🟢 ALCISTA"

    if zona["tipo"] == "LOW":

        if precio < zona["min"] * 0.999:
            return "🔴 BAJISTA"

    return None


# =========================
# HEARTBEAT
# =========================
def heartbeat(precio, num_zonas):

    global ultimo_heartbeat

    ahora = time.time()

    if ahora - ultimo_heartbeat > HEARTBEAT_INTERVAL:

        enviar(f"""
🫀 HEARTBEAT – BOT ACTIVO

Par: {SYMBOL}
Precio 1h: {int(precio)}
Zonas detectadas: {num_zonas}
Próximo heartbeat en 6h
""")

        ultimo_heartbeat = ahora


# =========================
# EVALUAR
# =========================
def evaluar():

    global zona_actual
    global zona_alertada_proximidad
    global zona_consumida

    df_macro = obtener_candles(INTERVAL_MACRO)

    if df_macro is None:
        return

    zonas = detectar_zonas(df_macro)

    if not zonas:
        return

    precio_macro = df_macro["close"].iloc[-1]

    heartbeat(precio_macro, len(zonas))

    df_entry = obtener_candles(INTERVAL_ENTRY, limit=50)

    if df_entry is None:
        return

    precio_entry = df_entry["close"].iloc[-1]

    # RADAR 0
    impulso = radar0_impulso(df_entry)

    if impulso:

        timestamp = df_entry.iloc[-1]["time"]

        if timestamp not in radar0_enviado:

            radar0_enviado.add(timestamp)

            enviar(f"""
🚨 RADAR 0 – IMPULSO DETECTADO

Dirección: {impulso}
Precio actual: {int(precio_entry)}
Volumen alto en 5m
""")

    # seleccionar zona más cercana al precio
    zona = min(zonas, key=lambda z: abs(precio_macro - z["centro"]))

    if not misma_zona(zona_actual, zona):

        zona_actual = zona
        zona_alertada_proximidad = False
        zona_consumida = False

        enviar(f"""
💰 RADAR 1 – NUEVA ZONA DE LIQUIDEZ

Tipo: {zona['tipo']}
Centro: {int(zona['centro'])}
Toques: {zona['toques']}
""")

    # RADAR 2 proximidad
    distancia_rel = abs(precio_macro - zona["centro"]) / zona["centro"]

    if distancia_rel < PROXIMITY and not zona_alertada_proximidad:

        zona_alertada_proximidad = True

        enviar(f"""
🧲 RADAR 2 – PRECIO CERCA DE LIQUIDEZ

Zona: {int(zona['centro'])}
Distancia: {distancia_rel*100:.2f}%
""")


    # RADAR 3 sweep
    if sweep(df_entry, zona):

        enviar(f"""
🚨 RADAR 3 – SWEEP DETECTADO

Zona barrida: {int(zona['centro'])}
Posible reversión
""")


    # RADAR 4 breakout
    b = breakout(precio_entry, zona)

    if b:

        zona_consumida = True

        enviar(f"""
📡 RADAR 4 – BREAKOUT {b}

Zona: {int(zona['centro'])}
Precio: {int(precio_entry)}
""")


# =========================
# LOOP
# =========================
enviar("🤖 BOT_DE_ARTURO V11.1 iniciado correctamente")

while True:

    try:
        evaluar()

    except Exception as e:

        print(f"Error: {e}")

        enviar(f"⚠️ Error bot: {str(e)[:100]}")

    time.sleep(60)
