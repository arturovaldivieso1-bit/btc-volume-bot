Ajustee: import requests
import pandas as pd
import time
import os

print("BOT_DE_ARTURO V10.3 LIQUIDEZ PRO iniciado 🚀")

# =========================
# CONFIG (desde variables de entorno)
# =========================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOL = "BTCUSDT"

TF_LIQUIDITY = "1h"      # Temporalidad para detectar zonas (estable)
TF_ENTRY = "5m"          # Temporalidad para entradas (rápida)

LOOKBACK = 100           # Ventana para buscar zonas
MIN_TOUCHES = 3          # REDUCIDO: antes 4, ahora 3 (más sensible)
CLUSTER_RANGE = 0.002    # AUMENTADO: antes 0.0015, ahora 0.2% (agrupa mejor)
PROXIMITY = 0.0025       # AUMENTADO: antes 0.0015, ahora 0.25% (avisa antes)
MICRO_ZONE_FILTER = 0.001 # Se mantiene para evitar ruido

HEARTBEAT_INTERVAL = 21600  # 6 horas
last_heartbeat = 0

# Sets para evitar duplicados
zonas_r1 = set()
zonas_r2 = set()
zonas_r3 = set()
zonas_r4 = set()
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
# CLUSTER (igual que V10.2)
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
    
    # Ordenar por toques
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
    
    # Detectar mecha larga (cuerpo pequeño comparado con rango)
    rango = high - low
    cuerpo = abs(close - open_)
    mecha_larga = (rango > 0) and (cuerpo / rango < 0.4)  # Cuerpo < 40% del rango
    
    if z["type"] == "HIGH":
        # Sweep alcista: toca resistencia y cierra debajo, con volumen y mecha
        if high > z["max"] and close < z["center"] and vol > ma * 1.3 and mecha_larga:
            return True
    elif z["type"] == "LOW":
        # Sweep bajista: toca soporte y cierra arriba, con volumen y mecha
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
Precio: {int(price)}
Zonas detectadas: {num_zonas}
Próximo heartbeat en 6h
""")
        last_heartbeat = now

# =========================
# EVALUAR (versión optimizada)
# =========================
def evaluate():
    global zona_activa, last_heartbeat

    # 1. Obtener datos de 1h (para zonas)
    df_lq = candles(TF_LIQUIDITY)
    if df_lq is None:
        return
    zones = detect_zones(df_lq)
    if not zones:
        return
    
    price_1h = df_lq["close"].iloc[-1]
    
    # Heartbeat
    heartbeat(price_1h, len(zones))
    
    # 2. Seleccionar zona activa si no existe
    if zona_activa is None:
        for z in zones:
            if liquidity_score(z) >= 3:  # Solo zonas de alta calidad
                zona_activa = z
                break
    
    if zona_activa is None:
        return
    
    z = zona_activa
    score = liquidity_score(z)
    level = int(z["center"])
    
    # 3. Obtener datos de 5m (para entradas)
    df_en = candles(TF_ENTRY, limit=50)
    if df_en is None:
        return
    close5 = df_en.iloc[-1]["close"]
    dist = abs(price_1h - z["center"]) / price_1h * 100
    side = "🟢 HIGH" if price_1h < z["center"] else "🔴 LOW"
    
    # ---- RADAR 1: detección inicial ----
    if level not in zonas_r1:
        zonas_r1.add(level)
        send(f"""
💰 RADAR 1 - Zona de liquidez

Tipo: {side}
Zona: {level}
Rango: {int(z['min'])}-{int(z['max'])}
Toques: {z['touches']}
Score: {score}
Precio 1h: {int(price_1h)}
""")
    
    # ---- RADAR 2: acercamiento (más sensible) ----
    if dist < PROXIMITY * 100 and level not in zonas_r2:
        zonas_r2.add(level)
        send(f"""
🔎 RADAR 2 - Acercándose

Tipo: {side}
Zona: {level}
Distancia: {dist:.2f}%
Precio actual 5m: {int(close5)}
""")
    
    # ---- RADAR 3: sweep (más reactivo) ----
    if sweep(df_en, z) and level not in zonas_r3:
        zonas_r3.add(level)
        send(f"""
🔄 RADAR 3 - Sweep detectado

Tipo: {side}
Zona barrida: {level}
Precio 5m: {int(close5)}
Posible reversión inminente
""")
    
    # ---- RADAR 4: breakout confirmado ----
    if z["type"] == "LOW" and close5 < z["min"] and level not in zonas_r4:
        zonas_r4.add(level)
        send(f"""
💥 RADAR 4 - BREAKOUT BAJISTA

Liquidez absorbida (soporte roto)
Zona: {level}
Precio 5m: {int(close5)}
""")
        zona_activa = None  # Zona agotada
    
    elif z["type"] == "HIGH" and close5 > z["max"] and level not in zonas_r4:
        zonas_r4.add(level)
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
    send("🤖 BOT_DE_ARTURO V10.3 LIQUIDEZ PRO iniciado correctamente")
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
