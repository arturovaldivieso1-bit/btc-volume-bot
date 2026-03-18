# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os
import numpy as np
from datetime import datetime, timedelta, UTC
from collections import deque
import json

# =========================
# CONFIGURACIÓN INICIAL
# =========================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOL = "BTCUSDT"

# Timeframes
INTERVAL_MACRO = "1h"          # Para estructura de liquidez (spot y bias)
INTERVAL_ENTRY = "5m"           # Para eventos
INTERVAL_BIAS = "4h"            # Para tendencia de más largo plazo

# Parámetros de liquidez spot
LOOKBACK = 168
MIN_TOUCHES = 3
CLUSTER_RANGE = 0.0025          # 0.25% para agrupar precios
PROXIMITY = 0.003               # 0.3% umbral inferior para Radar 2
RADAR1_MIN_DIST = 0.01          # 1% umbral mínimo para enviar Radar 1 (solo spot, futuros ya no lo usan)
ZONA_EQUIVALENTE = 0.01         # 1% para considerar misma zona (evita spam)

# Parámetros de Open Interest (futuros) - dinámicos
OI_WINDOW = 50
OI_PERCENTILE = 75
OI_LOOKBACK = 3
OI_CLUSTER_RANGE = 0.002
OI_CONFIANZA_ALTA = 200_000_000
OI_CONFIANZA_MEDIA = 100_000_000

# Tolerancia para validación cruzada spot-futuros
SPOT_FUTUROS_TOLERANCIA = 0.005   # 0.5%

# Radar 0 - Impulso
IMPULSE_PRICE_CHANGE = 0.65
IMPULSE_COOLDOWN = 300

# Radar 3 - Sweep
SWEEP_VOLUME_FACTOR = 1.5
SWEEP_BREAK_MARGIN = 0.001        # 0.1%

# Radar 4 - Breakout
BREAKOUT_MARGIN = 0.003           # 0.3% (se mantiene)
BREAKOUT_RETEST_CANDLES = 1        # Número de velas para confirmar retest (0 = sin retest)

# Sistema de bias y scoring
EMA_SHORT = 50
EMA_LONG = 200
SCORE_UMBRAL_ACCION = 5            # Mínimo score para enviar alerta
PESOS = {
    "zona_spot": 1,
    "zona_oi": 2,
    "toques_altos": 1,             # si toques >= 5
    "cercania": 1,                  # si distancia < 0.5%
    "bias_favorable": 3,
    "evento_impulso": 1,
    "evento_sweep": 2,
    "evento_breakout": 2,
    "validacion_spot": 2
}

# Umbrales de volumen para probabilidades (estudio) - serán recalibrados
VOL1_MED = 400
VOL1_P75 = 692
VOL1_P90 = 1134
PROB1_MED = 68
PROB1_P75 = 82
PROB1_P90 = 85

VOL2_MED = 712
VOL2_P75 = 1189
VOL2_P90 = 1876
PROB2_MED = 72
PROB2_P75 = 84
PROB2_P90 = 87

VOL3_MED = 1023
VOL3_P75 = 1645
VOL3_P90 = 2534
PROB3_MED = 75
PROB3_P75 = 86
PROB3_P90 = 89

# Sistema
HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6
MAPA_COOLDOWN_MINUTOS = 60
RADAR2_COOLDOWN_MINUTOS = 120
REDONDEO_BASE = 200

# Archivo para persistencia (opcional)
HISTORIAL_FILE = "historial_eventos.json"

# Variables de estado
last_impulse_time = None
last_heartbeat_time = None
last_event_time = None
last_mapa_time = None
zona_actual = None
zona_consumida = False
alerted_liquidity = set()
alerted_proximidad = {}
sweep_pendiente = None
ultima_zona_arriba = None
ultima_zona_abajo = None

# Historial de incrementos de OI
oi_increment_history = []

# Historial de eventos para backtesting dinámico (en memoria)
historial_eventos = deque(maxlen=1000)  # guarda últimos 1000 eventos

# =========================
# FUNCIONES AUXILIARES
# =========================

def enviar(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Error Telegram: {e}")

def fmt(n):
    return f"{int(n):,}"

def obtener_candles_spot(interval, limit=200):
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
        print(f"Error Binance spot: {e}")
        return pd.DataFrame()

def obtener_precio_actual():
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
        r = requests.get(url, timeout=10)
        return float(r.json()["price"])
    except:
        return None

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
            clusters.append({"centro": p, "valores": [p]})
    return clusters

def detectar_zonas_spot(df):
    if df.empty or len(df) < LOOKBACK:
        return [], []
    highs = df["high"].tail(LOOKBACK).tolist()
    lows = df["low"].tail(LOOKBACK).tolist()
    clusters_high = cluster_precios(highs)
    clusters_low = cluster_precios(lows)
    zonas_high = []
    for c in clusters_high:
        if len(c["valores"]) >= MIN_TOUCHES:
            zonas_high.append({
                "tipo": "HIGH",
                "centro": c["centro"],
                "max": max(c["valores"]),
                "min": min(c["valores"]),
                "toques": len(c["valores"])
            })
    zonas_low = []
    for c in clusters_low:
        if len(c["valores"]) >= MIN_TOUCHES:
            zonas_low.append({
                "tipo": "LOW",
                "centro": c["centro"],
                "max": max(c["valores"]),
                "min": min(c["valores"]),
                "toques": len(c["valores"])
            })
    zonas_high.sort(key=lambda x: x["toques"], reverse=True)
    zonas_low.sort(key=lambda x: x["toques"], reverse=True)
    return zonas_high, zonas_low

def redondear_centro(centro, base=REDONDEO_BASE):
    return round(centro / base) * base

def seleccionar_mejores_zonas_spot(zonas_high, zonas_low, precio):
    arriba = [z for z in zonas_high if z["centro"] > precio] + [z for z in zonas_low if z["centro"] > precio]
    abajo = [z for z in zonas_low if z["centro"] < precio] + [z for z in zonas_high if z["centro"] < precio]

    for z in arriba:
        z["distancia"] = z["centro"] - precio
        z["score"] = z["toques"] * 1000 - z["distancia"]
        z["centro_rd"] = redondear_centro(z["centro"])
    for z in abajo:
        z["distancia"] = precio - z["centro"]
        z["score"] = z["toques"] * 1000 - z["distancia"]
        z["centro_rd"] = redondear_centro(z["centro"])

    arriba.sort(key=lambda x: x["score"], reverse=True)
    abajo.sort(key=lambda x: x["score"], reverse=True)

    mejor_arriba = arriba[0] if arriba else None
    mejor_abajo = abajo[0] if abajo else None
    return mejor_arriba, mejor_abajo

def distancia(zona, precio):
    return abs(zona["centro"] - precio) / precio

def misma_zona(z1, z2):
    if z1 is None or z2 is None:
        return False
    c1_arriba, c1_abajo = z1
    c2_arriba, c2_abajo = z2
    if c1_arriba is None and c2_arriba is None:
        diff_arriba = 0
    elif c1_arriba is None or c2_arriba is None:
        diff_arriba = 1
    else:
        diff_arriba = abs(c1_arriba - c2_arriba) / c1_arriba

    if c1_abajo is None and c2_abajo is None:
        diff_abajo = 0
    elif c1_abajo is None or c2_abajo is None:
        diff_abajo = 1
    else:
        diff_abajo = abs(c1_abajo - c2_abajo) / c1_abajo

    return diff_arriba < ZONA_EQUIVALENTE and diff_abajo < ZONA_EQUIVALENTE

def calcular_bias(df_1h, df_4h, precio_actual):
    """
    Calcula el bias direccional basado en EMAs y pendiente.
    Retorna: "ALCISTA", "BAJISTA" o "LATERAL"
    """
    if df_1h.empty or df_4h.empty:
        return "LATERAL"

    # EMAs
    ema_short_1h = df_1h["close"].ewm(span=EMA_SHORT, adjust=False).mean().iloc[-1]
    ema_long_1h = df_1h["close"].ewm(span=EMA_LONG, adjust=False).mean().iloc[-1]
    ema_short_4h = df_4h["close"].ewm(span=EMA_SHORT, adjust=False).mean().iloc[-1]
    ema_long_4h = df_4h["close"].ewm(span=EMA_LONG, adjust=False).mean().iloc[-1]

    # Pendiente simple (comparar últimos 2 valores)
    pendiente_1h = df_1h["close"].iloc[-1] - df_1h["close"].iloc[-2]
    pendiente_4h = df_4h["close"].iloc[-1] - df_4h["close"].iloc[-2]

    # Reglas
    alcista = (precio_actual > ema_short_1h and ema_short_1h > ema_long_1h and pendiente_1h > 0) or \
              (precio_actual > ema_short_4h and ema_short_4h > ema_long_4h and pendiente_4h > 0)
    bajista = (precio_actual < ema_short_1h and ema_short_1h < ema_long_1h and pendiente_1h < 0) or \
              (precio_actual < ema_short_4h and ema_short_4h < ema_long_4h and pendiente_4h < 0)

    if alcista and not bajista:
        return "ALCISTA"
    elif bajista and not alcista:
        return "BAJISTA"
    else:
        return "LATERAL"

# =========================
# FUNCIONES PARA OPEN INTEREST (FUTUROS)
# =========================

def obtener_open_interest_hist(period="5m", limit=200):
    url = "https://fapi.binance.com/futures/data/openInterestHist"
    params = {
        "symbol": SYMBOL,
        "period": period,
        "limit": limit
    }
    try:
        data = requests.get(url, params=params, timeout=10).json()
        df = pd.DataFrame(data)
        df["sumOpenInterest"] = pd.to_numeric(df["sumOpenInterest"])
        df["sumOpenInterestValue"] = pd.to_numeric(df["sumOpenInterestValue"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit='ms')
        return df
    except Exception as e:
        print(f"Error obteniendo Open Interest: {e}")
        return pd.DataFrame()

def cluster_oi_por_precio(eventos_oi):
    clusters = []
    for ev in sorted(eventos_oi, key=lambda x: x["precio"]):
        agregado = False
        for c in clusters:
            if abs(ev["precio"] - c["centro"]) / ev["precio"] < OI_CLUSTER_RANGE:
                c["valores"].append(ev["precio"])
                c["centro"] = sum(c["valores"]) / len(c["valores"])
                c["oi_total"] += ev["oi_incremento"]
                c["min"] = min(c["min"], ev["precio"])
                c["max"] = max(c["max"], ev["precio"])
                agregado = True
                break
        if not agregado:
            clusters.append({
                "centro": ev["precio"],
                "valores": [ev["precio"]],
                "oi_total": ev["oi_incremento"],
                "min": ev["precio"],
                "max": ev["precio"]
            })
    for c in clusters:
        if c["min"] == c["max"]:
            c["min"] = c["centro"] * (1 - OI_CLUSTER_RANGE)
            c["max"] = c["centro"] * (1 + OI_CLUSTER_RANGE)
    clusters.sort(key=lambda x: x["oi_total"], reverse=True)
    return clusters

def detectar_zonas_oi(df_oi, df_spot):
    global oi_increment_history

    if df_oi.empty or len(df_oi) < OI_LOOKBACK + 1:
        return []

    nuevos_incrementos = []
    for i in range(1, len(df_oi)):
        oi_actual = float(df_oi.iloc[i]["sumOpenInterestValue"])
        oi_anterior = float(df_oi.iloc[i-1]["sumOpenInterestValue"])
        inc = max(0, oi_actual - oi_anterior)
        nuevos_incrementos.append(inc)

    oi_increment_history.extend(nuevos_incrementos)
    if len(oi_increment_history) > OI_WINDOW:
        oi_increment_history = oi_increment_history[-OI_WINDOW:]

    umbral_dinamico = np.percentile(oi_increment_history, OI_PERCENTILE) if len(oi_increment_history) >= 10 else (20_000_000)

    eventos_oi = []
    for i in range(OI_LOOKBACK, len(df_oi)):
        suma_incrementos = 0
        for j in range(i - OI_LOOKBACK + 1, i + 1):
            oi_actual = float(df_oi.iloc[j]["sumOpenInterestValue"])
            oi_anterior = float(df_oi.iloc[j-1]["sumOpenInterestValue"])
            suma_incrementos += max(0, oi_actual - oi_anterior)
        if suma_incrementos > umbral_dinamico:
            ts = df_oi.iloc[i]["timestamp"]
            precio_zona = None
            if not df_spot.empty and 'time' in df_spot.columns:
                df_spot['time_dt'] = pd.to_datetime(df_spot['time'], unit='ms')
                idx = (df_spot['time_dt'] - ts).abs().idxmin()
                precio_zona = df_spot.loc[idx, 'close']
            if precio_zona:
                eventos_oi.append({
                    "precio": precio_zona,
                    "oi_incremento": suma_incrementos,
                    "timestamp": ts
                })

    if not eventos_oi:
        return []

    clusters = cluster_oi_por_precio(eventos_oi)

    for c in clusters:
        if c["oi_total"] > OI_CONFIANZA_ALTA:
            c["confianza"] = "🔥🔥🔥"
        elif c["oi_total"] > OI_CONFIANZA_MEDIA:
            c["confianza"] = "🔥🔥"
        else:
            c["confianza"] = "🔥"

    return clusters

# =========================
# FUNCIONES DE SCORE Y PESOS
# =========================

def calcular_peso_zona(zona_spot, zona_oi, precio_actual):
    peso = 0
    if zona_spot:
        peso += PESOS["zona_spot"]
        if zona_spot["toques"] >= 5:
            peso += PESOS["toques_altos"]
        if distancia(zona_spot, precio_actual) < 0.005:  # 0.5%
            peso += PESOS["cercania"]
    if zona_oi:
        peso += PESOS["zona_oi"]
        if distancia(zona_oi, precio_actual) < 0.005:
            peso += PESOS["cercania"]
    return peso

def calcular_score_evento(evento_tipo, direccion_evento, bias, peso_zona, validacion_spot=False):
    score = 0
    # Peso base de la zona
    score += peso_zona
    # Evento específico
    score += PESOS.get(f"evento_{evento_tipo}", 0)
    # Validación spot-futuros
    if validacion_spot:
        score += PESOS["validacion_spot"]
    # Bias favorable
    if bias == direccion_evento:
        score += PESOS["bias_favorable"]
    return score

# =========================
# RADAR 1 (CON PESOS Y SCORE)
# =========================

def enviar_liquidez_detectada(mejor_zona_oi_arriba, mejor_zona_oi_abajo, mejor_zona_spot_arriba, mejor_zona_spot_abajo, precio, hora, bias):
    global ultima_zona_arriba, ultima_zona_abajo

    def enviar_zona(zona, tipo, es_oi, zonas_spot_referencia=None):
        # Calcular peso y validación
        peso = calcular_peso_zona(zona if not es_oi else None, zona if es_oi else None, precio)
        validacion = ""
        if es_oi and zonas_spot_referencia:
            for zs in zonas_spot_referencia:
                if zs and abs(zona['centro'] - zs['centro']) / zona['centro'] < SPOT_FUTUROS_TOLERANCIA:
                    validacion = "\n✅ Confirmado por spot"
                    peso += PESOS["validacion_spot"]  # ya se suma después
                    break

        # Determinar si se envía según umbral de score (si es futuros siempre se envía? decidimos que sí, pero podemos filtrar)
        # Por ahora, filtramos por score solo para eventos, no para zonas. Las zonas son el mapa, siempre se muestran si cambian.

        if es_oi:
            icono = "🧲"
            fuente = "FUTUROS"
            confianza = zona.get('confianza', '')
            oi_valor = zona['oi_total'] / 1_000_000
            linea_extra = f"OI acumulado: +{oi_valor:.1f}M USD {confianza}"
        else:
            icono = "💰"
            fuente = "SPOT"
            linea_extra = f"{zona['toques']} toques{' 🔥' if zona['toques'] >= 5 else ''}"

        direccion_texto = "ARRIBA" if tipo == "HIGH" else "ABAJO"
        color_direccion = "🟢" if tipo == "HIGH" else "🔴"

        centro = fmt(zona['centro'])
        rango = f"{fmt(zona['min'])}-{fmt(zona['max'])}"
        dist = distancia(zona, precio) * 100
        msg = f"{icono} RADAR 1 – LIQUIDEZ {fuente} {color_direccion} {direccion_texto}\n\n"
        msg += f"Centro: {centro} ({rango})\n"
        msg += f"Distancia: {dist:.1f}%\n"
        msg += f"{linea_extra}"
        if validacion:
            msg += validacion
        msg += f"\n\nPrecio actual: {fmt(precio)} | Hora: {hora}"
        msg += f"\nBias: {bias}"
        enviar(msg)

    # Enviar zonas si existen (siempre, sin filtro de score)
    if mejor_zona_oi_arriba:
        enviar_zona(mejor_zona_oi_arriba, "HIGH", True, [mejor_zona_spot_arriba] if mejor_zona_spot_arriba else None)
        ultima_zona_arriba = mejor_zona_oi_arriba["centro"]
    elif mejor_zona_spot_arriba and distancia(mejor_zona_spot_arriba, precio) >= RADAR1_MIN_DIST:
        enviar_zona(mejor_zona_spot_arriba, "HIGH", False)
        ultima_zona_arriba = mejor_zona_spot_arriba["centro"]
    else:
        ultima_zona_arriba = None

    if mejor_zona_oi_abajo:
        enviar_zona(mejor_zona_oi_abajo, "LOW", True, [mejor_zona_spot_abajo] if mejor_zona_spot_abajo else None)
        ultima_zona_abajo = mejor_zona_oi_abajo["centro"]
    elif mejor_zona_spot_abajo and distancia(mejor_zona_spot_abajo, precio) >= RADAR1_MIN_DIST:
        enviar_zona(mejor_zona_spot_abajo, "LOW", False)
        ultima_zona_abajo = mejor_zona_spot_abajo["centro"]
    else:
        ultima_zona_abajo = None

# =========================
# RADAR 2 (PROXIMIDAD)
# =========================

def radar_proximidad(mejor_zona_arriba, mejor_zona_abajo, precio, hora, bias):
    global last_event_time

    # Solo considerar zonas que coincidan con la última de Radar 1
    if ultima_zona_arriba is not None:
        if mejor_zona_arriba and abs(mejor_zona_arriba["centro"] - ultima_zona_arriba) / ultima_zona_arriba > 0.001:
            mejor_zona_arriba = None
    if ultima_zona_abajo is not None:
        if mejor_zona_abajo and abs(mejor_zona_abajo["centro"] - ultima_zona_abajo) / ultima_zona_abajo > 0.001:
            mejor_zona_abajo = None

    ahora = datetime.now(UTC)

    def check_and_send(zona, color_emoji, tipo):
        if zona is None:
            return False
        centro_rd = redondear_centro(zona["centro"])
        key = (centro_rd, tipo)
        dist = distancia(zona, precio)
        if PROXIMITY <= dist < RADAR1_MIN_DIST:
            ultimo = alerted_proximidad.get(key)
            if ultimo is None or (ahora - ultimo) > timedelta(minutes=RADAR2_COOLDOWN_MINUTOS):
                alerted_proximidad[key] = ahora
                titulo = f"🔍 Radar 2 – CERCA {fmt(centro_rd)} ({dist*100:.1f}%) {color_emoji}"
                msg = f"{titulo}\n\nPrecio: {fmt(precio)} | Hora: {hora}\nBias: {bias}"
                enviar(msg)
                last_event_time = ahora
                return True
        return False

    if mejor_zona_arriba:
        check_and_send(mejor_zona_arriba, "🟢", "HIGH")
    if mejor_zona_abajo:
        check_and_send(mejor_zona_abajo, "🔴", "LOW")

# =========================
# RADAR 0 (IMPULSO) CONDICIONADO
# =========================

def radar_impulse(df_entry, precio_actual, zonas_arriba, zonas_abajo, bias):
    global last_impulse_time, last_event_time, historial_eventos
    if df_entry.empty or len(df_entry) < 3:
        return

    vela = df_entry.iloc[-1]
    try:
        open_price = float(vela["open"])
        close_price = float(vela["close"])
        volume = float(vela["volume"])
    except:
        return

    price_change = abs(close_price - open_price) / open_price * 100

    if price_change < IMPULSE_PRICE_CHANGE:
        return

    ahora = datetime.now(UTC)
    if last_impulse_time and (ahora - last_impulse_time).seconds < IMPULSE_COOLDOWN:
        return

    alcista = close_price > open_price
    direccion = "ALCISTA" if alcista else "BAJISTA"
    emoji = "🟢" if alcista else "🔴"

    # Determinar si el impulso apunta hacia alguna zona relevante
    zona_relevante = None
    if alcista and zonas_arriba:
        zona_relevante = zonas_arriba[0]  # la más cercana arriba
    elif not alcista and zonas_abajo:
        zona_relevante = zonas_abajo[0]   # la más cercana abajo

    # Si no apunta a zona relevante, no enviamos (opcional, podemos decidir enviar igual con bajo score)
    if not zona_relevante:
        return

    # Calcular peso de la zona (simplificado, usando solo la zona relevante)
    peso_zona = calcular_peso_zona(zona_relevante if not zona_relevante.get('oi_total') else None,
                                    zona_relevante if zona_relevante.get('oi_total') else None, precio_actual)
    # Validación spot (si la zona es de futuros, buscar spot cercano)
    validacion_spot = False
    if 'oi_total' in zona_relevante:
        # Buscar en zonas_arriba/abajo si hay spot cerca
        for z in (zonas_arriba + zonas_abajo):
            if 'toques' in z and abs(zona_relevante['centro'] - z['centro']) / zona_relevante['centro'] < SPOT_FUTUROS_TOLERANCIA:
                validacion_spot = True
                break

    score = calcular_score_evento("impulso", direccion, bias, peso_zona, validacion_spot)

    if score < SCORE_UMBRAL_ACCION:
        return

    # Guardar evento en historial
    historial_eventos.append({
        "timestamp": ahora.isoformat(),
        "tipo": "impulso",
        "direccion": direccion,
        "precio": precio_actual,
        "score": score,
        "volumen": volume
    })

    # Enviar alerta
    titulo = f"🚀 Radar 0 - Impulso {direccion} {emoji} (score {score})"
    msg = f"{titulo}\n\n"
    msg += f"Precio: {fmt(precio_actual)} - Hora UTC: {ahora.strftime('%H:%M')}\n"
    msg += f"Variación: {price_change:.2f}%\n"
    msg += f"Volumen: {volume:.2f} BTC\n"
    msg += f"Bias: {bias}\n"
    msg += f"Zona objetivo: {fmt(zona_relevante['centro'])} ({distancia(zona_relevante, precio_actual)*100:.1f}%)"
    enviar(msg)
    last_impulse_time = ahora
    last_event_time = ahora

# =========================
# RADAR 3 (SWEEP) CONDICIONADO
# =========================

def radar_sweep(df_entry, mejor_zona_arriba, mejor_zona_abajo, precio_actual, zonas_arriba, zonas_abajo, bias):
    global sweep_pendiente, last_event_time, historial_eventos
    if df_entry.empty or len(df_entry) < 2:
        return
    vela_actual = df_entry.iloc[-1]
    if sweep_pendiente:
        zona, tipo_sweep = sweep_pendiente
        if tipo_sweep == "HIGH" and vela_actual["close"] < vela_actual["open"]:
            direccion_rev = "BAJISTA"
            emoji_rev = "🔴"
        elif tipo_sweep == "LOW" and vela_actual["close"] > vela_actual["open"]:
            direccion_rev = "ALCISTA"
            emoji_rev = "🟢"
        else:
            sweep_pendiente = None
            return

        # Calcular peso y score
        peso_zona = calcular_peso_zona(zona if 'toques' in zona else None,
                                        zona if 'oi_total' in zona else None, precio_actual)
        validacion_spot = False
        if 'oi_total' in zona:
            for z in (zonas_arriba + zonas_abajo):
                if 'toques' in z and abs(zona['centro'] - z['centro']) / zona['centro'] < SPOT_FUTUROS_TOLERANCIA:
                    validacion_spot = True
                    break
        direccion_evento = "BAJISTA" if tipo_sweep == "HIGH" else "ALCISTA"  # la reversión es la dirección
        score = calcular_score_evento("sweep", direccion_evento, bias, peso_zona, validacion_spot)

        if score < SCORE_UMBRAL_ACCION:
            sweep_pendiente = None
            return

        ahora = datetime.now(UTC)
        color_zona = "🟢" if tipo_sweep == "HIGH" else "🔴"
        titulo = f"🔄 Radar 3 – SWEEP {tipo_sweep} {color_zona} ({fmt(zona['centro'])}) (score {score})"
        msg = f"{titulo}\n\n"
        msg += f"REVERSIÓN {direccion_rev} {emoji_rev}\n\n"
        msg += f"Precio: {fmt(precio_actual)} | Hora: {ahora.strftime('%H:%M')}\n"
        msg += f"Bias: {bias}"
        enviar(msg)

        # Guardar evento
        historial_eventos.append({
            "timestamp": ahora.isoformat(),
            "tipo": "sweep",
            "direccion": direccion_rev,
            "precio": precio_actual,
            "score": score,
            "zona": zona['centro']
        })

        last_event_time = ahora
        sweep_pendiente = None
        return

    if len(df_entry) < 2:
        return
    vela_anterior = df_entry.iloc[-2]
    vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]

    # Buscar sweep solo en zonas relevantes (con peso suficiente)
    zonas_a_evaluar = []
    if mejor_zona_arriba:
        peso = calcular_peso_zona(mejor_zona_arriba if 'toques' in mejor_zona_arriba else None,
                                   mejor_zona_arriba if 'oi_total' in mejor_zona_arriba else None, precio_actual)
        if peso >= 3:  # umbral arbitrario, podemos ajustar
            zonas_a_evaluar.append(mejor_zona_arriba)
    if mejor_zona_abajo:
        peso = calcular_peso_zona(mejor_zona_abajo if 'toques' in mejor_zona_abajo else None,
                                   mejor_zona_abajo if 'oi_total' in mejor_zona_abajo else None, precio_actual)
        if peso >= 3:
            zonas_a_evaluar.append(mejor_zona_abajo)

    for zona in zonas_a_evaluar:
        if 'max' in zona and vela_anterior["high"] > zona["max"] * (1 + SWEEP_BREAK_MARGIN) and vela_anterior["close"] < zona["centro"]:
            if vela_anterior["volume"] > vol_medio * SWEEP_VOLUME_FACTOR:
                sweep_pendiente = (zona, "HIGH")
                break
        elif 'min' in zona and vela_anterior["low"] < zona["min"] * (1 - SWEEP_BREAK_MARGIN) and vela_anterior["close"] > zona["centro"]:
            if vela_anterior["volume"] > vol_medio * SWEEP_VOLUME_FACTOR:
                sweep_pendiente = (zona, "LOW")
                break

# =========================
# RADAR 4 (BREAKOUT) CON RETEST
# =========================

def radar_breakout(df_entry, mejor_zona_arriba, mejor_zona_abajo, precio_actual, zonas_arriba, zonas_abajo, bias):
    global zona_consumida, last_event_time, alerted_liquidity, historial_eventos
    if df_entry.empty or len(df_entry) < BREAKOUT_RETEST_CANDLES + 1:
        return
    vela = df_entry.iloc[-1]
    close = vela["close"]
    ahora = datetime.now(UTC)

    # Función para verificar retest
    def hubo_retest(zona, direccion):
        if BREAKOUT_RETEST_CANDLES == 0:
            return True
        # Revisar las últimas BREAKOUT_RETEST_CANDLES velas (excluyendo la actual) para ver si hubo un cierre dentro de la zona
        for i in range(2, BREAKOUT_RETEST_CANDLES + 2):
            if i > len(df_entry):
                break
            vela_anterior = df_entry.iloc[-i]
            if direccion == "ALCISTA" and vela_anterior["close"] > zona["min"] and vela_anterior["close"] < zona["max"]:
                return True
            if direccion == "BAJISTA" and vela_anterior["close"] > zona["min"] and vela_anterior["close"] < zona["max"]:
                return True
        return False

    # Evaluar zonas
    if mejor_zona_arriba:
        key = ("break", redondear_centro(mejor_zona_arriba["centro"]))
        if key not in alerted_liquidity and close > mejor_zona_arriba.get("max", mejor_zona_arriba["centro"]*1.01) * (1 + BREAKOUT_MARGIN):
            if hubo_retest(mejor_zona_arriba, "ALCISTA"):
                # Calcular score
                peso_zona = calcular_peso_zona(mejor_zona_arriba if 'toques' in mejor_zona_arriba else None,
                                                mejor_zona_arriba if 'oi_total' in mejor_zona_arriba else None, precio_actual)
                validacion_spot = False
                if 'oi_total' in mejor_zona_arriba:
                    for z in (zonas_arriba + zonas_abajo):
                        if 'toques' in z and abs(mejor_zona_arriba['centro'] - z['centro']) / mejor_zona_arriba['centro'] < SPOT_FUTUROS_TOLERANCIA:
                            validacion_spot = True
                            break
                score = calcular_score_evento("breakout", "ALCISTA", bias, peso_zona, validacion_spot)

                if score >= SCORE_UMBRAL_ACCION:
                    alerted_liquidity.add(key)
                    zona_consumida = True
                    titulo = f"🌋 Radar 4 – BREAKOUT ALCISTA 🟢 ({fmt(mejor_zona_arriba['centro'])}) (score {score})"
                    msg = f"{titulo}\n\n"
                    msg += f"Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}\n"
                    msg += f"Bias: {bias}"
                    enviar(msg)
                    historial_eventos.append({
                        "timestamp": ahora.isoformat(),
                        "tipo": "breakout",
                        "direccion": "ALCISTA",
                        "precio": close,
                        "score": score,
                        "zona": mejor_zona_arriba['centro']
                    })
                    last_event_time = ahora
                    return

    if mejor_zona_abajo:
        key = ("break", redondear_centro(mejor_zona_abajo["centro"]))
        if key not in alerted_liquidity and close < mejor_zona_abajo.get("min", mejor_zona_abajo["centro"]*0.99) * (1 - BREAKOUT_MARGIN):
            if hubo_retest(mejor_zona_abajo, "BAJISTA"):
                peso_zona = calcular_peso_zona(mejor_zona_abajo if 'toques' in mejor_zona_abajo else None,
                                                mejor_zona_abajo if 'oi_total' in mejor_zona_abajo else None, precio_actual)
                validacion_spot = False
                if 'oi_total' in mejor_zona_abajo:
                    for z in (zonas_arriba + zonas_abajo):
                        if 'toques' in z and abs(mejor_zona_abajo['centro'] - z['centro']) / mejor_zona_abajo['centro'] < SPOT_FUTUROS_TOLERANCIA:
                            validacion_spot = True
                            break
                score = calcular_score_evento("breakout", "BAJISTA", bias, peso_zona, validacion_spot)

                if score >= SCORE_UMBRAL_ACCION:
                    alerted_liquidity.add(key)
                    zona_consumida = True
                    titulo = f"🌋 Radar 4 – BREAKOUT BAJISTA 🔴 ({fmt(mejor_zona_abajo['centro'])}) (score {score})"
                    msg = f"{titulo}\n\n"
                    msg += f"Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}\n"
                    msg += f"Bias: {bias}"
                    enviar(msg)
                    historial_eventos.append({
                        "timestamp": ahora.isoformat(),
                        "tipo": "breakout",
                        "direccion": "BAJISTA",
                        "precio": close,
                        "score": score,
                        "zona": mejor_zona_abajo['centro']
                    })
                    last_event_time = ahora
                    return

# =========================
# ALERTAS DE SISTEMA
# =========================

def heartbeat():
    global last_heartbeat_time
    ahora = datetime.now(UTC)
    if last_heartbeat_time is None:
        last_heartbeat_time = ahora
        return
    if (ahora - last_heartbeat_time) > timedelta(hours=HEARTBEAT_HOURS):
        precio = obtener_precio_actual() or 0
        msg = f"🤖 BOT DE ARTURO FUNCIONANDO (V12)\nHora UTC: {ahora.strftime('%H:%M')}\nPrecio: {fmt(precio)}"
        enviar(msg)
        last_heartbeat_time = ahora

def sin_eventos():
    global last_event_time
    ahora = datetime.now(UTC)
    if last_event_time is None:
        last_event_time = ahora
        return
    if (ahora - last_event_time) > timedelta(hours=NO_EVENT_HOURS):
        precio = obtener_precio_actual() or 0
        msg = f"⚠️ MERCADO SIN EVENTOS RELEVANTES\nTiempo sin señales: {NO_EVENT_HOURS}h\nPrecio: {fmt(precio)} | Hora: {ahora.strftime('%H:%M')}\nEstado: lateral / baja volatilidad"
        enviar(msg)
        last_event_time = ahora

# =========================
# FUNCIÓN PRINCIPAL
# =========================

def evaluar():
    global zona_actual, zona_consumida, alerted_liquidity, alerted_proximidad, last_event_time, last_mapa_time

    ahora = datetime.now(UTC)
    hora_str = ahora.strftime('%H:%M')

    # Obtener datos
    df_1h = obtener_candles_spot(INTERVAL_MACRO)
    df_4h = obtener_candles_spot(INTERVAL_BIAS)
    df_entry = obtener_candles_spot(INTERVAL_ENTRY)
    df_oi = obtener_open_interest_hist(period="5m", limit=200)
    precio = obtener_precio_actual()

    if df_entry.empty or precio is None:
        return

    # Calcular bias
    bias = calcular_bias(df_1h, df_4h, precio)

    # Detectar zonas spot
    zonas_high_spot, zonas_low_spot = detectar_zonas_spot(df_1h) if not df_1h.empty else ([], [])
    mejor_spot_arriba, mejor_spot_abajo = seleccionar_mejores_zonas_spot(zonas_high_spot, zonas_low_spot, precio)

    # Detectar zonas OI
    clusters_oi = detectar_zonas_oi(df_oi, df_1h) if not df_oi.empty else []
    mejor_oi_arriba = None
    mejor_oi_abajo = None
    if clusters_oi:
        arriba_oi = [c for c in clusters_oi if c["centro"] > precio]
        abajo_oi = [c for c in clusters_oi if c["centro"] < precio]
        if arriba_oi:
            arriba_oi.sort(key=lambda x: x["oi_total"], reverse=True)
            mejor_oi_arriba = arriba_oi[0]
        if abajo_oi:
            abajo_oi.sort(key=lambda x: x["oi_total"], reverse=True)
            mejor_oi_abajo = abajo_oi[0]

    # Definir zona actual
    centro_arriba = mejor_oi_arriba["centro"] if mejor_oi_arriba else (mejor_spot_arriba["centro"] if mejor_spot_arriba else None)
    centro_abajo = mejor_oi_abajo["centro"] if mejor_oi_abajo else (mejor_spot_abajo["centro"] if mejor_spot_abajo else None)
    nueva_zona = (centro_arriba, centro_abajo)

    # Radar 1 si cambia zona
    if not misma_zona(zona_actual, nueva_zona):
        zona_actual = nueva_zona
        zona_consumida = False
        alerted_liquidity.clear()
        if last_mapa_time is None or (ahora - last_mapa_time) > timedelta(minutes=MAPA_COOLDOWN_MINUTOS):
            enviar_liquidez_detectada(mejor_oi_arriba, mejor_oi_abajo, mejor_spot_arriba, mejor_spot_abajo, precio, hora_str, bias)
            last_mapa_time = ahora
        last_event_time = ahora

    # Limpieza de proximidad
    keys_a_remover = []
    for key, ts in alerted_proximidad.items():
        if (ahora - ts) > timedelta(hours=2):
            keys_a_remover.append(key)
    for key in keys_a_remover:
        alerted_proximidad.pop(key, None)

    # Zonas de referencia para radares (prioridad OI)
    zona_ref_arriba = mejor_oi_arriba if mejor_oi_arriba else mejor_spot_arriba
    zona_ref_abajo = mejor_oi_abajo if mejor_oi_abajo else mejor_spot_abajo

    # Listas completas para búsquedas
    zonas_arriba = [z for z in ([mejor_oi_arriba] if mejor_oi_arriba else []) + ([mejor_spot_arriba] if mejor_spot_arriba else []) if z]
    zonas_abajo = [z for z in ([mejor_oi_abajo] if mejor_oi_abajo else []) + ([mejor_spot_abajo] if mejor_spot_abajo else []) if z]

    # Radar 2
    radar_proximidad(zona_ref_arriba, zona_ref_abajo, precio, hora_str, bias)

    # Radar 0
    radar_impulse(df_entry, precio, zonas_arriba, zonas_abajo, bias)

    # Radar 3
    radar_sweep(df_entry, zona_ref_arriba, zona_ref_abajo, precio, zonas_arriba, zonas_abajo, bias)

    # Radar 4
    radar_breakout(df_entry, zona_ref_arriba, zona_ref_abajo, precio, zonas_arriba, zonas_abajo, bias)

    # Sistema
    heartbeat()
    sin_eventos()

# =========================
# INICIO
# =========================

if __name__ == "__main__":
    print("🚀 Iniciando BOT V12 (con bias, scoring y persistencia)...")
    precio_inicial = obtener_precio_actual()
    hora_actual = datetime.now(UTC).strftime('%H:%M')
    msg = f"🤖 BOT DE ARTURO FUNCIONANDO (V12)\nHora UTC: {hora_actual}\nPrecio: {fmt(precio_inicial)}"
    enviar(msg)

    last_heartbeat_time = datetime.now(UTC)
    last_event_time = datetime.now(UTC)
    last_mapa_time = None

    # Cargar historial si existe (opcional)
    if os.path.exists(HISTORIAL_FILE):
        try:
            with open(HISTORIAL_FILE, "r") as f:
                data = json.load(f)
                historial_eventos.extend(data[-1000:])  # últimos 1000
        except:
            pass

    while True:
        try:
            evaluar()
        except Exception as e:
            print(f"❌ Error en ciclo principal: {e}")
            enviar(f"⚠️ ERROR: {str(e)[:100]}")
            time.sleep(60)
        time.sleep(60)

        # Cada cierto tiempo, guardar historial (ej. cada hora)
        if datetime.now(UTC).minute == 0:  # cada hora exacta
            try:
                with open(HISTORIAL_FILE, "w") as f:
                    json.dump(list(historial_eventos), f)
            except:
                pass
