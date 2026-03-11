# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os
from datetime import datetime, UTC

print("======================================")
print("🚀 BOT_DE_ARTURO V11 iniciado")
print("Sistema de radares institucional")
print("0 Movimientos | 1 Liquidez | 2 Proximidad | 3 Sweep | 4 Breakout")
print("Heartbeat activo...")
print("======================================")

# =========================
# CONFIGURACIÓN INICIAL (Variables globales ajustables)
# =========================

# Tokens de Telegram (obligatorio configurarlas como variables de entorno)
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise ValueError("⚠️ Debes configurar TOKEN y CHAT_ID como variables de entorno")

# Par de trading
SYMBOL = "BTCUSDT"

# Intervalos de tiempo
INTERVAL_MACRO = "1h"      # Temporalidad mayor para detectar zonas de liquidez
INTERVAL_ENTRY = "5m"      # Temporalidad menor para análisis fino (sweep, breakout, impulsos)

# Parámetros para detección de zonas de liquidez (RADAR 1)
LOOKBACK = 100              # Número de velas hacia atrás para buscar toques
MIN_TOUCHES = 4             # Mínimo de toques para considerar una zona válida (mayor = más filtro)
CLUSTER_RANGE = 0.002       # 0.2% - Rango para agrupar precios en un mismo clúster
PROXIMITY = 0.0015          # 0.15% - Distancia para alertar proximidad a la zona (RADAR 2)
ZONA_EQUIVALENTE = 0.001    # 0.1% - Tolerancia para considerar dos zonas como la misma

# Parámetros para RADAR 0 (detección de impulsos)
IMPULSE_RANGE = 1.3         # % mínimo de rango de la vela (high-low)/close
IMPULSE_VOLUME = 1.1        # Volumen mínimo relativo a la media móvil de 20
IMPULSE_COOLDOWN = 900      # Segundos entre alertas de impulso

# Cooldown sweep
SWEEP_COOLDOWN = 600

# Variables de estado internas (no modificar)
last_impulse_time = None
last_sweep_time = None

zona_actual = None
zona_alertada_proximidad = False
zona_consumida = False

last_heartbeat = datetime.now(UTC)


# =========================
# FUNCIÓN PARA ENVIAR MENSAJES POR TELEGRAM
# =========================
def enviar(msg):
    """Envía un mensaje de texto al chat de Telegram configurado."""
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": msg.strip()
        }, timeout=10)
    except Exception as e:
        print("Error enviando Telegram:", e)


# =========================
# OBTENER VELAS DE BINANCE
# =========================
def obtener_candles(interval, limit=200):
    """
    Descarga velas de Binance para el símbolo y intervalo indicados.
    Retorna un DataFrame con columnas: time, open, high, low, close, volume.
    """

    try:

        url = "https://api.binance.com/api/v3/klines"

        params = {
            "symbol": SYMBOL,
            "interval": interval,
            "limit": limit
        }

        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()

        data = r.json()

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "_","_","_","_","_","_"
        ])

        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)

        return df

    except Exception as e:

        print("Error descargando velas:", e)

        return pd.DataFrame()


# =========================
# AGRUPAR PRECIOS EN CLÚSTERES
# =========================
def cluster(lista):
    """
    Agrupa una lista de precios en clústeres según CLUSTER_RANGE.
    Retorna una lista de clústeres, cada uno con:
        - centro: promedio de los valores del clúster
        - valores: lista de precios agrupados
    """

    clusters = []

    for p in sorted(lista):

        agregado = False

        for c in clusters:

            # Si la distancia relativa al centro del clúster es menor que el rango, se agrega
            if abs(p - c["centro"]) / c["centro"] < CLUSTER_RANGE:

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
# DETECTAR ZONAS DE LIQUIDEZ
# =========================
def detectar_zonas(df):
    """
    Analiza el DataFrame macro (LOOKBACK velas) para identificar zonas de liquidez:
    - Clústeres de máximos (resistencia) y mínimos (soporte) con suficientes toques.
    Retorna una lista de zonas ordenadas por toques (mayor primero).
    Cada zona contiene:
        - tipo: 'HIGH' o 'LOW'
        - centro: precio promedio del clúster
        - max, min: extremos del clúster
        - toques: número de veces que se tocó la zona
    """

    if df.empty:
        return []

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
# COMPARAR SI DOS ZONAS SON LA MISMA
# =========================
def misma_zona(z1, z2):

    if z1 is None or z2 is None:
        return False

    return abs(z1["centro"] - z2["centro"]) / z1["centro"] < ZONA_EQUIVALENTE


# =========================
# RADAR 0 - DETECCIÓN DE IMPULSOS
# =========================
def radar_impulse(df):

    global last_impulse_time

    if df.empty:
        return

    vela = df.iloc[-1]

    vol_ma = df["volume"].rolling(20).mean().iloc[-1]

    if pd.isna(vol_ma):
        return

    rango = (vela["high"] - vela["low"]) / vela["close"] * 100

    vol_rel = vela["volume"] / vol_ma

    if rango > IMPULSE_RANGE and vol_rel > IMPULSE_VOLUME:

        ahora = datetime.now(UTC)

        if last_impulse_time is None or (ahora - last_impulse_time).total_seconds() > IMPULSE_COOLDOWN:

            precio = vela["close"]

            enviar(f"""
⚡ RADAR 0 — IMPULSO DETECTADO

Hora UTC: {ahora.strftime("%H:%M")}
Precio: {int(precio)}

Rango: {rango:.2f}%
Volumen: {vol_rel:.2f}x media

Movimiento anómalo en {INTERVAL_ENTRY}
""")

            print("⚡ IMPULSO detectado")

            last_impulse_time = ahora


# =========================
# DETECCIÓN DE SWEEP (RADAR 3)
# =========================
def sweep(df, zona):

    global last_sweep_time

    if df.empty:
        return False

    vela = df.iloc[-1]

    vol_ma = df["volume"].rolling(20).mean().iloc[-1]

    if pd.isna(vol_ma):
        return False

    ahora = datetime.now(UTC)

    if last_sweep_time and (ahora - last_sweep_time).total_seconds() < SWEEP_COOLDOWN:
        return False

    high = vela["high"]
    low = vela["low"]
    close = vela["close"]

    rechazo = False

    if zona["tipo"] == "HIGH":
        if high > zona["max"] and close < zona["centro"]:
            rechazo = True

    if zona["tipo"] == "LOW":
        if low < zona["min"] and close > zona["centro"]:
            rechazo = True

    if rechazo and vela["volume"] > vol_ma * 1.5:

        last_sweep_time = ahora

        return True

    return False


# =========================
# DETECCIÓN DE BREAKOUT (RADAR 4)
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
# FUNCIÓN PRINCIPAL DE EVALUACIÓN
# =========================
def evaluar():

    global zona_actual
    global zona_alertada_proximidad
    global zona_consumida
    global last_heartbeat

    df_macro = obtener_candles(INTERVAL_MACRO,150)

    if df_macro.empty:
        return

    zonas = detectar_zonas(df_macro)

    if not zonas:
        return

    zona = zonas[0]

    precio = df_macro["close"].iloc[-1]

    # RADAR 1
    if not misma_zona(zona_actual, zona):

        zona_actual = zona
        zona_alertada_proximidad = False
        zona_consumida = False

        enviar(f"""
💰 RADAR 1 - NUEVA ZONA DE LIQUIDEZ

Tipo: {zona["tipo"]}
Centro: {int(zona["centro"])}

Rango: {int(zona["min"])} - {int(zona["max"])}

Precio actual: {int(precio)}
""")

        print("💰 Nueva zona detectada")

    # RADAR 2
    distancia_rel = abs(precio - zona["centro"]) / zona["centro"]

    if distancia_rel < PROXIMITY and not zona_alertada_proximidad:

        zona_alertada_proximidad = True

        enviar(f"""
🧲 RADAR 2 - PROXIMIDAD A LIQUIDEZ

Zona: {int(zona['centro'])}
Precio actual: {int(precio)}

Distancia: {distancia_rel*100:.2f}%
""")

        print("🧲 Proximidad detectada")

    df_entry = obtener_candles(INTERVAL_ENTRY,100)

    radar_impulse(df_entry)

    # RADAR 3
    if sweep(df_entry, zona):

        enviar(f"""
🚨 RADAR 3 - SWEEP DETECTADO

Zona barrida: {int(zona['centro'])}

Señal de posible reversión.
""")

        print("🚨 SWEEP detectado")

    # RADAR 4
    b = breakout(df_entry["close"].iloc[-1], zona)

    if b:

        zona_consumida = True

        direccion = "ALCISTA" if b=="UP" else "BAJISTA"

        enviar(f"""
📡 RADAR 4 - BREAKOUT CONFIRMADO

Dirección: {direccion}

Liquidez del nivel {int(zona['centro'])} absorbida.

Precio actual: {int(df_entry['close'].iloc[-1])}
""")

        print("📡 BREAKOUT detectado")

    # HEARTBEAT
    ahora = datetime.now(UTC)

    if (ahora - last_heartbeat).total_seconds() > 900:

        print(f"💓 Heartbeat activo {ahora.strftime('%H:%M:%S')} UTC")

        last_heartbeat = ahora


# =========================
# BUCLE PRINCIPAL
# =========================
while True:

    try:

        evaluar()

    except Exception as e:

        print(datetime.now(UTC),"Error en el ciclo principal:", e)

    time.sleep(60)
