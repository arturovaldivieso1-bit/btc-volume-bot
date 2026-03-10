import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V11.1 iniciado 🚀")

# =========================
# CONFIG (variables de entorno)
# =========================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOL = "BTCUSDT"

TF_LIQUIDITY = "1h"      # Temporalidad para detectar zonas (estable)
TF_ENTRY = "5m"          # Temporalidad para entradas (rápida)

LOOKBACK = 100           # Ventana para buscar zonas
MIN_TOUCHES = 3          # Más sensible (antes 4)
CLUSTER_RANGE = 0.002    # Agrupa mejor (0.2%)
PROXIMITY = 0.0025       # Radar 2 avisa antes (0.25%)
MICRO_ZONE_FILTER = 0.001 # Elimina micro-zonas

HEARTBEAT_INTERVAL = 21600  # 6 horas
last_heartbeat = 0

# Sets para evitar duplicados en cada radar
radar0_enviado = set()
radar1_enviado = set()
radar2_enviado = set()
radar3_enviado = set()
radar4_enviado = set()

zona_activa = None

# =========================
# TELEGRAM
# =========================
def send(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Error Telegram: {e}")

# =========================
# DATOS (con timeout)
# =========================
def candles(interval, limit=200):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": interval, "limit": limit}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        df = pd.DataFrame(data)
        df = df[[1, 2, 3, 4, 5]]
        df.columns = ["open", "high", "low", "close", "volume"]
        df = df.astype(float)
        return df
    except Exception as e:
        print(f"Error obteniendo datos {interval}: {e}")
        return None

# =========================
# RADAR 0 (IMPULSO EN 5m)
# =========================
def radar0_impulso(df):
    if len(df) < 30:
        return None
    last = df.iloc[-1]
    prev = df.iloc[-20:-1]  # últimas 19 velas (excluye la actual)
    vela_actual = abs(last["close"] - last["open"])
    vela_prom = (prev["high"] - prev["low"]).mean()
    vol_actual = last["volume"]
    vol_prom = prev["volume"].mean()
    high_rango = prev["high"].max()
    low_rango = prev["low"].min()

    # Umbrales ligeramente reducidos para ser más reactivo en 5m
    impulso_alcista = (
        vela_actual > vela_prom * 1.6 and
        vol_actual > vol_prom * 1.5 and
        last["close"] > high_rango
    )
    impulso_bajista = (
        vela_actual > vela_prom * 1.6 and
        vol_actual > vol_prom * 1.5 and
        last["close"] < low_rango
    )
    if impulso_alcista:
        return "ALCISTA"
    if impulso_bajista:
        return "BAJISTA"
    return None

# =========================
# CLUSTER (igual)
# =========================
def cluster(prices):
    clusters = []
    for p in sorted(prices):
        added = False
        for c in clusters:
            if abs(p - c["center"]) / p < CLUSTER_RANGE:
                c["values"].append(p)
                c["center"] = sum(c["values"]) / len(c["values"])
                added = True
                break
        if not added:
            clusters.append({"center": p, "values": [p]})
    return clusters

# =========================
# DETECTAR ZONAS (mejorada)
# =========================
def detect_zones(df):
    highs = df["high"].tail(LOOKBACK).tolist()
    lows = df["low"].tail(LOOKBACK).tolist()
    
    ch = cluster(highs)
    cl = cluster(lows)
    zones = []
    
    for c in ch:
        if len(c["values"]) >= MIN_TOUCHES:
            zones.append({
                "type": "HIGH",
                "center": c["center"],
                "min": min(c["values"]),
                "max": max(c["values"]),
                "touches": len(c["values"])
            })
    for c in cl:
        if len(c["values"]) >= MIN_TOUCHES:
            zones.append({
                "type": "LOW",
                "center": c["center"],
                "min": min(c["values"]),
                "max": max(c["values"]),
                "touches": len(c["values"])
            })
    
    zones = sorted(zones, key=lambda z: z["touches"], reverse=True)
    
    # Filtro anti micro-zonas
    filtered = []
    for z in zones:
        keep = True
        for f in filtered:
            if abs(z["center"] - f["center"]) / z["center"] < MICRO_ZONE_FILTER:
                keep = False
                break
        if keep:
            filtered.append(z)
    return filtered

# =========================
# SCORE (ponderado)
# =========================
def liquidity_score(z):
    score = 0
    if z["touches"] > 8:
        score += 3
    elif z["touches"] > 5:
        score += 2
    else:
        score += 1
    spread = (z["max"] - z["min"]) / z["center"]
    if spread < 0.001:
        score += 3
    elif spread < 0.002:
        score += 2
    else:
        score += 1
    return score

# =========================
# SWEEP (más reactivo en 5m)
# =========================
def sweep(df5, z):
    v = df5.iloc[-1]
    high = v["high"]
    low = v["low"]
    close = v["close"]
    open_ = v["open"]
    vol = v["volume"]
    ma = df5["volume"].rolling(20).mean().iloc[-1]
    
    rango = high - low
    cuerpo = abs(close - open_)
    mecha_larga = (rango > 0) and (cuerpo / rango < 0.4)  # Cuerpo < 40%
    
    if z["type"] == "HIGH":
        if high > z["max"] and close < z["center"] and vol > ma * 1.3 and mecha_larga:
            return True
    elif z["type"] == "LOW":
        if low < z["min"] and close > z["center"] and vol > ma * 1.3 and mecha_larga:
            return True
    return False

# =========================
# HEARTBEAT
# =========================
def heartbeat(price, num_zonas):
    global last_heartbeat
    now = time.time()
    if now - last_heartbeat > HEARTBEAT_INTERVAL:
        send(f"""
🫀 HEARTBEAT - BOT ACTIVO

Par: {SYMBOL}
Precio 1h: {int(price)}
Zonas detectadas: {num_zonas}
Próximo heartbeat en 6h
""")
        last_heartbeat = now

# =========================
# EVALUAR (versión con radar 0)
# =========================
def evaluate():
    global zona_activa, last_heartbeat

    # 1. Obtener datos de 1h (para zonas)
    df_lq = candles(TF_LIQUIDITY)
    if df_lq is None:
        return
    zones = detect_zones(df_lq)
    price_1h = df_lq["close"].iloc[-1]
    
    # Heartbeat
    heartbeat(price_1h, len(zones))
    
    # 2. Obtener datos de 5m (para entradas y radar 0)
    df_en = candles(TF_ENTRY, limit=50)
    if df_en is None:
        return
    close5 = df_en.iloc[-1]["close"]
    
    # --- RADAR 0: impulsos en 5m ---
    impulso = radar0_impulso(df_en)
    if impulso:
        candle_time = df_en.iloc[-1]["timestamp"]
        if candle_time not in radar0_enviado:
            radar0_enviado.add(candle_time)
            delta = close5 - df_en.iloc[-1]["open"]
            send(f"""
🚨 RADAR 0 - IMPULSO DETECTADO

Dirección: {impulso}
Δ precio: {delta:.2f} USD
Volumen alto en 5m
Precio actual: {int(close5)}
""")
    
    # 3. Seleccionar zona activa si no existe
    if zona_activa is None:
        for z in zones:
            if liquidity_score(z) >= 3:
                zona_activa = z
                break
    
    if zona_activa is None:
        return
    
    z = zona_activa
    score = liquidity_score(z)
    level = int(z["center"])
    dist = abs(price_1h - z["center"]) / price_1h * 100
    side = "🟢 HIGH" if price_1h < z["center"] else "🔴 LOW"
    
    # ---- RADAR 1 ----
    if level not in radar1_enviado:
        radar1_enviado.add(level)
        send(f"""
💰 RADAR 1 - Zona de liquidez

Tipo: {side}
Zona: {level}
Rango: {int(z['min'])}-{int(z['max'])}
Toques: {z['touches']}
Score: {score}
Precio 1h: {int(price_1h)}
""")
    
    # ---- RADAR 2 ----
    if dist < PROXIMITY * 100 and level not in radar2_enviado:
        radar2_enviado.add(level)
        send(f"""
🔎 RADAR 2 - Acercándose

Tipo: {side}
Zona: {level}
Distancia: {dist:.2f}%
Precio 5m: {int(close5)}
""")
    
    # ---- RADAR 3 ----
    if sweep(df_en, z) and level not in radar3_enviado:
        radar3_enviado.add(level)
        send(f"""
🔄 RADAR 3 - Sweep detectado

Tipo: {side}
Zona barrida: {level}
Precio 5m: {int(close5)}
Posible reversión inminente
""")
    
    # ---- RADAR 4 ----
    if z["type"] == "LOW" and close5 < z["min"] and level not in radar4_enviado:
        radar4_enviado.add(level)
        send(f"""
💥 RADAR 4 - BREAKOUT BAJISTA

Liquidez absorbida (soporte roto)
Zona: {level}
Precio 5m: {int(close5)}
""")
        zona_activa = None
    elif z["type"] == "HIGH" and close5 > z["max"] and level not in radar4_enviado:
        radar4_enviado.add(level)
        send(f"""
💥 RADAR 4 - BREAKOUT ALCISTA

Liquidez absorbida (resistencia rota)
Zona: {level}
Precio 5m: {int(close5)}
""")
        zona_activa = None

# =========================
# LOOP PRINCIPAL
# =========================
# Mensaje de inicio
try:
    send("🤖 BOT_DE_ARTURO V10.4 (con Radar 0) iniciado correctamente")
    print("Mensaje de inicio enviado")
except:
    print("No se pudo enviar mensaje de inicio - revisa TOKEN y CHAT_ID")

print("Ciclo principal cada 60 segundos...")
while True:
    try:
        evaluate()
    except Exception as e:
        print(f"Error en loop: {e}")
        send(f"⚠️ Error en bot: {str(e)[:100]}")
    time.sleep(60)
