import requests
import pandas as pd
import time
import numpy as np

print("BOT_DE_ARTURO V11.5 (liquidez optimizada) iniciado 🚀")

# =========================
# CONFIG
# =========================

SYMBOL = "BTCUSDT"
INTERVAL = "5m"
LIMIT = 200

TELEGRAM_TOKEN = "TU_TOKEN"
TELEGRAM_CHAT_ID = "TU_CHAT_ID"

MICRO_ZONE_FILTER = 0.004

HEARTBEAT_INTERVAL = 14400  # 4 horas
ultimo_heartbeat = time.time()

zona_activa = None

radar0_enviado = set()
radar1_enviado = set()
radar2_enviado = set()
radar3_enviado = set()
radar4_enviado = set()

# Cache para zonas
ultimas_zonas = []
ultimo_calculo_zonas = 0
CACHE_ZONAS_SEGUNDOS = 300  # 5 minutos

# =========================
# TELEGRAM
# =========================

def enviar_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except:
        print("Error enviando mensaje Telegram")

# =========================
# HEARTBEAT
# =========================

def heartbeat():
    global ultimo_heartbeat
    ahora = time.time()
    if ahora - ultimo_heartbeat > HEARTBEAT_INTERVAL:
        enviar_telegram(
"""💓 HEARTBEAT

BOT_DE_ARTURO V11.5 sigue activo
Monitoreando BTCUSDT
"""
        )
        ultimo_heartbeat = ahora

# =========================
# LOG DE INICIO
# =========================

# Enviamos mensaje de inicio al arrancar
try:
    enviar_telegram(
"""🤖 BOT_DE_ARTURO V11.5 INICIADO

✅ Versión: 11.5 (liquidez optimizada)
📊 Símbolo: BTCUSDT
⏱️ Intervalo: 5m
🎯 Filtro zonas: 0.4%
💓 Heartbeat: cada 4h

Sistema operativo y monitoreando...
"""
    )
    print("Mensaje de inicio enviado a Telegram")
except:
    print("No se pudo enviar mensaje de inicio (quizás token incorrecto)")

# =========================
# DATA
# =========================

def get_data():
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "limit": LIMIT
    }
    try:
        data = requests.get(url, params=params, timeout=10).json()
        df = pd.DataFrame(data, columns=[
            "timestamp","open","high","low","close","volume",
            "close_time","qav","trades","tbbav","tbqav","ignore"
        ])
        df = df.astype(float)
        return df
    except Exception as e:
        print(f"Error obteniendo datos: {e}")
        return None

# =========================
# RADAR 0 (IMPULSO)
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

    impulso_alcista = (
        vela_actual > vela_prom * 1.8 and
        vol_actual > vol_prom * 1.7 and
        last["close"] > high_rango
    )
    impulso_bajista = (
        vela_actual > vela_prom * 1.8 and
        vol_actual > vol_prom * 1.7 and
        last["close"] < low_rango
    )
    if impulso_alcista:
        return "ALCISTA"
    if impulso_bajista:
        return "BAJISTA"
    return None

# =========================
# DETECTAR ZONAS (optimizada)
# =========================

def detect_zones(df):
    highs = df["high"].values
    lows = df["low"].values
    zones = []
    
    # Muestreo cada 3 velas para rendimiento
    for i in range(0, len(df)-5, 3):
        h = highs[i]
        touches = ((abs(highs - h) / h) < MICRO_ZONE_FILTER).sum()
        if touches >= 4:
            zones.append({
                "type": "HIGH",
                "center": h,
                "touches": touches,
                "min": h * (1 - MICRO_ZONE_FILTER),
                "max": h * (1 + MICRO_ZONE_FILTER)
            })
        l = lows[i]
        touches = ((abs(lows - l) / l) < MICRO_ZONE_FILTER).sum()
        if touches >= 4:
            zones.append({
                "type": "LOW",
                "center": l,
                "touches": touches,
                "min": l * (1 - MICRO_ZONE_FILTER),
                "max": l * (1 + MICRO_ZONE_FILTER)
            })
    
    # Ordenar por toques
    zones = sorted(zones, key=lambda z: z["touches"], reverse=True)
    
    # Eliminar duplicados cercanos
    zonas_unicas = []
    for z in zones:
        if not any(abs(z["center"] - zf["center"]) / zf["center"] < MICRO_ZONE_FILTER * 2 
                   for zf in zonas_unicas):
            zonas_unicas.append(z)
    
    return zonas_unicas[:5]  # Top 5

# =========================
# SCORE
# =========================

def liquidity_score(z):
    score = 0
    if z["touches"] >= 4:
        score += 1
    if z["touches"] >= 6:
        score += 1
    if z["touches"] >= 8:
        score += 1
    return score

# =========================
# EVALUAR ZONA
# =========================

def evaluate(df):
    global zona_activa, ultimas_zonas, ultimo_calculo_zonas
    
    close = df.iloc[-1]["close"]
    high = df.iloc[-1]["high"]
    low = df.iloc[-1]["low"]
    
    # Recalcular zonas cada cierto tiempo
    ahora = time.time()
    if ahora - ultimo_calculo_zonas > CACHE_ZONAS_SEGUNDOS or zona_activa is None:
        ultimas_zonas = detect_zones(df)
        ultimo_calculo_zonas = ahora
        
        # Seleccionar nueva zona si no hay activa
        if zona_activa is None and ultimas_zonas:
            for z in ultimas_zonas:
                if liquidity_score(z) >= 1:
                    zona_activa = z
                    break

    if zona_activa is None:
        return

    z = zona_activa
    level = round(z["center"], 2)

    # RADAR 1
    if level not in radar1_enviado:
        radar1_enviado.add(level)
        enviar_telegram(
f"""💰 RADAR 1

Liquidez detectada

Zona: {level}
Toques: {z['touches']}
"""
        )

    dist = abs(close - z["center"]) / z["center"]

    # RADAR 2 (más sensible: 0.25%)
    if dist < 0.0025 and level not in radar2_enviado:
        radar2_enviado.add(level)
        enviar_telegram(
f"""🔎 RADAR 2

Precio acercándose a liquidez

Zona: {level}
Distancia: {round(dist*100, 3)}%
"""
        )

    # RADAR 3 (sweep mejorado)
    rango_vela = high - low
    cuerpo = abs(df.iloc[-1]["close"] - df.iloc[-1]["open"])
    cuerpo_pct = cuerpo / rango_vela if rango_vela > 0 else 1

    if z["type"] == "HIGH":
        if high > z["max"] and cuerpo_pct < 0.6 and level not in radar3_enviado:
            radar3_enviado.add(level)
            enviar_telegram(
f"""🔄 RADAR 3

Sweep de liquidez arriba

Zona: {level}
Posible reversión
"""
            )
    elif z["type"] == "LOW":
        if low < z["min"] and cuerpo_pct < 0.6 and level not in radar3_enviado:
            radar3_enviado.add(level)
            enviar_telegram(
f"""🔄 RADAR 3

Sweep de liquidez abajo

Zona: {level}
Posible reversión
"""
            )

    # RADAR 4 (breakout)
    if z["type"] == "HIGH":
        if close > z["max"] and level not in radar4_enviado:
            radar4_enviado.add(level)
            enviar_telegram(
f"""💥 RADAR 4

Breakout alcista

Zona rota: {level}
"""
            )
            zona_activa = None
    elif z["type"] == "LOW":
        if close < z["min"] and level not in radar4_enviado:
            radar4_enviado.add(level)
            enviar_telegram(
f"""💥 RADAR 4

Breakout bajista

Zona rota: {level}
"""
            )
            zona_activa = None

# =========================
# LOOP PRINCIPAL
# =========================

print("🚀 BOT iniciado - Esperando 10 segundos antes de comenzar...")
time.sleep(10)  # Pequeña pausa inicial

while True:
    try:
        df = get_data()
        if df is None:
            print("No hay datos, esperando 60s...")
            time.sleep(60)
            continue
            
        impulso = radar0_impulso(df)
        if impulso:
            candle_time = df.iloc[-1]["timestamp"]
            if candle_time not in radar0_enviado:
                radar0_enviado.add(candle_time)
                delta = df.iloc[-1]["close"] - df.iloc[-1]["open"]
                enviar_telegram(
f"""🚨 RADAR 0

Mercado despertó

Impulso: {impulso}
Δ precio: {round(delta, 2)} USD
Volumen alto detectado
"""
                )
        
        evaluate(df)
        heartbeat()
        
    except Exception as e:
        print(f"error en loop: {e}")
        enviar_telegram(f"⚠️ Error en bot: {str(e)[:100]}")
    
    time.sleep(60)
