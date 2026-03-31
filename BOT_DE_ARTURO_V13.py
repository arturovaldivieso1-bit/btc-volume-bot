# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os
import numpy as np
from datetime import datetime, timedelta, UTC
from collections import deque
import json
import threading

# =========================
# CONFIGURACIÓN INICIAL
# =========================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOL = "BTCUSDT"

# Timeframes
INTERVAL_MACRO = "1h"          # para detección de estructura y zonas macro (2 semanas)
INTERVAL_ENTRY = "5m"           # para eventos
INTERVAL_BIAS = "4h"            # para tendencia macro (opcional)

# Parámetros de liquidez spot (usados internamente)
LOOKBACK = 168
MIN_TOUCHES = 3
CLUSTER_RANGE = 0.0025
PROXIMITY = 0.003
RADAR1_MIN_DIST = 0.01
ZONA_EQUIVALENTE = 0.01

# Open Interest (futuros)
OI_WINDOW = 50
OI_PERCENTILE = 75
OI_LOOKBACK = 3
OI_CLUSTER_RANGE = 0.002
OI_CONFIANZA_ALTA = 500_000_000
OI_CONFIANZA_MEDIA = 200_000_000
OI_CONFIANZA_BAJA = 100_000_000

SPOT_FUTUROS_TOLERANCIA = 0.005

# Radar 0 – solo variación de precio ≥0.65%
IMPULSE_PRICE_CHANGE = 0.65
IMPULSE_COOLDOWN = 300

# Radar 3 y 4 – filtro principal por volumen
SWEEP_VOLUME_FACTOR = 1.2
SWEEP_BREAK_MARGIN = 0.0005
SWEEP_PESO_MINIMO = 2
BREAKOUT_MARGIN = 0.003
BREAKOUT_RETEST_CANDLES = 1
BREAKOUT_PESO_MINIMO = 2
VOL_MIN_SWEEP = 400               # si volumen > 400 BTC, se genera setup automáticamente

# Umbrales de volumen (BTC) y probabilidades de continuación (estudio)
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

# Scoring y bias (ahora solo para confianza, no bloquean)
EMA_SHORT = 50
EMA_LONG = 200
SCORE_UMBRAL_ACCION_IMPULSO = 3      # para setup de impulso (solo si hay zona)
SCORE_UMBRAL_ACCION_SWEEP = 2        # bajo, pero solo se usará si volumen bajo (no generará setup)
SCORE_UMBRAL_ACCION_BREAKOUT = 2
SCORE_MAX_TEORICO = 30

PESOS = {
    "zona_spot": 1,
    "zona_oi": 2,
    "toques_altos": 1,
    "cercania": 1,
    "bias_favorable_tendencia": 3,
    "bias_favorable_lateral": 2,
    "evento_impulso": 1,
    "evento_sweep": 2,
    "evento_breakout": 2,
    "validacion_spot": 2,
    "volumen_alto": 3      # base, se añadirá bonificación por percentil
}

def peso_por_oi(oi_total):
    if oi_total > 500_000_000:
        return 4
    elif oi_total > 200_000_000:
        return 3
    elif oi_total > 100_000_000:
        return 2
    else:
        return 1

def bonificacion_volumen(volumen):
    if volumen > VOL1_P90:
        return 10
    elif volumen > VOL1_P75:
        return 6
    elif volumen > VOL1_MED:
        return 3
    else:
        return 0

# Sistema
HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6
MAPA_COOLDOWN_MINUTOS = 60
RADAR2_COOLDOWN_MINUTOS = 120
REDONDEO_BASE = 200

# ML Manual – evaluación
EVALUACION_VELAS_SCALP = 2        # 2 velas = 10 minutos
EVALUACION_VELAS_TEND = 6         # 6 velas = 30 minutos
EVALUACION_VELAS_LARGA = 12       # 12 velas = 1 hora (secundaria)
RANGO_EXITO_SCALP = 0.003         # 0.3%
RANGO_EXITO_TEND = 0.005          # 0.5%
RANGO_EXITO_LARGA = 0.01          # 1.0% (opcional)

# Deriva silenciosa
ALERTA_DERIVA_HORAS = 2
ALERTA_DERIVA_PORCENTAJE = 0.5    # 0.5%

# Radar 5 (estructura lenta)
ZONA_MACRO_ALERTA_DIST = 0.01      # 1% – alerta de aproximación
ZONA_MACRO_SETUP_DIST = 0.003      # 0.3% – generar setup
ZONA_MACRO_COOLDOWN_HORAS = 2      # no repetir para la misma zona en 2h

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
oi_increment_history = []
historial_eventos = deque(maxlen=2000)
ultimos_eventos = deque(maxlen=10)

# Régimen y zonas macro para Radar 5
regimen_actual = "NEUTRAL"
ultimo_cambio_regimen = None
estructura_detected_at = None
zonas_macro = []   # almacenará clusters de soporte/resistencia en 1h (2 semanas)
alerted_macro = {}   # {(centro_redondeado, direccion): timestamp}

# Deriva silenciosa
ultima_deriva_time = None
ultimo_precio_deriva = None

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

def cluster_precios(lista, rango=CLUSTER_RANGE):
    clusters = []
    for p in sorted(lista):
        agregado = False
        for c in clusters:
            if abs(p - c["centro"]) / p < rango:
                c["valores"].append(p)
                c["centro"] = sum(c["valores"]) / len(c["valores"])
                agregado = True
                break
        if not agregado:
            clusters.append({"centro": p, "valores": [p]})
    return clusters

def detectar_zonas_spot(df, lookback=LOOKBACK, min_toques=MIN_TOUCHES, rango=CLUSTER_RANGE):
    if df.empty or len(df) < lookback:
        return [], []
    highs = df["high"].tail(lookback).tolist()
    lows = df["low"].tail(lookback).tolist()
    clusters_high = cluster_precios(highs, rango)
    clusters_low = cluster_precios(lows, rango)
    zonas_high = []
    for c in clusters_high:
        if len(c["valores"]) >= min_toques:
            zonas_high.append({
                "tipo": "HIGH",
                "centro": c["centro"],
                "max": max(c["valores"]),
                "min": min(c["valores"]),
                "toques": len(c["valores"])
            })
    zonas_low = []
    for c in clusters_low:
        if len(c["valores"]) >= min_toques:
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
    if df_1h.empty or df_4h.empty:
        return "LATERAL"
    ema_short_1h = df_1h["close"].ewm(span=EMA_SHORT, adjust=False).mean().iloc[-1]
    ema_long_1h = df_1h["close"].ewm(span=EMA_LONG, adjust=False).mean().iloc[-1]
    ema_short_4h = df_4h["close"].ewm(span=EMA_SHORT, adjust=False).mean().iloc[-1]
    ema_long_4h = df_4h["close"].ewm(span=EMA_LONG, adjust=False).mean().iloc[-1]
    pendiente_1h = df_1h["close"].iloc[-1] - df_1h["close"].iloc[-2]
    pendiente_4h = df_4h["close"].iloc[-1] - df_4h["close"].iloc[-2]
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

def normalizar_score(score, max_teorico=SCORE_MAX_TEORICO):
    return round(score / max_teorico * 10, 1)

def calcular_peso_zona(zona_spot, zona_oi, precio_actual):
    peso = 0
    if zona_spot:
        peso += PESOS["zona_spot"]
        if zona_spot["toques"] >= 5:
            peso += PESOS["toques_altos"]
        if distancia(zona_spot, precio_actual) < 0.005:
            peso += PESOS["cercania"]
    if zona_oi:
        peso += peso_por_oi(zona_oi["oi_total"])
        if distancia(zona_oi, precio_actual) < 0.005:
            peso += PESOS["cercania"]
    return peso

def calcular_score_evento(evento_tipo, direccion_evento, bias, peso_zona, validacion_spot=False, volumen=None):
    score = peso_zona
    score += PESOS.get(f"evento_{evento_tipo}", 0)
    if validacion_spot:
        score += PESOS["validacion_spot"]
    if bias == "LATERAL":
        if evento_tipo == "sweep":
            score += PESOS["bias_favorable_lateral"]
    elif bias == direccion_evento:
        score += PESOS["bias_favorable_tendencia"]
    if volumen:
        score += bonificacion_volumen(volumen)
    return score

def obtener_probabilidad(volumen, umbrales, probabilidades):
    if volumen > umbrales[2]:
        return probabilidades[2]
    elif volumen > umbrales[1]:
        return probabilidades[1]
    elif volumen > umbrales[0]:
        return probabilidades[0]
    else:
        return None

def registrar_evento_para_patron(tipo, direccion):
    clave = f"{tipo}_{direccion}"
    ultimos_eventos.append(clave)
    if len(ultimos_eventos) >= 3:
        if all(e == f"sweep_{direccion}" for e in list(ultimos_eventos)[-3:]):
            enviar(f"⚠️ POSIBLE ACUMULACIÓN: 3 sweeps {direccion} consecutivos")

# =========================
# DETECCIÓN DE RÉGIMEN Y ZONAS MACRO (1h, 2 semanas)
# =========================

def detectar_estructura_y_zonas(df_1h):
    """
    Detecta régimen de mercado (ACUMULACION/DISTRIBUCION/NEUTRAL) y
    extrae zonas macro de soporte/resistencia usando 336 velas (2 semanas).
    """
    if df_1h.empty or len(df_1h) < 336:
        return "NEUTRAL", []

    # 1. Detectar régimen por pivotes (últimas 336 velas)
    highs = df_1h["high"].values[-336:]
    lows = df_1h["low"].values[-336:]
    pivot_highs = []
    pivot_lows = []
    for i in range(1, len(highs)-1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            pivot_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            pivot_lows.append(lows[i])
    if len(pivot_highs) < 2 and len(pivot_lows) < 2:
        regimen = "NEUTRAL"
    else:
        tendencia_highs = all(pivot_highs[i] < pivot_highs[i+1] for i in range(len(pivot_highs)-1)) if len(pivot_highs) >= 2 else False
        tendencia_lows = all(pivot_lows[i] < pivot_lows[i+1] for i in range(len(pivot_lows)-1)) if len(pivot_lows) >= 2 else False
        if tendencia_highs and tendencia_lows:
            regimen = "ACUMULACION"
        elif (not tendencia_highs) and (not tendencia_lows):
            regimen = "DISTRIBUCION"
        else:
            regimen = "NEUTRAL"

    # 2. Detectar zonas macro (soporte/resistencia) con un rango de agrupación más amplio (0.5%)
    zonas_macro = []
    # Usamos la misma función detectar_zonas_spot con lookback=336 y rango=0.005
    zonas_high, zonas_low = detectar_zonas_spot(df_1h, lookback=336, min_toques=3, rango=0.005)
    # Unir ambas listas y ordenar por toques
    zonas_macro = zonas_high + zonas_low
    zonas_macro.sort(key=lambda x: x["toques"], reverse=True)
    return regimen, zonas_macro[:5]   # guardamos hasta 5 zonas

def actualizar_regimen(df_1h, hubo_impulso, resultado_impulso=None):
    global regimen_actual, ultimo_cambio_regimen, estructura_detected_at, zonas_macro
    ahora = datetime.now(UTC)
    if hubo_impulso:
        regimen_actual = "IMPULSO"
        ultimo_cambio_regimen = ahora
        return
    if regimen_actual == "IMPULSO" and ultimo_cambio_regimen and (ahora - ultimo_cambio_regimen) > timedelta(minutes=15):
        if resultado_impulso == "FRACASO":
            nuevo_regimen, zonas_macro = detectar_estructura_y_zonas(df_1h)
            regimen_actual = nuevo_regimen
            ultimo_cambio_regimen = ahora
            estructura_detected_at = ahora
        return
    if regimen_actual != "IMPULSO":
        nuevo_regimen, zonas_macro = detectar_estructura_y_zonas(df_1h)
        if nuevo_regimen != regimen_actual:
            regimen_actual = nuevo_regimen
            ultimo_cambio_regimen = ahora
            if regimen_actual in ["ACUMULACION", "DISTRIBUCION"]:
                estructura_detected_at = ahora

# =========================
# OPEN INTEREST
# =========================

def obtener_open_interest_hist(period="5m", limit=200):
    url = "https://fapi.binance.com/futures/data/openInterestHist"
    params = {"symbol": SYMBOL, "period": period, "limit": limit}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        df = pd.DataFrame(data)
        df["sumOpenInterest"] = pd.to_numeric(df["sumOpenInterest"])
        df["sumOpenInterestValue"] = pd.to_numeric(df["sumOpenInterestValue"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit='ms')
        return df
    except Exception as e:
        print(f"Error OI: {e}")
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
        oi_act = float(df_oi.iloc[i]["sumOpenInterestValue"])
        oi_ant = float(df_oi.iloc[i-1]["sumOpenInterestValue"])
        nuevos_incrementos.append(max(0, oi_act - oi_ant))
    oi_increment_history.extend(nuevos_incrementos)
    if len(oi_increment_history) > OI_WINDOW:
        oi_increment_history = oi_increment_history[-OI_WINDOW:]
    umbral = np.percentile(oi_increment_history, OI_PERCENTILE) if len(oi_increment_history) >= 10 else 20_000_000
    eventos = []
    for i in range(OI_LOOKBACK, len(df_oi)):
        suma = 0
        for j in range(i - OI_LOOKBACK + 1, i + 1):
            oi_act = float(df_oi.iloc[j]["sumOpenInterestValue"])
            oi_ant = float(df_oi.iloc[j-1]["sumOpenInterestValue"])
            suma += max(0, oi_act - oi_ant)
        if suma > umbral:
            ts = df_oi.iloc[i]["timestamp"]
            precio_zona = None
            if not df_spot.empty and 'time' in df_spot.columns:
                df_spot['time_dt'] = pd.to_datetime(df_spot['time'], unit='ms')
                idx = (df_spot['time_dt'] - ts).abs().idxmin()
                precio_zona = df_spot.loc[idx, 'close']
            if precio_zona:
                eventos.append({"precio": precio_zona, "oi_incremento": suma, "timestamp": ts})
    if not eventos:
        return []
    clusters = cluster_oi_por_precio(eventos)
    for c in clusters:
        if c["oi_total"] > OI_CONFIANZA_ALTA:
            c["confianza"] = "🔥🔥🔥"
        elif c["oi_total"] > OI_CONFIANZA_MEDIA:
            c["confianza"] = "🔥🔥"
        else:
            c["confianza"] = "🔥"
    return clusters

# =========================
# RADARES INTERNOS (1 y 2)
# =========================

def actualizar_zonas_internas(mejor_zona_oi_arriba, mejor_zona_oi_abajo, mejor_zona_spot_arriba, mejor_zona_spot_abajo, precio, hora, bias):
    global ultima_zona_arriba, ultima_zona_abajo
    if mejor_zona_oi_arriba:
        ultima_zona_arriba = mejor_zona_oi_arriba["centro"]
    elif mejor_zona_spot_arriba and distancia(mejor_zona_spot_arriba, precio) >= RADAR1_MIN_DIST:
        ultima_zona_arriba = mejor_zona_spot_arriba["centro"]
    else:
        ultima_zona_arriba = None
    if mejor_zona_oi_abajo:
        ultima_zona_abajo = mejor_zona_oi_abajo["centro"]
    elif mejor_zona_spot_abajo and distancia(mejor_zona_spot_abajo, precio) >= RADAR1_MIN_DIST:
        ultima_zona_abajo = mejor_zona_spot_abajo["centro"]
    else:
        ultima_zona_abajo = None

def radar_proximidad_interno(mejor_zona_arriba, mejor_zona_abajo, precio, hora, bias):
    global last_event_time
    ahora = datetime.now(UTC)
    def check_and_record(zona, tipo):
        if zona is None:
            return
        centro_rd = redondear_centro(zona["centro"])
        key = (centro_rd, tipo)
        dist = distancia(zona, precio)
        if PROXIMITY <= dist < RADAR1_MIN_DIST:
            ultimo = alerted_proximidad.get(key)
            if ultimo is None or (ahora - ultimo) > timedelta(minutes=RADAR2_COOLDOWN_MINUTOS):
                alerted_proximidad[key] = ahora
                last_event_time = ahora
    if mejor_zona_arriba:
        check_and_record(mejor_zona_arriba, "HIGH")
    if mejor_zona_abajo:
        check_and_record(mejor_zona_abajo, "LOW")

# =========================
# RADAR 0 – IMPULSO (siempre informa, setup si volumen alto o score)
# =========================

def generar_setup_impulso(zona, precio_actual, direccion, score_norm):
    if zona is None:
        return None
    if direccion == "ALCISTA":
        entrada = round(precio_actual, 1)
        sl = round(zona["min"] * 0.997, 1)
        tp = round(zona["centro"] * 1.008, 1)
        accion = "COMPRA (LONG)"
    else:
        entrada = round(precio_actual, 1)
        sl = round(zona["max"] * 1.003, 1)
        tp = round(zona["centro"] * 0.992, 1)
        accion = "VENTA (SHORT)"
    confianza = "ALTA" if score_norm >= 7 else "MEDIA" if score_norm >= 5 else "BAJA"
    return {
        "accion": accion,
        "entrada": entrada,
        "sl": sl,
        "tp": tp,
        "confianza": confianza
    }

def radar_impulse(df_entry, precio_actual, zonas_arriba, zonas_abajo, bias):
    global last_impulse_time, last_event_time, historial_eventos, regimen_actual
    if df_entry.empty or len(df_entry) < 3:
        return False

    vela = df_entry.iloc[-1]
    try:
        open_price = float(vela["open"])
        close_price = float(vela["close"])
        volume = float(vela["volume"])
        vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]
    except:
        return False

    price_change = abs(close_price - open_price) / open_price * 100
    if price_change < IMPULSE_PRICE_CHANGE:
        return False

    ahora = datetime.now(UTC)
    if last_impulse_time and (ahora - last_impulse_time).seconds < IMPULSE_COOLDOWN:
        return False

    alcista = close_price > open_price
    direccion = "ALCISTA" if alcista else "BAJISTA"
    emoji = "🟢" if alcista else "🔴"

    # Zona relevante (solo para contexto)
    zona_relevante = None
    if alcista and zonas_arriba:
        zona_relevante = zonas_arriba[0]
    elif not alcista and zonas_abajo:
        zona_relevante = zonas_abajo[0]

    # Probabilidades de continuación (1,2,3 velas misma dirección)
    prob_lines = []
    vol1 = volume
    prob1 = obtener_probabilidad(vol1, [VOL1_MED, VOL1_P75, VOL1_P90], [PROB1_MED, PROB1_P75, PROB1_P90])
    if prob1:
        prob_lines.append(f"  1v ({vol1:.0f}): {prob1}%")

    if len(df_entry) >= 2:
        vela_ant = df_entry.iloc[-2]
        alcista_ant = vela_ant["close"] > vela_ant["open"]
        if alcista == alcista_ant:
            vol2 = vol1 + vela_ant["volume"]
            prob2 = obtener_probabilidad(vol2, [VOL2_MED, VOL2_P75, VOL2_P90], [PROB2_MED, PROB2_P75, PROB2_P90])
            if prob2:
                prob_lines.append(f"  2v (misma dir, {vol2:.0f}): {prob2}%")

    if len(df_entry) >= 3:
        vela_ant2 = df_entry.iloc[-3]
        alcista_ant2 = vela_ant2["close"] > vela_ant2["open"]
        if alcista == alcista_ant and alcista == alcista_ant2:
            vol3 = vol1 + df_entry.iloc[-2]["volume"] + df_entry.iloc[-3]["volume"]
            prob3 = obtener_probabilidad(vol3, [VOL3_MED, VOL3_P75, VOL3_P90], [PROB3_MED, PROB3_P75, PROB3_P90])
            if prob3:
                prob_lines.append(f"  3v (misma dir, {vol3:.0f}): {prob3}%")

    if not prob_lines:
        prob_lines.append(f"  Volumen insuficiente: prob base 68%")

    # Registrar evento
    evento = {
        "timestamp": ahora.isoformat(),
        "tipo": "impulso",
        "direccion": direccion,
        "precio": precio_actual,
        "score_abs": 0,
        "score_norm": 0,
        "volumen": volume,
        "volumen_ratio": volume / vol_medio if vol_medio else 1.0,
        "zona_centro": zona_relevante['centro'] if zona_relevante else None,
        "resultado_scalp": None,
        "resultado_tend": None,
        "resultado_largo": None,
        "evaluado_scalp": False,
        "evaluado_tend": False,
        "evaluado_largo": False,
        "evaluado": False
    }
    historial_eventos.append(evento)

    # Calcular score y setup (solo si hay zona)
    score_norm = 0
    setup = None
    if zona_relevante:
        peso_zona = calcular_peso_zona(zona_relevante if 'toques' in zona_relevante else None,
                                        zona_relevante if 'oi_total' in zona_relevante else None, precio_actual)
        validacion_spot = False
        if 'oi_total' in zona_relevante:
            for z in (zonas_arriba + zonas_abajo):
                if 'toques' in z and abs(zona_relevante['centro'] - z['centro']) / zona_relevante['centro'] < SPOT_FUTUROS_TOLERANCIA:
                    validacion_spot = True
                    break
        score_abs = calcular_score_evento("impulso", direccion, bias, peso_zona, validacion_spot, volume)
        score_norm = normalizar_score(score_abs)
        evento["score_abs"] = score_abs
        evento["score_norm"] = score_norm
        # Setup si volumen >400 o score suficiente
        if volume > VOL_MIN_SWEEP or score_norm >= SCORE_UMBRAL_ACCION_IMPULSO:
            setup = generar_setup_impulso(zona_relevante, precio_actual, direccion, score_norm)

    # Mensaje con simbología
    titulo = f"🚀 **IMPULSO {direccion} {emoji}** – Score {score_norm}/10"
    msg = f"{titulo}\n\n"
    msg += f"Precio: {fmt(precio_actual)} - Hora UTC: {ahora.strftime('%H:%M')}\n"
    msg += f"Variación: {price_change:.2f}%\n"
    msg += f"Volumen: {vol1:.0f} BTC ({(vol1/vol_medio):.1f}x media)\n"
    msg += f"Bias: {bias}\n\n"
    msg += f"📊 Probabilidad:\n" + "\n".join(prob_lines)
    if zona_relevante:
        msg += f"\n🧲 Z objetivo: {fmt(zona_relevante['centro'])} ({distancia(zona_relevante, precio_actual)*100:.1f}%)"
    if setup:
        msg += f"\n\n👉 **SETUP**\n"
        msg += f"Entrada: {fmt(setup['entrada'])}\n"
        msg += f"SL: {fmt(setup['sl'])} ({(setup['entrada']-setup['sl'])/setup['entrada']*100:.2f}%)\n"
        msg += f"TP: {fmt(setup['tp'])} ({(setup['tp']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
        msg += f"Confianza: {setup['confianza']}"
    enviar(msg)

    last_impulse_time = ahora
    last_event_time = ahora
    return True

# =========================
# RADAR 3 (SWEEP) – setup solo si volumen >400 BTC
# =========================

def generar_setup_sweep(zona, precio_actual, direccion_rev, score_norm):
    if zona is None:
        return None
    if direccion_rev == "ALCISTA":
        entrada = round(precio_actual, 1)
        sl = round(zona["min"] * 0.997, 1)
        tp = round(zona["centro"] * 1.008, 1)
        accion = "COMPRA (LONG)"
    else:
        entrada = round(precio_actual, 1)
        sl = round(zona["max"] * 1.003, 1)
        tp = round(zona["centro"] * 0.992, 1)
        accion = "VENTA (SHORT)"
    confianza = "ALTA" if score_norm >= 7 else "MEDIA" if score_norm >= 5 else "BAJA"
    return {
        "accion": accion,
        "entrada": entrada,
        "sl": sl,
        "tp": tp,
        "confianza": confianza
    }

def radar_sweep(df_entry, mejor_zona_arriba, mejor_zona_abajo, precio_actual, zonas_arriba, zonas_abajo, bias):
    global sweep_pendiente, last_event_time, historial_eventos, regimen_actual
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

        vela_sweep = df_entry.iloc[-2]
        vol_sweep = vela_sweep["volume"]

        # Probabilidad de continuación
        prob1 = obtener_probabilidad(vol_sweep, [VOL1_MED, VOL1_P75, VOL1_P90], [PROB1_MED, PROB1_P75, PROB1_P90])
        prob_line = f"  Volumen: {vol_sweep:.0f} BTC -> {prob1}%" if prob1 else "  Volumen bajo, prob base 68%"

        # Calcular score (solo para confianza)
        peso_zona = calcular_peso_zona(zona if 'toques' in zona else None,
                                        zona if 'oi_total' in zona else None, precio_actual)
        validacion_spot = False
        if 'oi_total' in zona:
            for z in (zonas_arriba + zonas_abajo):
                if 'toques' in z and abs(zona['centro'] - z['centro']) / zona['centro'] < SPOT_FUTUROS_TOLERANCIA:
                    validacion_spot = True
                    break
        score_abs = calcular_score_evento("sweep", direccion_rev, bias, peso_zona, validacion_spot, vol_sweep)
        score_norm = normalizar_score(score_abs)

        # Registrar evento (para estadísticas)
        ahora = datetime.now(UTC)
        color_zona = "🟢" if tipo_sweep == "HIGH" else "🔴"
        evento = {
            "timestamp": ahora.isoformat(),
            "tipo": "sweep",
            "direccion": direccion_rev,
            "precio": precio_actual,
            "score_abs": score_abs,
            "score_norm": score_norm,
            "volumen": vol_sweep,
            "zona_centro": zona['centro'],
            "resultado_scalp": None,
            "resultado_tend": None,
            "resultado_largo": None,
            "evaluado_scalp": False,
            "evaluado_tend": False,
            "evaluado_largo": False,
            "evaluado": False
        }
        historial_eventos.append(evento)

        # Setup solo si volumen > 400 BTC
        if vol_sweep > VOL_MIN_SWEEP:
            setup = generar_setup_sweep(zona, precio_actual, direccion_rev, score_norm)
            if setup:
                msg = f"🔄 **SETUP REVERSIÓN {direccion_rev} (SWEEP)** – Score {score_norm}/10\n\n"
                msg += f"Entrada: {fmt(setup['entrada'])}\n"
                msg += f"SL: {fmt(setup['sl'])} ({(setup['entrada']-setup['sl'])/setup['entrada']*100:.2f}%)\n"
                msg += f"TP: {fmt(setup['tp'])} ({(setup['tp']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
                msg += f"Confianza: {setup['confianza']}\n\n"
                msg += f"🧲 Z barrida: {fmt(zona['centro'])} | P. actual: {fmt(precio_actual)} | Hora: {ahora.strftime('%H:%M')}\n"
                msg += f"Volumen: {vol_sweep:.0f} BTC\n{prob_line}\n"
                msg += f"Bias: {bias}"
                enviar(msg)
        else:
            # Si volumen bajo, solo informamos (sin setup)
            msg = f"🔄 **SWEEP {direccion_rev} (sin setup)** – Score {score_norm}/10\n\n"
            msg += f"Z barrida: {fmt(zona['centro'])} | P. actual: {fmt(precio_actual)} | Hora: {ahora.strftime('%H:%M')}\n"
            msg += f"Volumen: {vol_sweep:.0f} BTC\n{prob_line}\n"
            msg += f"Bias: {bias}"
            enviar(msg)

        last_event_time = ahora
        sweep_pendiente = None
        return

    if len(df_entry) < 2:
        return
    vela_anterior = df_entry.iloc[-2]
    vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]

    zonas_a_evaluar = []
    if mejor_zona_arriba:
        peso = calcular_peso_zona(mejor_zona_arriba if 'toques' in mejor_zona_arriba else None,
                                   mejor_zona_arriba if 'oi_total' in mejor_zona_arriba else None, precio_actual)
        if peso >= SWEEP_PESO_MINIMO:
            zonas_a_evaluar.append(mejor_zona_arriba)
    if mejor_zona_abajo:
        peso = calcular_peso_zona(mejor_zona_abajo if 'toques' in mejor_zona_abajo else None,
                                   mejor_zona_abajo if 'oi_total' in mejor_zona_abajo else None, precio_actual)
        if peso >= SWEEP_PESO_MINIMO:
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
# RADAR 4 (BREAKOUT) – setup solo si volumen >400 BTC
# =========================

def generar_setup_breakout(zona, precio_actual, direccion, score_norm):
    if zona is None:
        return None
    if direccion == "ALCISTA":
        entrada = round(precio_actual, 1)
        sl = round(zona["min"] * 0.997, 1)
        tp = round(zona["centro"] * 1.015, 1)
        accion = "COMPRA (LONG)"
    else:
        entrada = round(precio_actual, 1)
        sl = round(zona["max"] * 1.003, 1)
        tp = round(zona["centro"] * 0.985, 1)
        accion = "VENTA (SHORT)"
    confianza = "ALTA" if score_norm >= 7 else "MEDIA" if score_norm >= 5 else "BAJA"
    return {
        "accion": accion,
        "entrada": entrada,
        "sl": sl,
        "tp": tp,
        "confianza": confianza
    }

def radar_breakout(df_entry, mejor_zona_arriba, mejor_zona_abajo, precio_actual, zonas_arriba, zonas_abajo, bias):
    global zona_consumida, last_event_time, alerted_liquidity, historial_eventos, regimen_actual
    if df_entry.empty or len(df_entry) < BREAKOUT_RETEST_CANDLES + 1:
        return
    vela = df_entry.iloc[-1]
    close = vela["close"]
    vol_break = vela["volume"]
    ahora = datetime.now(UTC)

    def hubo_retest(zona, direccion):
        if BREAKOUT_RETEST_CANDLES == 0:
            return True
        for i in range(2, BREAKOUT_RETEST_CANDLES + 2):
            if i > len(df_entry):
                break
            vela_ant = df_entry.iloc[-i]
            if vela_ant["close"] > zona["min"] and vela_ant["close"] < zona["max"]:
                return True
        return False

    if mejor_zona_arriba:
        key = ("break", redondear_centro(mejor_zona_arriba["centro"]))
        if key not in alerted_liquidity and close > mejor_zona_arriba.get("max", mejor_zona_arriba["centro"]*1.01) * (1 + BREAKOUT_MARGIN):
            if hubo_retest(mejor_zona_arriba, "ALCISTA"):
                peso_zona = calcular_peso_zona(mejor_zona_arriba if 'toques' in mejor_zona_arriba else None,
                                                mejor_zona_arriba if 'oi_total' in mejor_zona_arriba else None, precio_actual)
                if peso_zona >= BREAKOUT_PESO_MINIMO:
                    validacion_spot = False
                    if 'oi_total' in mejor_zona_arriba:
                        for z in (zonas_arriba + zonas_abajo):
                            if 'toques' in z and abs(mejor_zona_arriba['centro'] - z['centro']) / mejor_zona_arriba['centro'] < SPOT_FUTUROS_TOLERANCIA:
                                validacion_spot = True
                                break
                    score_abs = calcular_score_evento("breakout", "ALCISTA", bias, peso_zona, validacion_spot, vol_break)
                    score_norm = normalizar_score(score_abs)
                    if vol_break > VOL_MIN_SWEEP:
                        alerted_liquidity.add(key)
                        zona_consumida = True
                        evento = {
                            "timestamp": ahora.isoformat(),
                            "tipo": "breakout",
                            "direccion": "ALCISTA",
                            "precio": close,
                            "score_abs": score_abs,
                            "score_norm": score_norm,
                            "volumen": vol_break,
                            "zona_centro": mejor_zona_arriba['centro'],
                            "resultado_scalp": None,
                            "resultado_tend": None,
                            "resultado_largo": None,
                            "evaluado_scalp": False,
                            "evaluado_tend": False,
                            "evaluado_largo": False,
                            "evaluado": False
                        }
                        historial_eventos.append(evento)
                        registrar_evento_para_patron("breakout", "ALCISTA")
                        setup = generar_setup_breakout(mejor_zona_arriba, close, "ALCISTA", score_norm)
                        if setup:
                            prob = obtener_probabilidad(vol_break, [VOL1_MED, VOL1_P75, VOL1_P90], [PROB1_MED, PROB1_P75, PROB1_P90])
                            prob_line = f"Probabilidad: {prob}%" if prob else "Probabilidad base 68%"
                            msg = f"🌋 **SETUP {setup['accion']} (BREAKOUT ALCISTA)** – Score {score_norm}/10\n\n"
                            msg += f"Entrada: {fmt(setup['entrada'])}\n"
                            msg += f"SL: {fmt(setup['sl'])} ({(setup['entrada']-setup['sl'])/setup['entrada']*100:.2f}%)\n"
                            msg += f"TP: {fmt(setup['tp'])} ({(setup['tp']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
                            msg += f"Confianza: {setup['confianza']}\n\n"
                            msg += f"🧲 Z rota: {fmt(mejor_zona_arriba['centro'])} | Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}\n"
                            msg += f"Volumen: {vol_break:.0f} BTC\n{prob_line}\n"
                            msg += f"Bias: {bias}"
                            enviar(msg)
                        last_event_time = ahora
                        return

    if mejor_zona_abajo:
        key = ("break", redondear_centro(mejor_zona_abajo["centro"]))
        if key not in alerted_liquidity and close < mejor_zona_abajo.get("min", mejor_zona_abajo["centro"]*0.99) * (1 - BREAKOUT_MARGIN):
            if hubo_retest(mejor_zona_abajo, "BAJISTA"):
                peso_zona = calcular_peso_zona(mejor_zona_abajo if 'toques' in mejor_zona_abajo else None,
                                                mejor_zona_abajo if 'oi_total' in mejor_zona_abajo else None, precio_actual)
                if peso_zona >= BREAKOUT_PESO_MINIMO:
                    validacion_spot = False
                    if 'oi_total' in mejor_zona_abajo:
                        for z in (zonas_arriba + zonas_abajo):
                            if 'toques' in z and abs(mejor_zona_abajo['centro'] - z['centro']) / mejor_zona_abajo['centro'] < SPOT_FUTUROS_TOLERANCIA:
                                validacion_spot = True
                                break
                    score_abs = calcular_score_evento("breakout", "BAJISTA", bias, peso_zona, validacion_spot, vol_break)
                    score_norm = normalizar_score(score_abs)
                    if vol_break > VOL_MIN_SWEEP:
                        alerted_liquidity.add(key)
                        zona_consumida = True
                        evento = {
                            "timestamp": ahora.isoformat(),
                            "tipo": "breakout",
                            "direccion": "BAJISTA",
                            "precio": close,
                            "score_abs": score_abs,
                            "score_norm": score_norm,
                            "volumen": vol_break,
                            "zona_centro": mejor_zona_abajo['centro'],
                            "resultado_scalp": None,
                            "resultado_tend": None,
                            "resultado_largo": None,
                            "evaluado_scalp": False,
                            "evaluado_tend": False,
                            "evaluado_largo": False,
                            "evaluado": False
                        }
                        historial_eventos.append(evento)
                        registrar_evento_para_patron("breakout", "BAJISTA")
                        setup = generar_setup_breakout(mejor_zona_abajo, close, "BAJISTA", score_norm)
                        if setup:
                            prob = obtener_probabilidad(vol_break, [VOL1_MED, VOL1_P75, VOL1_P90], [PROB1_MED, PROB1_P75, PROB1_P90])
                            prob_line = f"Probabilidad: {prob}%" if prob else "Probabilidad base 68%"
                            msg = f"🌋 **SETUP {setup['accion']} (BREAKOUT BAJISTA)** – Score {score_norm}/10\n\n"
                            msg += f"Entrada: {fmt(setup['entrada'])}\n"
                            msg += f"SL: {fmt(setup['sl'])} ({(setup['sl']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
                            msg += f"TP: {fmt(setup['tp'])} ({(setup['entrada']-setup['tp'])/setup['entrada']*100:.2f}%)\n"
                            msg += f"Confianza: {setup['confianza']}\n\n"
                            msg += f"🧲 Z rota: {fmt(mejor_zona_abajo['centro'])} | Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}\n"
                            msg += f"Volumen: {vol_break:.0f} BTC\n{prob_line}\n"
                            msg += f"Bias: {bias}"
                            enviar(msg)
                        last_event_time = ahora
                        return

# =========================
# RADAR 5 – ESTRUCTURA LENTA (con alerta de aproximación y setup opcional)
# =========================

def generar_setup_lento(zona, precio_actual, direccion):
    if zona is None:
        return None
    if direccion == "LONG":
        entrada = round(precio_actual, 1)
        sl = round(zona["min"] * 0.995, 1)
        tp = round(zona["max"] * 1.02, 1)   # objetivo amplio
        accion = "COMPRA (LONG)"
    else:
        entrada = round(precio_actual, 1)
        sl = round(zona["max"] * 1.005, 1)
        tp = round(zona["min"] * 0.98, 1)
        accion = "VENTA (SHORT)"
    return {
        "accion": accion,
        "entrada": entrada,
        "sl": sl,
        "tp": tp,
        "confianza": "MEDIA"
    }

def radar_estructura_lenta(precio_actual, zonas_macro, regimen):
    global last_event_time, alerted_macro
    if regimen not in ["ACUMULACION", "DISTRIBUCION"]:
        return
    if not zonas_macro:
        return

    ahora = datetime.now(UTC)
    # Buscar la zona macro más cercana
    zonas_cercanas = []
    for z in zonas_macro:
        dist = abs(z["centro"] - precio_actual) / precio_actual
        if dist < ZONA_MACRO_ALERTA_DIST:
            zonas_cercanas.append((z, dist))
    if not zonas_cercanas:
        return

    zonas_cercanas.sort(key=lambda x: x[1])
    zona, dist = zonas_cercanas[0]
    direccion = "LONG" if regimen == "ACUMULACION" else "SHORT"

    # Cooldown por zona redondeada
    key = (redondear_centro(zona["centro"], 200), direccion)
    ultimo = alerted_macro.get(key)
    if ultimo and (ahora - ultimo) < timedelta(hours=ZONA_MACRO_COOLDOWN_HORAS):
        return
    alerted_macro[key] = ahora

    # Construir mensaje
    titulo = f"🐢 **ALERTA ESTRUCTURA LENTA ({regimen})**"
    msg = f"{titulo}\n\n"
    msg += f"🧲 Z macro: {fmt(zona['centro'])} (rango {fmt(zona['min'])}-{fmt(zona['max'])}) | Distancia: {dist*100:.2f}%\n"
    msg += f"P. actual: {fmt(precio_actual)} | Dirección: {direccion}\n"

    # Si está muy cerca, añadir setup
    if dist < ZONA_MACRO_SETUP_DIST:
        setup = generar_setup_lento(zona, precio_actual, direccion)
        if setup:
            msg += f"\n👉 **SETUP**\n"
            msg += f"Entrada: {fmt(setup['entrada'])}\n"
            msg += f"SL: {fmt(setup['sl'])} ({(setup['entrada']-setup['sl'])/setup['entrada']*100:.2f}%)\n"
            msg += f"TP: {fmt(setup['tp'])} ({(setup['tp']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
            msg += f"Confianza: {setup['confianza']}"
    else:
        msg += "\n(aprox.)"

    enviar(msg)
    last_event_time = ahora

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
        msg = f"🤖 BOT DE ARTURO FUNCIONANDO (V13.5)\nHora UTC: {ahora.strftime('%H:%M')}\nPrecio: {fmt(precio)}\nRégimen: {regimen_actual}"
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
# ALERTA DE DERIVA SILENCIOSA
# =========================

def alerta_deriva_silenciosa(precio_actual, regimen):
    global ultima_deriva_time, ultimo_precio_deriva
    ahora = datetime.now(UTC)
    if ultima_deriva_time is None:
        ultima_deriva_time = ahora
        ultimo_precio_deriva = precio_actual
        return

    if (ahora - ultima_deriva_time) >= timedelta(hours=ALERTA_DERIVA_HORAS):
        variacion = abs(precio_actual - ultimo_precio_deriva) / ultimo_precio_deriva * 100
        if variacion >= ALERTA_DERIVA_PORCENTAJE:
            direccion = "ALCISTA" if precio_actual > ultimo_precio_deriva else "BAJISTA"
            msg = f"📉 **DERIVA SILENCIOSA** – {variacion:.1f}% en {ALERTA_DERIVA_HORAS}h\n"
            msg += f"Precio: {fmt(ultimo_precio_deriva)} → {fmt(precio_actual)}\n"
            msg += f"Dirección: {direccion} ({regimen})"
            enviar(msg)
        # Reiniciar contador
        ultima_deriva_time = ahora
        ultimo_precio_deriva = precio_actual

# =========================
# EVALUACIÓN DE RESULTADOS (2v, 6v, 12v)
# =========================

def evaluar_eventos_pendientes(precio_actual):
    for evento in list(historial_eventos):
        # Scalping (2 velas, 0.3%)
        if not evento.get("evaluado_scalp", False):
            ts_evento = datetime.fromisoformat(evento["timestamp"])
            if (datetime.now(UTC) - ts_evento) > timedelta(minutes=EVALUACION_VELAS_SCALP * 5):
                precio_inicial = evento["precio"]
                direccion = evento["direccion"]
                if direccion == "ALCISTA":
                    if precio_actual >= precio_inicial * (1 + RANGO_EXITO_SCALP):
                        evento["resultado_scalp"] = "EXITO"
                    elif precio_actual <= precio_inicial * (1 - RANGO_EXITO_SCALP):
                        evento["resultado_scalp"] = "FRACASO"
                    else:
                        evento["resultado_scalp"] = "NEUTRO"
                else:
                    if precio_actual <= precio_inicial * (1 - RANGO_EXITO_SCALP):
                        evento["resultado_scalp"] = "EXITO"
                    elif precio_actual >= precio_inicial * (1 + RANGO_EXITO_SCALP):
                        evento["resultado_scalp"] = "FRACASO"
                    else:
                        evento["resultado_scalp"] = "NEUTRO"
                evento["evaluado_scalp"] = True

        # Tendencia (6 velas, 0.5%)
        if not evento.get("evaluado_tend", False):
            ts_evento = datetime.fromisoformat(evento["timestamp"])
            if (datetime.now(UTC) - ts_evento) > timedelta(minutes=EVALUACION_VELAS_TEND * 5):
                precio_inicial = evento["precio"]
                direccion = evento["direccion"]
                if direccion == "ALCISTA":
                    if precio_actual >= precio_inicial * (1 + RANGO_EXITO_TEND):
                        evento["resultado_tend"] = "EXITO"
                    elif precio_actual <= precio_inicial * (1 - RANGO_EXITO_TEND):
                        evento["resultado_tend"] = "FRACASO"
                    else:
                        evento["resultado_tend"] = "NEUTRO"
                else:
                    if precio_actual <= precio_inicial * (1 - RANGO_EXITO_TEND):
                        evento["resultado_tend"] = "EXITO"
                    elif precio_actual >= precio_inicial * (1 + RANGO_EXITO_TEND):
                        evento["resultado_tend"] = "FRACASO"
                    else:
                        evento["resultado_tend"] = "NEUTRO"
                evento["evaluado_tend"] = True

        # Largo plazo (12 velas, 1.0%) – opcional
        if not evento.get("evaluado_largo", False):
            ts_evento = datetime.fromisoformat(evento["timestamp"])
            if (datetime.now(UTC) - ts_evento) > timedelta(minutes=EVALUACION_VELAS_LARGA * 5):
                precio_inicial = evento["precio"]
                direccion = evento["direccion"]
                if direccion == "ALCISTA":
                    if precio_actual >= precio_inicial * (1 + RANGO_EXITO_LARGA):
                        evento["resultado_largo"] = "EXITO"
                    elif precio_actual <= precio_inicial * (1 - RANGO_EXITO_LARGA):
                        evento["resultado_largo"] = "FRACASO"
                    else:
                        evento["resultado_largo"] = "NEUTRO"
                else:
                    if precio_actual <= precio_inicial * (1 - RANGO_EXITO_LARGA):
                        evento["resultado_largo"] = "EXITO"
                    elif precio_actual >= precio_inicial * (1 + RANGO_EXITO_LARGA):
                        evento["resultado_largo"] = "FRACASO"
                    else:
                        evento["resultado_largo"] = "NEUTRO"
                evento["evaluado_largo"] = True

        if evento.get("evaluado_scalp", False) and evento.get("evaluado_tend", False) and evento.get("evaluado_largo", False):
            evento["evaluado"] = True

def generar_informe_resultados_como_texto():
    if not historial_eventos:
        return None
    df = pd.DataFrame(list(historial_eventos))
    df_scalp = df[df["evaluado_scalp"] == True]
    df_tend = df[df["evaluado_tend"] == True]
    df_largo = df[df["evaluado_largo"] == True]

    if df_scalp.empty and df_tend.empty and df_largo.empty:
        return None

    informe = "📊 **INFORME DE RESULTADOS (V13.5)**\n\n"
    for tipo in df["tipo"].unique():
        informe += f"🔹 {tipo.upper()}\n"

        # Scalping (2v)
        subset = df_scalp[df_scalp["tipo"] == tipo]
        if not subset.empty:
            total = len(subset)
            exitos = len(subset[subset["resultado_scalp"] == "EXITO"])
            fracasos = len(subset[subset["resultado_scalp"] == "FRACASO"])
            neutros = len(subset[subset["resultado_scalp"] == "NEUTRO"])
            tasa = exitos / total * 100 if total else 0
            informe += f"   ⏱️ Scalping (2v): {total} ev | Éxito: {exitos} ({tasa:.1f}%) | Fracaso: {fracasos} | Neutro: {neutros}\n"
            # Desglose por volumen
            if "volumen" in subset.columns:
                vol_bajo = subset[subset["volumen"] < 300]
                vol_medio = subset[(subset["volumen"] >= 300) & (subset["volumen"] <= 600)]
                vol_alto = subset[subset["volumen"] > 600]
                if not vol_bajo.empty:
                    ex = len(vol_bajo[vol_bajo["resultado_scalp"] == "EXITO"])
                    to = len(vol_bajo)
                    informe += f"      📊 Vol <300: {to} ops, éxito {ex/to*100:.1f}%\n"
                if not vol_medio.empty:
                    ex = len(vol_medio[vol_medio["resultado_scalp"] == "EXITO"])
                    to = len(vol_medio)
                    informe += f"      📊 Vol 300-600: {to} ops, éxito {ex/to*100:.1f}%\n"
                if not vol_alto.empty:
                    ex = len(vol_alto[vol_alto["resultado_scalp"] == "EXITO"])
                    to = len(vol_alto)
                    informe += f"      📊 Vol >600: {to} ops, éxito {ex/to*100:.1f}%\n"
            # Score promedio
            if "score_norm" in subset.columns:
                score_exito = subset[subset["resultado_scalp"] == "EXITO"]["score_norm"].mean()
                score_fracaso = subset[subset["resultado_scalp"] == "FRACASO"]["score_norm"].mean() if len(subset[subset["resultado_scalp"] == "FRACASO"]) > 0 else 0
                informe += f"      🎯 Score promedio: éxito {score_exito:.1f} / fracaso {score_fracaso:.1f}\n"

        # Tendencia (6v)
        subset = df_tend[df_tend["tipo"] == tipo]
        if not subset.empty:
            total = len(subset)
            exitos = len(subset[subset["resultado_tend"] == "EXITO"])
            fracasos = len(subset[subset["resultado_tend"] == "FRACASO"])
            neutros = len(subset[subset["resultado_tend"] == "NEUTRO"])
            tasa = exitos / total * 100 if total else 0
            informe += f"   📈 Tendencia (6v): {total} ev | Éxito: {exitos} ({tasa:.1f}%) | Fracaso: {fracasos} | Neutro: {neutros}\n"

        # Largo plazo (12v)
        subset = df_largo[df_largo["tipo"] == tipo]
        if not subset.empty:
            total = len(subset)
            exitos = len(subset[subset["resultado_largo"] == "EXITO"])
            fracasos = len(subset[subset["resultado_largo"] == "FRACASO"])
            neutros = len(subset[subset["resultado_largo"] == "NEUTRO"])
            tasa = exitos / total * 100 if total else 0
            informe += f"   🐢 Largo plazo (12v): {total} ev | Éxito: {exitos} ({tasa:.1f}%) | Fracaso: {fracasos} | Neutro: {neutros}\n"

        informe += "\n"
    return informe

# =========================
# COMANDOS DE TELEGRAM
# =========================

ultimo_update_id = None

def obtener_mensajes():
    global ultimo_update_id
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params = {"timeout": 30, "offset": ultimo_update_id}
    try:
        r = requests.get(url, params=params, timeout=35)
        data = r.json()
        if data["ok"]:
            for update in data["result"]:
                ultimo_update_id = update["update_id"] + 1
                if "message" in update and "text" in update["message"]:
                    texto = update["message"]["text"]
                    chat_id = update["message"]["chat"]["id"]
                    if texto.startswith("/"):
                        procesar_comando(texto, chat_id)
    except Exception as e:
        print(f"Error getUpdates: {e}")

def procesar_comando(texto, chat_id):
    if texto == "/stats" or texto == "/reporte":
        informe = generar_informe_resultados_como_texto()
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        if informe:
            requests.post(url, data={"chat_id": chat_id, "text": informe}, timeout=10)
        else:
            requests.post(url, data={"chat_id": chat_id, "text": "No hay datos suficientes aún."}, timeout=10)

# =========================
# FUNCIÓN PRINCIPAL EVALUAR
# =========================

def evaluar():
    global zona_actual, zona_consumida, alerted_liquidity, alerted_proximidad, last_event_time, last_mapa_time, regimen_actual, zonas_macro

    ahora = datetime.now(UTC)
    hora_str = ahora.strftime('%H:%M')

    # Obtener datos
    df_1h = obtener_candles_spot(INTERVAL_MACRO, limit=400)   # para tener al menos 336 velas
    df_4h = obtener_candles_spot(INTERVAL_BIAS, limit=100)
    df_entry = obtener_candles_spot(INTERVAL_ENTRY)
    df_oi = obtener_open_interest_hist(period="5m", limit=200)
    precio = obtener_precio_actual()

    if df_entry.empty or precio is None:
        return

    evaluar_eventos_pendientes(precio)

    bias = calcular_bias(df_1h, df_4h, precio)

    # Detectar zonas spot (para radares 0,3,4)
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

    # Actualizar zonas internas (Radar 1 y 2)
    actualizar_zonas_internas(mejor_oi_arriba, mejor_oi_abajo, mejor_spot_arriba, mejor_spot_abajo, precio, hora_str, bias)

    zona_ref_arriba = mejor_oi_arriba if mejor_oi_arriba else mejor_spot_arriba
    zona_ref_abajo = mejor_oi_abajo if mejor_oi_abajo else mejor_spot_abajo
    radar_proximidad_interno(zona_ref_arriba, zona_ref_abajo, precio, hora_str, bias)

    # Zonas para radares de eventos
    zonas_arriba = [z for z in ([mejor_oi_arriba] if mejor_oi_arriba else []) + ([mejor_spot_arriba] if mejor_spot_arriba else []) if z]
    zonas_abajo = [z for z in ([mejor_oi_abajo] if mejor_oi_abajo else []) + ([mejor_spot_abajo] if mejor_spot_abajo else []) if z]

    # Radar 0 (impulso)
    hubo_impulso = radar_impulse(df_entry, precio, zonas_arriba, zonas_abajo, bias)

    # Obtener resultado del último impulso para transición de régimen
    resultado_impulso = None
    if hubo_impulso and historial_eventos:
        ultimo_impulso = next((e for e in reversed(historial_eventos) if e["tipo"] == "impulso" and e.get("evaluado_scalp")), None)
        if ultimo_impulso:
            resultado_impulso = ultimo_impulso.get("resultado_scalp")

    # Actualizar régimen y zonas macro (en 1h con 2 semanas)
    actualizar_regimen(df_1h, hubo_impulso, resultado_impulso)

    # Radar 5 (estructura lenta)
    radar_estructura_lenta(precio, zonas_macro, regimen_actual)

    # Radar 3 y 4 (sweep, breakout)
    radar_sweep(df_entry, zona_ref_arriba, zona_ref_abajo, precio, zonas_arriba, zonas_abajo, bias)
    radar_breakout(df_entry, zona_ref_arriba, zona_ref_abajo, precio, zonas_arriba, zonas_abajo, bias)

    # Alerta de deriva silenciosa
    alerta_deriva_silenciosa(precio, regimen_actual)

    # Limpieza de alerted_proximidad
    keys_a_remover = []
    for key, ts in alerted_proximidad.items():
        if (ahora - ts) > timedelta(hours=2):
            keys_a_remover.append(key)
    for key in keys_a_remover:
        alerted_proximidad.pop(key, None)

    # Sistema
    heartbeat()
    sin_eventos()

# =========================
# BUCLE PRINCIPAL
# =========================

if __name__ == "__main__":
    print("🚀 Iniciando BOT V13.5 (simbología restaurada, volumen como filtro, radar 5 mejorado)...")
    precio_inicial = obtener_precio_actual()
    hora_actual = datetime.now(UTC).strftime('%H:%M')
    msg = f"🤖 BOT DE ARTURO FUNCIONANDO (V13.5)\nHora UTC: {hora_actual}\nPrecio: {fmt(precio_inicial)}"
    enviar(msg)

    last_heartbeat_time = datetime.now(UTC)
    last_event_time = datetime.now(UTC)
    last_mapa_time = None

    # Cargar historial
    if os.path.exists(HISTORIAL_FILE):
        try:
            with open(HISTORIAL_FILE, "r") as f:
                data = json.load(f)
                historial_eventos.extend(data[-2000:])
        except:
            pass

    # Hilo para comandos
    def escuchar_mensajes():
        while True:
            obtener_mensajes()
            time.sleep(1)
    threading.Thread(target=escuchar_mensajes, daemon=True).start()

    while True:
        try:
            evaluar()
        except Exception as e:
            print(f"❌ Error en ciclo principal: {e}")
            enviar(f"⚠️ ERROR: {str(e)[:100]}")
            time.sleep(60)
        time.sleep(60)

        # Guardar historial cada hora
        if datetime.now(UTC).minute == 0:
            try:
                with open(HISTORIAL_FILE, "w") as f:
                    json.dump(list(historial_eventos), f)
            except:
                pass
