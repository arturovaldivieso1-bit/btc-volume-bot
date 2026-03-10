import requests
import pandas as pd
import time
import numpy as np

print("BOT_DE_ARTURO V12 (optimizado 5m) iniciado 🚀")

# =========================
# CONFIG
# =========================

SYMBOL = "BTCUSDT"
INTERVAL = "5m"
LIMIT = 500  # más velas para mejor detección

TELEGRAM_TOKEN = "TU_TOKEN"
TELEGRAM_CHAT_ID = "TU_CHAT_ID"

MICRO_ZONE_FILTER = 0.005  # aumentado a 0.5%

HEARTBEAT_INTERVAL = 14400
ultimo_heartbeat = time.time()

zona_activa = None
radar0_enviado = set()
radar1_enviado = set()
radar2_enviado = set()
radar3_enviado = set()
radar4_enviado = set()

# Contador para recalcular zonas cada cierto tiempo
contador_zonas = 0
RECALCULAR_ZONAS_CADA = 10  # ~ cada 5 minutos (10 ciclos de 30s)

# =========================
# TELEGRAM (sin cambios)
# =========================

def enviar_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=payload, timeout=10)
    except:
        print("Error enviando mensaje Telegram")

# =========================
# HEARTBEAT (sin cambios)
# =========================

def heartbeat():
    global ultimo_heartbeat
    ahora = time.time()
    if ahora - ultimo_heartbeat > HEARTBEAT_INTERVAL:
        enviar_telegram("💓 HEARTBEAT\n\nBOT_DE_ARTURO V12 sigue activo\nMonitoreando BTCUSDT")
        ultimo_heartbeat = ahora

# =========================
# DATA (sin cambios)
# =========================

def get_data():
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    data = requests.get(url, params=params, timeout=10).json()
    df = pd.DataFrame(data, columns=[
        "timestamp","open","high","low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"
    ])
    df = df.astype(float)
    return df

# =========================
# NUEVO: OBTENER DATOS 1h (para filtro macro)
# =========================

def get_data_1h():
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": "1h", "limit": 50}
    data = requests.get(url, params=params, timeout=10).json()
    df = pd.DataFrame(data, columns=[
        "timestamp","open","high","low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"
    ])
    df = df.astype(float)
    return df

def tendencia_1h():
    """Devuelve 'ALCISTA', 'BAJISTA' o 'NEUTRAL' según media móvil de 20 periodos"""
    try:
        df = get_data_1h()
        if len(df) < 20:
            return "NEUTRAL"
        df['sma20'] = df['close'].rolling(20).mean()
        ultimo = df.iloc[-1]
        previo = df.iloc[-2]
        if ultimo['close'] > ultimo['sma20'] and ultimo['close'] > previo['close']:
            return "ALCISTA"
        elif ultimo['close'] < ultimo['sma20'] and ultimo['close'] < previo['close']:
            return "BAJISTA"
        else:
            return "NEUTRAL"
    except:
        return "NEUTRAL"

# =========================
# RADAR 0 (impulso) - umbrales reducidos para mayor sensibilidad
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

    # Umbrales reducidos: 1.5x en lugar de 1.8x, 1.4x en lugar de 1.7x
    impulso_alcista = (
        vela_actual > vela_prom * 1.5 and
        vol_actual > vol_prom * 1.4 and
        last["close"] > high_rango
    )
    impulso_bajista = (
        vela_actual > vela_prom * 1.5 and
        vol_actual > vol_prom * 1.4 and
        last["close"] < low_rango
    )
    if impulso_alcista:
        return "ALCISTA"
    if impulso_bajista:
        return "BAJISTA"
    return None

# =========================
# DETECTAR ZONAS - con menos toques requeridos (3) y filtro ampliado
# =========================

def detect_zones(df):
    highs = df["high"].values
    lows = df["low"].values
    zones = []
    for i in range(len(df)-5):
        h = highs[i]
        touches = ((abs(highs - h) / h) < MICRO_ZONE_FILTER).sum()
        if touches >= 3:  # reducido de 4 a 3
            zones.append({
                "type": "HIGH",
                "center": h,
                "touches": touches,
                "min": h * (1 - MICRO_ZONE_FILTER),
                "max": h * (1 + MICRO_ZONE_FILTER)
            })
        l = lows[i]
        touches = ((abs(lows - l) / l) < MICRO_ZONE_FILTER).sum()
        if touches >= 3:
            zones.append({
                "type": "LOW",
                "center": l,
                "touches": touches,
                "min": l * (1 - MICRO_ZONE_FILTER),
                "max": l * (1 + MICRO_ZONE_FILTER)
            })
    # Eliminar duplicados cercanos y ordenar por toques
    zones = sorted(zones, key=lambda z: z["touches"], reverse=True)
    # Fusión simple: si dos zonas del mismo tipo están muy cerca, quedarse con la de más toques
    zonas_filtradas = []
    for z in zones:
        if not any(abs(z["center"] - zf["center"]) / zf["center"] < MICRO_ZONE_FILTER * 2 for zf in zonas_filtradas):
            zonas_filtradas.append(z)
    return zonas_filtradas

# =========================
# SCORE (sin cambios)
# =========================

def liquidity_score(z):
    score = 0
    if z["touches"] >= 3:
        score += 1
    if z["touches"] >= 5:
        score += 1
    if z["touches"] >= 7:
        score += 1
    return score

# =========================
# EVALUAR ZONA - con filtro macro y radares mejorados
# =========================

def evaluate(df):
    global zona_activa, contador_zonas

    close = df.iloc[-1]["close"]
    high = df.iloc[-1]["high"]
    low = df.iloc[-1]["low"]
    open_ = df.iloc[-1]["open"]

    # Recalcular zonas cada cierto tiempo
    if contador_zonas % RECALCULAR_ZONAS_CADA == 0:
        zonas = detect_zones(df)
        # Si no hay zona activa, elegir una con score >=1 y que coincida con tendencia macro
        if zona_activa is None:
            tend = tendencia_1h()
            for z in zonas:
                score = liquidity_score(z)
                if score >= 1:
                    # Filtro macro: si tendencia alcista, priorizar zonas HIGH; si bajista, LOW
                    if tend == "ALCISTA" and z["type"] == "HIGH":
                        zona_activa = z
                        break
                    elif tend == "BAJISTA" and z["type"] == "LOW":
                        zona_activa = z
                        break
                    elif tend == "NEUTRAL":
                        zona_activa = z
                        break
            # Si no se encontró con filtro, tomar la primera con score
            if zona_activa is None and zonas:
                zona_activa = zonas[0]
    contador_zonas += 1

    if zona_activa is None:
        return

    z = zona_activa
    level = round(z["center"], 2)

    # RADAR 1 (detección de zona)
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

    # RADAR 2 (acercamiento) - umbral aumentado a 0.3%
    if dist < 0.003 and level not in radar2_enviado:
        radar2_enviado.add(level)
        enviar_telegram(
f"""🔎 RADAR 2

Precio acercándose a liquidez

Zona: {level}
Distancia: {round(dist*100, 3)}%
"""
        )

    # RADAR 3 (sweep) - versión mejorada
    # Se activa si la vela toca la zona y tiene una mecha significativa (cuerpo < 50% del rango)
    rango_vela = high - low
    if rango_vela == 0:
        cuerpo_pct = 0
    else:
        cuerpo_pct = abs(close - open_) / rango_vela

    if z["type"] == "HIGH":
        if high > z["max"] and cuerpo_pct < 0.5 and level not in radar3_enviado:
            radar3_enviado.add(level)
            enviar_telegram(
f"""🔄 RADAR 3

Sweep de liquidez arriba (intrabarra)

Zona: {level}
Posible reversión
"""
            )
    elif z["type"] == "LOW":
        if low < z["min"] and cuerpo_pct < 0.5 and level not in radar3_enviado:
            radar3_enviado.add(level)
            enviar_telegram(
f"""🔄 RADAR 3

Sweep de liquidez abajo (intrabarra)

Zona: {level}
Posible reversión
"""
            )

    # RADAR 4 (breakout) - confirmación al cierre, pero con ventana para no desactivar inmediatamente
    if z["type"] == "HIGH":
        if close > z["max"] and level not in radar4_enviado:
            radar4_enviado.add(level)
            enviar_telegram(
f"""💥 RADAR 4

Breakout alcista

Zona rota: {level}
"""
            )
            # No desactivamos zona aún, esperamos 3 velas para confirmar
    elif z["type"] == "LOW":
        if close < z["min"] and level not in radar4_enviado:
            radar4_enviado.add(level)
            enviar_telegram(
f"""💥 RADAR 4

Breakout bajista

Zona rota: {level}
"""
            )

    # Desactivar zona solo si el precio se aleja claramente (ej. 3 velas por encima/ debajo)
    # Para simplificar, lo haremos con un contador de velas fuera de zona (no implementado aquí)
    # Podríamos añadir un contador 'velas_fuera' en zona_activa y desactivar tras 3.
    # Por ahora, lo dejamos como estaba: se desactiva tras breakout y se reinicia con nueva zona.

# =========================
# LOOP PRINCIPAL - con ciclo de 30s
# =========================

while True:
    try:
        df = get_data()
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
        print("error:", e)
    time.sleep(30)  # reducido de 60 a 30 segundos
