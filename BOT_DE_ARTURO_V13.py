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
INTERVAL_MACRO = "1h"          # Para estructura de liquidez y régimen
INTERVAL_ENTRY = "5m"           # Para eventos
INTERVAL_BIAS = "4h"            # Para tendencia de más largo plazo

# Parámetros de liquidez spot (usados internamente)
LOOKBACK = 168
MIN_TOUCHES = 3
CLUSTER_RANGE = 0.0025
PROXIMITY = 0.003
RADAR1_MIN_DIST = 0.01
ZONA_EQUIVALENTE = 0.01

# Parámetros de Open Interest (futuros)
OI_WINDOW = 50
OI_PERCENTILE = 75
OI_LOOKBACK = 3
OI_CLUSTER_RANGE = 0.002
OI_CONFIANZA_ALTA = 500_000_000
OI_CONFIANZA_MEDIA = 200_000_000
OI_CONFIANZA_BAJA = 100_000_000

SPOT_FUTUROS_TOLERANCIA = 0.005

# Radar 0 - Impulso (sin cambios en detección)
IMPULSE_PRICE_CHANGE = 0.65
IMPULSE_COOLDOWN = 300

# Radar 3 - Sweep (umbrales más sensibles)
SWEEP_VOLUME_FACTOR = 1.2        # Bajado de 1.5 para capturar más sweeps
SWEEP_BREAK_MARGIN = 0.0005      # 0.05% de margen (más permisivo)
SWEEP_PESO_MINIMO = 2            # Antes 3, ahora 2 (zonas con menos peso)

# Radar 4 - Breakout (umbrales más sensibles)
BREAKOUT_MARGIN = 0.003
BREAKOUT_RETEST_CANDLES = 1
BREAKOUT_PESO_MINIMO = 2

# Sistema de bias y scoring
EMA_SHORT = 50
EMA_LONG = 200
SCORE_UMBRAL_ACCION_IMPULSO = 4     # Para Radar 0 (bajo)
SCORE_UMBRAL_ACCION_SWEEP = 4       # Para Radar 3 (más sensible)
SCORE_UMBRAL_ACCION_BREAKOUT = 4    # Para Radar 4
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
    "validacion_spot": 2
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

# Sistema
HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6
MAPA_COOLDOWN_MINUTOS = 60
RADAR2_COOLDOWN_MINUTOS = 120
REDONDEO_BASE = 200

# ML Manual
HISTORIAL_FILE = "historial_eventos.json"
EVALUACION_VELAS_SCALP = 3
EVALUACION_VELAS_TEND = 12
RANGO_EXITO_SCALP = 0.004
RANGO_EXITO_TEND = 0.005

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

# Variables para régimen
regimen_actual = "NEUTRAL"
ultimo_cambio_regimen = None
estructura_detected_at = None

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

def calcular_score_evento(evento_tipo, direccion_evento, bias, peso_zona, validacion_spot=False):
    score = peso_zona
    score += PESOS.get(f"evento_{evento_tipo}", 0)
    if validacion_spot:
        score += PESOS["validacion_spot"]
    if bias == "LATERAL":
        if evento_tipo == "sweep":
            score += PESOS["bias_favorable_lateral"]
    elif bias == direccion_evento:
        score += PESOS["bias_favorable_tendencia"]
    return score

def registrar_evento_para_patron(tipo, direccion):
    clave = f"{tipo}_{direccion}"
    ultimos_eventos.append(clave)
    if len(ultimos_eventos) >= 3:
        if all(e == f"sweep_{direccion}" for e in list(ultimos_eventos)[-3:]):
            enviar(f"⚠️ POSIBLE ACUMULACIÓN: 3 sweeps {direccion} consecutivos")

# =========================
# DETECCIÓN DE RÉGIMEN (ESTRUCTURA LENTA)
# =========================

def detectar_estructura(df, ventana=20):
    """
    Analiza pivotes en el timeframe macro para determinar régimen de mercado.
    Retorna: "ACUMULACION", "DISTRIBUCION", "NEUTRAL"
    """
    if df.empty or len(df) < ventana:
        return "NEUTRAL"

    # Calcular máximos y mínimos locales (pivotes)
    highs = df["high"].values
    lows = df["low"].values
    pivot_highs = []
    pivot_lows = []
    for i in range(1, len(df)-1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            pivot_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            pivot_lows.append(lows[i])

    if len(pivot_highs) < 2 and len(pivot_lows) < 2:
        return "NEUTRAL"

    # Evaluar tendencia de pivotes
    tendencia_highs = all(pivot_highs[i] < pivot_highs[i+1] for i in range(len(pivot_highs)-1)) if len(pivot_highs) >= 2 else False
    tendencia_lows = all(pivot_lows[i] < pivot_lows[i+1] for i in range(len(pivot_lows)-1)) if len(pivot_lows) >= 2 else False

    if tendencia_highs and tendencia_lows:
        return "ACUMULACION"
    elif (not tendencia_highs) and (not tendencia_lows):
        return "DISTRIBUCION"
    else:
        return "NEUTRAL"

def actualizar_regimen(df_estructura, hubo_impulso, resultado_impulso=None):
    """
    Actualiza el régimen actual basado en la estructura y el resultado del último impulso.
    """
    global regimen_actual, ultimo_cambio_regimen, estructura_detected_at
    ahora = datetime.now(UTC)

    # Si hubo un impulso, pasamos a modo IMPULSO (independientemente de estructura)
    if hubo_impulso:
        regimen_actual = "IMPULSO"
        ultimo_cambio_regimen = ahora
        return

    # Si estamos en IMPULSO y ha pasado tiempo sin otro impulso, evaluamos si volver a estructura
    if regimen_actual == "IMPULSO" and ultimo_cambio_regimen and (ahora - ultimo_cambio_regimen) > timedelta(minutes=15):
        # Si el último impulso fue un fracaso (resultado_scalp == FRACASO), cambiamos a estructura
        if resultado_impulso == "FRACASO":
            regimen_actual = detectar_estructura(df_estructura)
            ultimo_cambio_regimen = ahora
            estructura_detected_at = ahora
            return
        else:
            # Mantenemos impulso hasta que pase más tiempo
            return

    # Si no estamos en impulso, determinamos régimen por estructura
    if regimen_actual != "IMPULSO":
        estructura = detectar_estructura(df_estructura)
        if estructura != regimen_actual:
            regimen_actual = estructura
            ultimo_cambio_regimen = ahora
            if estructura in ["ACUMULACION", "DISTRIBUCION"]:
                estructura_detected_at = ahora

# =========================
# FUNCIONES PARA OPEN INTEREST
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
# RADARES INTERNOS (1 y 2) - solo alimentan datos
# =========================

def actualizar_zonas_internas(mejor_zona_oi_arriba, mejor_zona_oi_abajo, mejor_zona_spot_arriba, mejor_zona_spot_abajo, precio, hora, bias):
    global ultima_zona_arriba, ultima_zona_abajo
    # Solo actualiza variables internas, no envía mensajes
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
    # Solo actualiza alerted_proximidad para evitar spam en los radares externos
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
# RADAR 0 (IMPULSO) -> Genera setup
# =========================

def generar_setup_impulso(zona, precio_actual, direccion, score_norm, riesgo_sugerido):
    """Genera un setup operativo para impulso."""
    if zona is None:
        return None
    if direccion == "ALCISTA":
        entrada = round(precio_actual, 1)
        # Stop ligeramente por debajo del mínimo de la zona o 0.3% si no hay zona
        stop_loss = round(zona["min"] * 0.997, 1)
        take_profit = round(zona["centro"] * 1.008, 1)  # objetivo moderado
        accion = "COMPRA (LONG)"
    else:
        entrada = round(precio_actual, 1)
        stop_loss = round(zona["max"] * 1.003, 1)
        take_profit = round(zona["centro"] * 0.992, 1)
        accion = "VENTA (SHORT)"
    return {
        "accion": accion,
        "entrada": entrada,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "riesgo": riesgo_sugerido,
        "confianza": "ALTA" if score_norm >= 7 else "MEDIA" if score_norm >= 5 else "BAJA"
    }

def radar_impulse(df_entry, precio_actual, zonas_arriba, zonas_abajo, bias):
    global last_impulse_time, last_event_time, historial_eventos, regimen_actual
    if df_entry.empty or len(df_entry) < 3:
        return

    vela = df_entry.iloc[-1]
    try:
        open_price = float(vela["open"])
        close_price = float(vela["close"])
        volume = float(vela["volume"])
        vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]
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

    # Zona relevante en la dirección
    zona_relevante = None
    if alcista and zonas_arriba:
        zona_relevante = zonas_arriba[0]
    elif not alcista and zonas_abajo:
        zona_relevante = zonas_abajo[0]

    # Calcular score (solo para contexto)
    peso_zona = 0
    validacion_spot = False
    if zona_relevante:
        peso_zona = calcular_peso_zona(zona_relevante if 'toques' in zona_relevante else None,
                                        zona_relevante if 'oi_total' in zona_relevante else None, precio_actual)
        if 'oi_total' in zona_relevante:
            for z in (zonas_arriba + zonas_abajo):
                if 'toques' in z and abs(zona_relevante['centro'] - z['centro']) / zona_relevante['centro'] < SPOT_FUTUROS_TOLERANCIA:
                    validacion_spot = True
                    break

    score_abs = calcular_score_evento("impulso", direccion, bias, peso_zona, validacion_spot)
    score_norm = normalizar_score(score_abs)

    # Registrar evento
    evento = {
        "timestamp": ahora.isoformat(),
        "tipo": "impulso",
        "direccion": direccion,
        "precio": precio_actual,
        "score_abs": score_abs,
        "score_norm": score_norm,
        "volumen": volume,
        "volumen_ratio": volume / vol_medio if vol_medio else 1.0,
        "zona_centro": zona_relevante['centro'] if zona_relevante else None,
        "resultado_scalp": None,
        "resultado_tendencia": None,
        "evaluado_scalp": False,
        "evaluado_tendencia": False,
        "evaluado": False
    }
    historial_eventos.append(evento)

    # Generar setup solo si el score supera el umbral
    if score_norm >= SCORE_UMBRAL_ACCION_IMPULSO:
        # Riesgo según régimen
        riesgo_sugerido = 1.0 if regimen_actual == "IMPULSO" else 0.5
        setup = generar_setup_impulso(zona_relevante, precio_actual, direccion, score_norm, riesgo_sugerido)
        if setup:
            msg = f"🚀 **SETUP {direccion} (IMPULSO)** – Score {score_norm}/10\n\n"
            msg += f"Entrada sugerida: {fmt(setup['entrada'])}\n"
            msg += f"Stop loss: {fmt(setup['stop_loss'])} ({(setup['entrada']-setup['stop_loss'])/setup['entrada']*100:.2f}%)\n"
            msg += f"Take profit: {fmt(setup['take_profit'])} ({(setup['take_profit']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
            msg += f"Risk sugerido: {setup['riesgo']}% de capital\n"
            msg += f"Confianza: {setup['confianza']}\n\n"
            msg += f"Contexto: variación {price_change:.2f}%, volumen {volume:.0f} BTC ({volume/vol_medio:.1f}x media), bias {bias}"
            if zona_relevante:
                msg += f", zona objetivo {fmt(zona_relevante['centro'])} ({distancia(zona_relevante, precio_actual)*100:.1f}%)"
            enviar(msg)
        else:
            # Si no hay zona, al menos informamos del impulso con datos clave
            msg = f"🚀 **IMPULSO {direccion} {emoji}** – Score {score_norm}/10\n\n"
            msg += f"Precio: {fmt(precio_actual)} - Hora UTC: {ahora.strftime('%H:%M')}\n"
            msg += f"Variación: {price_change:.2f}%\n"
            msg += f"Volumen: {volume:.0f} BTC ({volume/vol_medio:.1f}x media)\n"
            msg += f"Bias: {bias}"
            if zona_relevante:
                msg += f"\nZona objetivo: {fmt(zona_relevante['centro'])} ({distancia(zona_relevante, precio_actual)*100:.1f}%)"
            enviar(msg)

    last_impulse_time = ahora
    last_event_time = ahora
    return True  # indica que hubo impulso

# =========================
# RADAR 3 (SWEEP) -> Genera setup de reversión
# =========================

def generar_setup_sweep(zona, precio_actual, direccion_rev, score_norm, riesgo_sugerido):
    if zona is None:
        return None
    if direccion_rev == "ALCISTA":
        entrada = round(precio_actual, 1)
        stop_loss = round(zona["min"] * 0.997, 1)
        take_profit = round(zona["centro"] * 1.008, 1)
        accion = "COMPRA (LONG)"
    else:
        entrada = round(precio_actual, 1)
        stop_loss = round(zona["max"] * 1.003, 1)
        take_profit = round(zona["centro"] * 0.992, 1)
        accion = "VENTA (SHORT)"
    return {
        "accion": accion,
        "entrada": entrada,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "riesgo": riesgo_sugerido,
        "confianza": "ALTA" if score_norm >= 7 else "MEDIA" if score_norm >= 5 else "BAJA"
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

        # Calcular score
        peso_zona = calcular_peso_zona(zona if 'toques' in zona else None,
                                        zona if 'oi_total' in zona else None, precio_actual)
        validacion_spot = False
        if 'oi_total' in zona:
            for z in (zonas_arriba + zonas_abajo):
                if 'toques' in z and abs(zona['centro'] - z['centro']) / zona['centro'] < SPOT_FUTUROS_TOLERANCIA:
                    validacion_spot = True
                    break
        score_abs = calcular_score_evento("sweep", direccion_rev, bias, peso_zona, validacion_spot)
        score_norm = normalizar_score(score_abs)

        if score_norm < SCORE_UMBRAL_ACCION_SWEEP:
            sweep_pendiente = None
            return

        registrar_evento_para_patron("sweep", direccion_rev)
        ahora = datetime.now(UTC)
        color_zona = "🟢" if tipo_sweep == "HIGH" else "🔴"
        evento = {
            "timestamp": ahora.isoformat(),
            "tipo": "sweep",
            "direccion": direccion_rev,
            "precio": precio_actual,
            "score_abs": score_abs,
            "score_norm": score_norm,
            "zona_centro": zona['centro'],
            "resultado_scalp": None,
            "resultado_tendencia": None,
            "evaluado_scalp": False,
            "evaluado_tendencia": False,
            "evaluado": False
        }
        historial_eventos.append(evento)

        # Setup con riesgo según régimen
        riesgo_sugerido = 1.0 if regimen_actual == "IMPULSO" else 0.5
        setup = generar_setup_sweep(zona, precio_actual, direccion_rev, score_norm, riesgo_sugerido)
        if setup:
            msg = f"🔄 **SETUP REVERSIÓN {direccion_rev} (SWEEP)** – Score {score_norm}/10\n\n"
            msg += f"Entrada sugerida: {fmt(setup['entrada'])}\n"
            msg += f"Stop loss: {fmt(setup['stop_loss'])} ({(setup['entrada']-setup['stop_loss'])/setup['entrada']*100:.2f}%)\n"
            msg += f"Take profit: {fmt(setup['take_profit'])} ({(setup['take_profit']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
            msg += f"Risk sugerido: {setup['riesgo']}% de capital\n"
            msg += f"Confianza: {setup['confianza']}\n\n"
            msg += f"Zona barrida: {fmt(zona['centro'])} | Precio actual: {fmt(precio_actual)} | Hora: {ahora.strftime('%H:%M')}\n"
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
# RADAR 4 (BREAKOUT) -> Genera setup de continuación
# =========================

def generar_setup_breakout(zona, precio_actual, direccion, score_norm, riesgo_sugerido):
    if zona is None:
        return None
    if direccion == "ALCISTA":
        entrada = round(precio_actual, 1)
        stop_loss = round(zona["min"] * 0.997, 1)
        take_profit = round(zona["centro"] * 1.015, 1)  # objetivo más amplio
        accion = "COMPRA (LONG)"
    else:
        entrada = round(precio_actual, 1)
        stop_loss = round(zona["max"] * 1.003, 1)
        take_profit = round(zona["centro"] * 0.985, 1)
        accion = "VENTA (SHORT)"
    return {
        "accion": accion,
        "entrada": entrada,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "riesgo": riesgo_sugerido,
        "confianza": "ALTA" if score_norm >= 7 else "MEDIA" if score_norm >= 5 else "BAJA"
    }

def radar_breakout(df_entry, mejor_zona_arriba, mejor_zona_abajo, precio_actual, zonas_arriba, zonas_abajo, bias):
    global zona_consumida, last_event_time, alerted_liquidity, historial_eventos, regimen_actual
    if df_entry.empty or len(df_entry) < BREAKOUT_RETEST_CANDLES + 1:
        return
    vela = df_entry.iloc[-1]
    close = vela["close"]
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
                    score_abs = calcular_score_evento("breakout", "ALCISTA", bias, peso_zona, validacion_spot)
                    score_norm = normalizar_score(score_abs)
                    if score_norm >= SCORE_UMBRAL_ACCION_BREAKOUT:
                        alerted_liquidity.add(key)
                        zona_consumida = True
                        evento = {
                            "timestamp": ahora.isoformat(),
                            "tipo": "breakout",
                            "direccion": "ALCISTA",
                            "precio": close,
                            "score_abs": score_abs,
                            "score_norm": score_norm,
                            "zona_centro": mejor_zona_arriba['centro'],
                            "resultado_scalp": None,
                            "resultado_tendencia": None,
                            "evaluado_scalp": False,
                            "evaluado_tendencia": False,
                            "evaluado": False
                        }
                        historial_eventos.append(evento)
                        registrar_evento_para_patron("breakout", "ALCISTA")
                        riesgo_sugerido = 1.0 if regimen_actual == "IMPULSO" else 0.5
                        setup = generar_setup_breakout(mejor_zona_arriba, close, "ALCISTA", score_norm, riesgo_sugerido)
                        if setup:
                            msg = f"🌋 **SETUP {setup['accion']} (BREAKOUT ALCISTA)** – Score {score_norm}/10\n\n"
                            msg += f"Entrada sugerida: {fmt(setup['entrada'])}\n"
                            msg += f"Stop loss: {fmt(setup['stop_loss'])} ({(setup['entrada']-setup['stop_loss'])/setup['entrada']*100:.2f}%)\n"
                            msg += f"Take profit: {fmt(setup['take_profit'])} ({(setup['take_profit']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
                            msg += f"Risk sugerido: {setup['riesgo']}% de capital\n"
                            msg += f"Confianza: {setup['confianza']}\n\n"
                            msg += f"Zona rota: {fmt(mejor_zona_arriba['centro'])} | Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}\n"
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
                    score_abs = calcular_score_evento("breakout", "BAJISTA", bias, peso_zona, validacion_spot)
                    score_norm = normalizar_score(score_abs)
                    if score_norm >= SCORE_UMBRAL_ACCION_BREAKOUT:
                        alerted_liquidity.add(key)
                        zona_consumida = True
                        evento = {
                            "timestamp": ahora.isoformat(),
                            "tipo": "breakout",
                            "direccion": "BAJISTA",
                            "precio": close,
                            "score_abs": score_abs,
                            "score_norm": score_norm,
                            "zona_centro": mejor_zona_abajo['centro'],
                            "resultado_scalp": None,
                            "resultado_tendencia": None,
                            "evaluado_scalp": False,
                            "evaluado_tendencia": False,
                            "evaluado": False
                        }
                        historial_eventos.append(evento)
                        registrar_evento_para_patron("breakout", "BAJISTA")
                        riesgo_sugerido = 1.0 if regimen_actual == "IMPULSO" else 0.5
                        setup = generar_setup_breakout(mejor_zona_abajo, close, "BAJISTA", score_norm, riesgo_sugerido)
                        if setup:
                            msg = f"🌋 **SETUP {setup['accion']} (BREAKOUT BAJISTA)** – Score {score_norm}/10\n\n"
                            msg += f"Entrada sugerida: {fmt(setup['entrada'])}\n"
                            msg += f"Stop loss: {fmt(setup['stop_loss'])} ({(setup['stop_loss']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
                            msg += f"Take profit: {fmt(setup['take_profit'])} ({(setup['entrada']-setup['take_profit'])/setup['entrada']*100:.2f}%)\n"
                            msg += f"Risk sugerido: {setup['riesgo']}% de capital\n"
                            msg += f"Confianza: {setup['confianza']}\n\n"
                            msg += f"Zona rota: {fmt(mejor_zona_abajo['centro'])} | Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}\n"
                            msg += f"Bias: {bias}"
                            enviar(msg)
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
        msg = f"🤖 BOT DE ARTURO FUNCIONANDO (V13)\nHora UTC: {ahora.strftime('%H:%M')}\nPrecio: {fmt(precio)}\nRégimen actual: {regimen_actual}"
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
# EVALUACIÓN DE RESULTADOS
# =========================

def evaluar_eventos_pendientes(precio_actual):
    for evento in list(historial_eventos):
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

        if not evento.get("evaluado_tendencia", False):
            ts_evento = datetime.fromisoformat(evento["timestamp"])
            if (datetime.now(UTC) - ts_evento) > timedelta(minutes=EVALUACION_VELAS_TEND * 5):
                precio_inicial = evento["precio"]
                direccion = evento["direccion"]
                if direccion == "ALCISTA":
                    if precio_actual >= precio_inicial * (1 + RANGO_EXITO_TEND):
                        evento["resultado_tendencia"] = "EXITO"
                    elif precio_actual <= precio_inicial * (1 - RANGO_EXITO_TEND):
                        evento["resultado_tendencia"] = "FRACASO"
                    else:
                        evento["resultado_tendencia"] = "NEUTRO"
                else:
                    if precio_actual <= precio_inicial * (1 - RANGO_EXITO_TEND):
                        evento["resultado_tendencia"] = "EXITO"
                    elif precio_actual >= precio_inicial * (1 + RANGO_EXITO_TEND):
                        evento["resultado_tendencia"] = "FRACASO"
                    else:
                        evento["resultado_tendencia"] = "NEUTRO"
                evento["evaluado_tendencia"] = True

        if evento.get("evaluado_scalp", False) and evento.get("evaluado_tendencia", False):
            evento["evaluado"] = True

def generar_informe_resultados_como_texto():
    if not historial_eventos:
        return None
    df = pd.DataFrame(list(historial_eventos))

    df_scalp = df[df["evaluado_scalp"] == True]
    df_tend = df[df["evaluado_tendencia"] == True]

    if df_scalp.empty and df_tend.empty:
        return None

    informe = "📊 **INFORME DE RESULTADOS (V13)**\n\n"

    for tipo in df["tipo"].unique():
        informe += f"🔹 {tipo.upper()}\n"

        # Scalping (3v)
        subset = df_scalp[df_scalp["tipo"] == tipo]
        if not subset.empty:
            total = len(subset)
            exitos = len(subset[subset["resultado_scalp"] == "EXITO"])
            fracasos = len(subset[subset["resultado_scalp"] == "FRACASO"])
            neutros = len(subset[subset["resultado_scalp"] == "NEUTRO"])
            tasa = exitos / total * 100 if total else 0
            informe += f"   ⏱️ Scalping (3v): {total} ev | Éxito: {exitos} ({tasa:.1f}%) | Fracaso: {fracasos} | Neutro: {neutros}\n"
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

        # Tendencia (12v)
        subset = df_tend[df_tend["tipo"] == tipo]
        if not subset.empty:
            total = len(subset)
            exitos = len(subset[subset["resultado_tendencia"] == "EXITO"])
            fracasos = len(subset[subset["resultado_tendencia"] == "FRACASO"])
            neutros = len(subset[subset["resultado_tendencia"] == "NEUTRO"])
            tasa = exitos / total * 100 if total else 0
            informe += f"   📈 Tendencia (12v): {total} ev | Éxito: {exitos} ({tasa:.1f}%) | Fracaso: {fracasos} | Neutro: {neutros}\n"

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
    global zona_actual, zona_consumida, alerted_liquidity, alerted_proximidad, last_event_time, last_mapa_time, regimen_actual

    ahora = datetime.now(UTC)
    hora_str = ahora.strftime('%H:%M')

    df_1h = obtener_candles_spot(INTERVAL_MACRO)
    df_4h = obtener_candles_spot(INTERVAL_BIAS)
    df_entry = obtener_candles_spot(INTERVAL_ENTRY)
    df_oi = obtener_open_interest_hist(period="5m", limit=200)
    precio = obtener_precio_actual()

    if df_entry.empty or precio is None:
        return

    evaluar_eventos_pendientes(precio)

    bias = calcular_bias(df_1h, df_4h, precio)

    # Detectar zonas (internas)
    zonas_high_spot, zonas_low_spot = detectar_zonas_spot(df_1h) if not df_1h.empty else ([], [])
    mejor_spot_arriba, mejor_spot_abajo = seleccionar_mejores_zonas_spot(zonas_high_spot, zonas_low_spot, precio)

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

    # Actualizar zonas internas (Radar 1 silencioso)
    actualizar_zonas_internas(mejor_oi_arriba, mejor_oi_abajo, mejor_spot_arriba, mejor_spot_abajo, precio, hora_str, bias)

    # Radar 2 silencioso (solo cooldown)
    zona_ref_arriba = mejor_oi_arriba if mejor_oi_arriba else mejor_spot_arriba
    zona_ref_abajo = mejor_oi_abajo if mejor_oi_abajo else mejor_spot_abajo
    radar_proximidad_interno(zona_ref_arriba, zona_ref_abajo, precio, hora_str, bias)

    # Zonas completas para radares
    zonas_arriba = [z for z in ([mejor_oi_arriba] if mejor_oi_arriba else []) + ([mejor_spot_arriba] if mejor_spot_arriba else []) if z]
    zonas_abajo = [z for z in ([mejor_oi_abajo] if mejor_oi_abajo else []) + ([mejor_spot_abajo] if mejor_spot_abajo else []) if z]

    # Ejecutar Radar 0 y obtener si hubo impulso
    hubo_impulso = radar_impulse(df_entry, precio, zonas_arriba, zonas_abajo, bias)

    # Detectar resultado del último impulso (para transición)
    resultado_impulso = None
    if hubo_impulso and historial_eventos:
        ultimo_impulso = next((e for e in reversed(historial_eventos) if e["tipo"] == "impulso" and e.get("evaluado_scalp")), None)
        if ultimo_impulso:
            resultado_impulso = ultimo_impulso.get("resultado_scalp")

    # Actualizar régimen
    actualizar_regimen(df_1h, hubo_impulso, resultado_impulso)

    # Ejecutar Radars 3 y 4 (generan setups si aplica)
    radar_sweep(df_entry, zona_ref_arriba, zona_ref_abajo, precio, zonas_arriba, zonas_abajo, bias)
    radar_breakout(df_entry, zona_ref_arriba, zona_ref_abajo, precio, zonas_arriba, zonas_abajo, bias)

    # Limpieza de alerted_proximidad (más de 2h)
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
    print("🚀 Iniciando BOT V13 (con régimen y setups operativos)...")
    precio_inicial = obtener_precio_actual()
    hora_actual = datetime.now(UTC).strftime('%H:%M')
    msg = f"🤖 BOT DE ARTURO FUNCIONANDO (V13)\nHora UTC: {hora_actual}\nPrecio: {fmt(precio_inicial)}"
    enviar(msg)

    last_heartbeat_time = datetime.now(UTC)
    last_event_time = datetime.now(UTC)
    last_mapa_time = None

    if os.path.exists(HISTORIAL_FILE):
        try:
            with open(HISTORIAL_FILE, "r") as f:
                data = json.load(f)
                historial_eventos.extend(data[-2000:])
        except:
            pass

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

        if datetime.now(UTC).minute == 0:
            try:
                with open(HISTORIAL_FILE, "w") as f:
                    json.dump(list(historial_eventos), f)
            except:
                pass
