# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os
from datetime import datetime, timedelta, UTC

# =========================
# CONFIGURACIÓN INICIAL (Variables globales ajustables)
# =========================

# Tokens de Telegram (obligatorio configurarlas como variables de entorno)
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

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
IMPULSE_RANGE = 1.3         # % mínimo de rango de la vela (high-low)/close (ej. 1.3 = 1.3%)
IMPULSE_VOLUME = 1.1        # Volumen mínimo relativo a la media móvil de 20 (1.1 = 110% del promedio)
IMPULSE_COOLDOWN = 900      # Segundos entre alertas de impulso (900 = 15 minutos)

# Variables de estado internas (no modificar)
last_impulse_time = None
zona_actual = None
zona_alertada_proximidad = False
zona_consumida = False


# =========================
# FUNCIÓN PARA ENVIAR MENSAJES POR TELEGRAM
# =========================
def enviar(msg):
    """Envía un mensaje de texto al chat de Telegram configurado."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": msg
        }, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")


# =========================
# OBTENER VELAS DE BINANCE
# =========================
def obtener_candles(interval, limit=200):
    """
    Descarga velas de Binance para el símbolo y intervalo indicados.
    Retorna un DataFrame con columnas: time, open, high, low, close, volume.
    Incluye timeout de 10 segundos para evitar bloqueos.
    """
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "limit": limit
    }
    try:
        data = requests.get(url, params=params, timeout=10).json()
    except Exception as e:
        print(f"Error obteniendo velas de Binance: {e}")
        # Devolver DataFrame vacío en caso de error
        return pd.DataFrame(columns=["time","open","high","low","close","volume"])
    
    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "_","_","_","_","_","_"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df


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
    """
    Determina si dos zonas (por su centro) son equivalentes según ZONA_EQUIVALENTE.
    Útil para no repetir alertas cuando la zona se mantiene.
    """
    if z1 is None or z2 is None:
        return False
    return abs(z1["centro"] - z2["centro"]) / z1["centro"] < ZONA_EQUIVALENTE


# =========================
# RADAR 0 - DETECCIÓN DE IMPULSOS
# =========================
def radar_impulse(df):
    """
    Detecta velas con gran rango y volumen superior al promedio.
    Utiliza los datos de entrada (INTERVAL_ENTRY) y compara con:
      - IMPULSE_RANGE: % mínimo de rango (high-low)/close
      - IMPULSE_VOLUME: relación volumen / media móvil 20
    Además respeta un cooldown (IMPULSE_COOLDOWN) para no saturar.
    Ajusta estos valores para hacer el radar más o menos sensible.
    """
    global last_impulse_time
    
    if df.empty or len(df) < 20:
        return
        
    vela = df.iloc[-1]
    rango = (vela["high"] - vela["low"]) / vela["close"] * 100  # en porcentaje
    vol_rel = vela["volume"] / df["volume"].rolling(20).mean().iloc[-1]

    if rango > IMPULSE_RANGE and vol_rel > IMPULSE_VOLUME:
        ahora = datetime.now(UTC)
        if last_impulse_time is None or (ahora - last_impulse_time).seconds > IMPULSE_COOLDOWN:
            precio = vela["close"]
            enviar(f"""
⚡ RADAR 0 — IMPULSO DETECTADO
Hora UTC: {ahora.strftime("%H:%M")}
Precio: {int(precio)}
Rango: {rango:.2f}%
Volumen: {vol_rel:.2f}x media
Movimiento anómalo en {INTERVAL_ENTRY}
""")
            last_impulse_time = ahora


# =========================
# DETECCIÓN DE SWEEP (RADAR 3)
# =========================
def sweep(df, zona):
    """
    Detecta si en la última vela de entrada se ha barrido la zona:
    - Para zona HIGH: precio supera el máximo y cierra por debajo del centro.
    - Para zona LOW: precio baja del mínimo y cierra por encima del centro.
    Requiere que el volumen sea al menos 1.5 veces el promedio.
    Retorna True si hay sweep.
    """
    if df.empty or len(df) < 20:
        return False
        
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
# DETECCIÓN DE BREAKOUT (RADAR 4)
# =========================
def breakout(precio, zona):
    """
    Detecta si el precio actual supera la zona con un margen del 0.3%:
    - Para HIGH: precio > max * 1.003  → ruptura alcista
    - Para LOW: precio < min * 0.997   → ruptura bajista
    Retorna 'UP' o 'DOWN' si hay breakout, None en caso contrario.
    Solo se considera si la zona no ha sido consumida antes.
    """
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
    """
    Ejecuta el flujo completo del bot en cada ciclo:
    1. Obtiene datos macro y detecta la zona principal (RADAR 1 si cambia).
    2. Si el precio está cerca de la zona, envía RADAR 2.
    3. Analiza velas de entrada para RADAR 0 (impulsos).
    4. Detecta sweep (RADAR 3) y breakout (RADAR 4).
    """
    global zona_actual
    global zona_alertada_proximidad
    global zona_consumida

    # --- Parte macro: detección de zonas ---
    df_macro = obtener_candles(INTERVAL_MACRO)
    if df_macro.empty:
        print("No se pudieron obtener datos macro, reintentando...")
        return
        
    zonas = detectar_zonas(df_macro)
    if not zonas:
        return
    zona = zonas[0]
    precio = df_macro["close"].iloc[-1]

    # Si la zona ha cambiado (nueva zona), reiniciamos estado y enviamos RADAR 1
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
💰 RADAR 1 - NUEVA ZONA DE LIQUIDEZ

Tipo: {tipo}
Centro: {centro} (rango {zmin}-{zmax})
Precio actual: {precio_i}
Distancia al centro: {distancia}$
""")

    # Proximidad a la zona (RADAR 2)
    distancia_rel = abs(precio - zona["centro"]) / precio
    if distancia_rel < PROXIMITY and not zona_alertada_proximidad:
        zona_alertada_proximidad = True
        enviar(f"""
🧲 RADAR 2 - PROXIMIDAD A LIQUIDEZ

Zona: {int(zona['centro'])} ({int(zona['min'])}-{int(zona['max'])})
Precio actual: {int(precio)}
Distancia: {distancia_rel*100:.2f}%
""")

    # --- Parte fina: análisis en intervalo de entrada ---
    df_entry = obtener_candles(INTERVAL_ENTRY)
    if df_entry.empty:
        print("No se pudieron obtener datos de entrada")
        return

    # RADAR 0 - Impulsos (siempre activo)
    radar_impulse(df_entry)

    # RADAR 3 - Sweep
    if sweep(df_entry, zona):
        enviar(f"""
🚨 RADAR 3 - SWEEP DETECTADO

Zona barrida: {int(zona['centro'])}
Señal de posible reversión.
""")

    # RADAR 4 - Breakout
    b = breakout(df_entry["close"].iloc[-1], zona)
    if b:
        zona_consumida = True
        direccion = "🟢 BULLISH (ALCISTA)" if b=="UP" else "🔴 BEARISH (BAJISTA)"
        enviar(f"""
📡 RADAR 4 - BREAKOUT CONFIRMADO

Dirección: {direccion}
Liquidez del nivel {int(zona['centro'])} absorbida.
Precio actual: {int(df_entry['close'].iloc[-1])}
""")


# =========================
# MENSAJE DE INICIO INMEDIATO
# =========================
print("🚀 BOT DE ARTURO V10 iniciado - Enviando mensaje de confirmación...")
enviar("🤖 BOT BTC INICIADO (V10 con RADAR 0,1,2,3,4 operativos)")


# =========================
# BUCLE PRINCIPAL
# =========================
while True:
    try:
        evaluar()
    except Exception as e:
        print(f"Error en el ciclo principal: {e}")
        enviar(f"⚠️ ERROR EN BOT: {str(e)[:100]}")  # Enviar error acortado
    time.sleep(60)   # Esperar 60 segundos antes de la siguiente ejecución
