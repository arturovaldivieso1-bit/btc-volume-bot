# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V10 iniciado 🚀")

# =========================
# CONFIGURACIÓN INICIAL (Variables globales ajustables)
# =========================

# Tokens de Telegram (obtenidos de variables de entorno)
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Par de trading
SYMBOL = "BTCUSDT"

# Intervalos de tiempo para análisis macro (detección de zonas) y entrada (sweep/breakout)
INTERVAL_MACRO = "1h"      # Temporalidad mayor para identificar zonas de liquidez
INTERVAL_ENTRY = "5m"      # Temporalidad menor para detectar movimientos finos

# Número de velas hacia atrás para buscar toques en la zona macro
LOOKBACK = 100

# Mínimo de toques para considerar una zona válida (ajustar: mayor = más filtro)
MIN_TOUCHES = 4

# Rango para agrupar precios en un mismo clúster (porcentaje respecto al precio)
# Ejemplo: 0.002 = 0.2% (más pequeño = zonas más ajustadas)
CLUSTER_RANGE = 0.002

# Distancia para alertar proximidad a la zona (porcentaje respecto al precio)
PROXIMITY = 0.0015  # 0.15%

# Rango para considerar dos zonas como la misma (evita duplicados)
ZONA_EQUIVALENTE = 0.001  # 0.1%

# Variables de estado (no tocar, las usa el bot internamente)
zona_actual = None
zona_alertada_proximidad = False
zona_consumida = False


# =========================
# FUNCIÓN PARA ENVIAR MENSAJES POR TELEGRAM
# =========================
def enviar(msg):
    """Envía un mensaje de texto al chat de Telegram configurado."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": msg
    })


# =========================
# OBTENER VELAS DE BINANCE
# =========================
def obtener_candles(interval, limit=200):
    """
    Descarga velas de Binance para el símbolo y intervalo indicados.
    Retorna un DataFrame con columnas: time, open, high, low, close, volume.
    """
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

    # Convertir a numérico
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
# DETECCIÓN DE SWEEP (barrido de liquidez)
# =========================
def sweep(df, zona):
    """
    Detecta si en la última vela de entrada (entry) se ha barrido la zona:
    - Para zona HIGH: precio supera el máximo y cierra por debajo del centro.
    - Para zona LOW: precio baja del mínimo y cierra por encima del centro.
    Además, requiere que el volumen sea al menos 1.5 veces el promedio.
    Retorna True si hay sweep.
    """
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
# DETECCIÓN DE BREAKOUT (ruptura de zona)
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
    1. Obtiene datos macro y detecta la zona principal.
    2. Si la zona cambia, envía RADAR 1 (nueva zona detectada).
    3. Si el precio está cerca de la zona, envía RADAR 2.
    4. Si hay sweep en entry, envía RADAR 3.
    5. Si hay breakout, envía RADAR 4 y marca zona como consumida.
    """
    global zona_actual
    global zona_alertada_proximidad
    global zona_consumida

    # Obtener velas macro y detectar zonas
    df_macro = obtener_candles(INTERVAL_MACRO)
    zonas = detectar_zonas(df_macro)

    if not zonas:
        return

    # Tomar la zona con más toques
    zona = zonas[0]
    precio = df_macro["close"].iloc[-1]

    # Si la zona ha cambiado (no es la misma que antes), reiniciamos estado
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

    # Calcular distancia relativa al centro de la zona
    distancia = abs(precio - zona["centro"]) / precio

    # Si estamos cerca y no se ha alertado aún, enviar RADAR 2
    if distancia < PROXIMITY and not zona_alertada_proximidad:
        zona_alertada_proximidad = True
        enviar(f"""
🧲 RADAR 2 - PROXIMIDAD A LIQUIDEZ

Zona: {int(zona['centro'])} ({int(zona['min'])}-{int(zona['max'])})
Precio actual: {int(precio)}
Distancia: {distancia*100:.2f}%
""")

    # Obtener velas de entrada para análisis fino
    df_entry = obtener_candles(INTERVAL_ENTRY)

    # Detectar sweep
    if sweep(df_entry, zona):
        enviar(f"""
🚨 RADAR 3 - SWEEP DETECTADO

Zona barrida: {int(zona['centro'])}
Señal de posible reversión.
""")

    # Detectar breakout
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
# BUCLE PRINCIPAL
# =========================
while True:
    try:
        evaluar()
    except Exception as e:
        print("Error en el ciclo principal:", e)
    time.sleep(60)   # Esperar 60 segundos antes de la siguiente ejecución
