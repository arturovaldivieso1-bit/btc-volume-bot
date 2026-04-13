# -*- coding: utf-8 -*-
# BOT V13.13 – Pre‑impulso score + alerta separada + heartbeat horario Chile (UTC-4)
#   - Umbral impulso reducido a 0.5%
#   - Alerta cuando pre_score >= 6 (cooldown 30 min)
#   - Heartbeat solo a las 9:00 y 16:00 Chile (UTC-4)
#   - Todo lo demás igual que V13.12

import requests
import pandas as pd
import time
import os
import numpy as np
import atexit
from datetime import datetime, timedelta, UTC
from collections import deque
import json
import threading
from concurrent.futures import ThreadPoolExecutor

# =========================
# CONFIGURACIÓN INICIAL
# =========================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOL = "BTCUSDT"

INTERVAL_MACRO = "1h"
INTERVAL_ENTRY = "5m"
INTERVAL_BIAS  = "4h"

LOOKBACK       = 168
MIN_TOUCHES    = 3
CLUSTER_RANGE  = 0.0025
PROXIMITY      = 0.003
RADAR1_MIN_DIST = 0.01
ZONA_EQUIVALENTE = 0.01

OI_WINDOW         = 50
OI_PERCENTILE     = 75
OI_LOOKBACK       = 3
OI_CLUSTER_RANGE  = 0.002
OI_CONFIANZA_ALTA  = 500_000_000
OI_CONFIANZA_MEDIA = 200_000_000
OI_CONFIANZA_BAJA  = 100_000_000

SPOT_FUTUROS_TOLERANCIA = 0.005

# Umbral de impulso reducido a 0.5% (antes 0.65)
IMPULSE_PRICE_CHANGE = 0.5
IMPULSE_COOLDOWN     = 300

SWEEP_VOLUME_FACTOR   = 1.2
SWEEP_BREAK_MARGIN    = 0.0005
SWEEP_PESO_MINIMO     = 2
BREAKOUT_MARGIN       = 0.003
BREAKOUT_RETEST_CANDLES = 1
BREAKOUT_PESO_MINIMO  = 2
VOL_MIN_SWEEP         = 400
SCORE_UMBRAL_SWEEP    = 7

VOL1_MED = 400;  VOL1_P75 = 692;  VOL1_P90 = 1134
PROB1_MED = 68;  PROB1_P75 = 82;  PROB1_P90 = 85

EMA_SHORT = 50
EMA_LONG  = 200
SCORE_UMBRAL_ACCION_IMPULSO = 3
SCORE_MAX_TEORICO           = 30

PESOS = {
    "zona_spot": 1, "zona_oi": 2, "toques_altos": 1, "cercania": 1,
    "bias_favorable_tendencia": 3, "bias_favorable_lateral": 2,
    "evento_impulso": 1, "evento_sweep": 2, "evento_breakout": 2,
    "validacion_spot": 2, "volumen_alto": 3
}

NO_EVENT_HOURS         = 6
MAPA_COOLDOWN_MINUTOS  = 60
RADAR2_COOLDOWN_MINUTOS = 120
REDONDEO_BASE          = 200

EVALUACION_VELAS_SCALP = 2
EVALUACION_VELAS_TEND  = 6
EVALUACION_VELAS_LARGA = 12
RANGO_EXITO_SCALP      = 0.003
RANGO_EXITO_TEND       = 0.005
RANGO_EXITO_LARGA      = 0.01

HISTORIAL_FILE = "historial_eventos.json"

ZONA_MACRO_ALERTA_DIST    = 0.01
ZONA_MACRO_SETUP_DIST     = 0.003
ZONA_MACRO_COOLDOWN_HORAS = 2

ALERTA_DERIVA_HORAS      = 1
ALERTA_DERIVA_PORCENTAJE = 0.65

# Parámetros para pre‑impulso score
COMPRESION_LOOKBACK = 10      # velas de 5m = 50 minutos
COMPRESION_FACTOR = 0.6       # rango actual < 60% de la media
VOL_RATIO_ALTO = 1.3          # para sumar 2 puntos
VOL_RATIO_MEDIO = 1.1         # para sumar 1 punto
BUILDUP_MIN_TOQUES = 4
TIEMPO_SIN_IMPULSO_MIN = 30   # minutos
PRE_SCORE_ALERTA_UMBRAL = 6   # enviar alerta separada cuando score >= 6
PRE_SCORE_ALERTA_COOLDOWN = 30 # minutos

# =========================
# ESTADO GLOBAL
# =========================

last_impulse_time  = None
last_heartbeat_time = None
last_pre_alert_time = None   # para cooldown de alerta de pre‑impulso
last_event_time    = None
last_mapa_time     = None
zona_actual        = None
zona_consumida     = False
alerted_liquidity_ts = {}
alerted_proximidad   = {}
sweep_pendiente      = None
ultima_zona_arriba   = None
ultima_zona_abajo    = None
oi_increment_history = []
historial_eventos = deque(maxlen=2000)
ultimos_eventos   = deque(maxlen=10)
regimen_actual        = "NEUTRAL"
ultimo_cambio_regimen = None
estructura_detected_at = None
zonas_macro           = []
ultima_deriva_time   = None
ultimo_precio_deriva = None
alerted_macro = {}
data_lock = threading.RLock()
ultimo_guardado           = datetime.now(UTC)
ultima_limpieza_liquidity = datetime.now(UTC)

_executor = ThreadPoolExecutor(max_workers=5)
def _cerrar_executor():
    print("🛑 Cerrando ThreadPoolExecutor...")
    _executor.shutdown(wait=False)
atexit.register(_cerrar_executor)

# =========================
# FUNCIONES AUXILIARES
# =========================

def peso_por_oi(oi_total):
    if oi_total > 500_000_000: return 4
    elif oi_total > 200_000_000: return 3
    elif oi_total > 100_000_000: return 2
    else: return 1

def bonificacion_volumen(volumen):
    if volumen > VOL1_P90: return 5
    elif volumen > VOL1_P75: return 3
    elif volumen > VOL1_MED: return 1
    else: return 0

def enviar(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Error Telegram: {e}")

def fmt(n):
    if n is None:
        return "N/D"
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
        print(f"Error Binance spot {interval}: {e}")
        return pd.DataFrame()

def obtener_precio_actual():
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
        r = requests.get(url, timeout=10)
        return float(r.json()["price"])
    except:
        return None

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

def obtener_todos_los_datos():
    future_1h     = _executor.submit(obtener_candles_spot, INTERVAL_MACRO, 400)
    future_4h     = _executor.submit(obtener_candles_spot, INTERVAL_BIAS, 100)
    future_5m     = _executor.submit(obtener_candles_spot, INTERVAL_ENTRY)
    future_oi     = _executor.submit(obtener_open_interest_hist, "5m", 200)
    future_precio = _executor.submit(obtener_precio_actual)
    return (
        future_1h.result(), future_4h.result(), future_5m.result(),
        future_oi.result(), future_precio.result()
    )

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
    lows  = df["low"].tail(lookback).tolist()
    zonas_high = [
        {"tipo":"HIGH","centro":c["centro"],"max":max(c["valores"]),"min":min(c["valores"]),"toques":len(c["valores"])}
        for c in cluster_precios(highs, rango) if len(c["valores"]) >= min_toques
    ]
    zonas_low = [
        {"tipo":"LOW","centro":c["centro"],"max":max(c["valores"]),"min":min(c["valores"]),"toques":len(c["valores"])}
        for c in cluster_precios(lows, rango) if len(c["valores"]) >= min_toques
    ]
    zonas_high.sort(key=lambda x: x["toques"], reverse=True)
    zonas_low.sort(key=lambda x: x["toques"],  reverse=True)
    return zonas_high, zonas_low

def redondear_centro(centro, base=REDONDEO_BASE):
    return round(centro / base) * base

def seleccionar_mejores_zonas_spot(zonas_high, zonas_low, precio):
    arriba = [z for z in zonas_high + zonas_low if z["centro"] > precio]
    abajo  = [z for z in zonas_low + zonas_high if z["centro"] < precio]
    for z in arriba:
        z["distancia"] = z["centro"] - precio
        z["score"]     = z["toques"] * 1000 - z["distancia"]
        z["centro_rd"] = redondear_centro(z["centro"])
    for z in abajo:
        z["distancia"] = precio - z["centro"]
        z["score"]     = z["toques"] * 1000 - z["distancia"]
        z["centro_rd"] = redondear_centro(z["centro"])
    arriba.sort(key=lambda x: x["score"], reverse=True)
    abajo.sort(key=lambda x: x["score"],  reverse=True)
    return (arriba[0] if arriba else None), (abajo[0] if abajo else None)

def distancia(zona, precio):
    return abs(zona["centro"] - precio) / precio

def calcular_bias(df_1h, df_4h, precio_actual):
    if df_1h.empty or df_4h.empty:
        return "LATERAL"
    ema_s1 = df_1h["close"].ewm(span=EMA_SHORT, adjust=False).mean().iloc[-1]
    ema_l1 = df_1h["close"].ewm(span=EMA_LONG,  adjust=False).mean().iloc[-1]
    ema_s4 = df_4h["close"].ewm(span=EMA_SHORT, adjust=False).mean().iloc[-1]
    ema_l4 = df_4h["close"].ewm(span=EMA_LONG,  adjust=False).mean().iloc[-1]
    p1 = df_1h["close"].iloc[-1] - df_1h["close"].iloc[-2]
    p4 = df_4h["close"].iloc[-1] - df_4h["close"].iloc[-2]
    alcista = (precio_actual > ema_s1 and ema_s1 > ema_l1 and p1 > 0) or \
              (precio_actual > ema_s4 and ema_s4 > ema_l4 and p4 > 0)
    bajista = (precio_actual < ema_s1 and ema_s1 < ema_l1 and p1 < 0) or \
              (precio_actual < ema_s4 and ema_s4 < ema_l4 and p4 < 0)
    if alcista and not bajista:   return "ALCISTA"
    elif bajista and not alcista: return "BAJISTA"
    else:                         return "LATERAL"

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
    score = peso_zona + PESOS.get(f"evento_{evento_tipo}", 0)
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

def obtener_probabilidad(volumen):
    if volumen > VOL1_P90:   return PROB1_P90
    elif volumen > VOL1_P75: return PROB1_P75
    elif volumen > VOL1_MED: return PROB1_MED
    else:                    return None

def registrar_evento_para_patron(tipo, direccion):
    clave = f"{tipo}_{direccion}"
    with data_lock:
        ultimos_eventos.append(clave)
        snapshot = list(ultimos_eventos)
    if len(snapshot) >= 3 and all(e == f"sweep_{direccion}" for e in snapshot[-3:]):
        enviar(f"⚠️ POSIBLE ACUMULACIÓN: 3 sweeps {direccion} consecutivos")

# =========================
# FUNCIONES PARA PRE‑IMPULSO SCORE
# =========================

def detectar_compresion(df_entry, lookback=COMPRESION_LOOKBACK, factor=COMPRESION_FACTOR):
    """Detecta si el rango (high-low) actual es inferior al factor * media de lookback velas."""
    if df_entry.empty or len(df_entry) < lookback:
        return False
    rangos = (df_entry["high"] - df_entry["low"]).tail(lookback)
    media_rango = rangos.mean()
    if media_rango == 0:
        return False
    rango_actual = df_entry["high"].iloc[-1] - df_entry["low"].iloc[-1]
    return rango_actual < media_rango * factor

def detectar_buildup(zona, df_oi, min_toques=BUILDUP_MIN_TOQUES):
    """Detecta acumulación de liquidez: zona con toques >= min_toques y OI creciente en últimas 3 velas."""
    if zona is None or df_oi.empty or len(df_oi) < 3:
        return False
    if zona.get("toques", 0) < min_toques:
        return False
    oi_vals = df_oi["sumOpenInterestValue"].tail(3).tolist()
    return all(oi_vals[i] < oi_vals[i+1] for i in range(len(oi_vals)-1))

def detectar_patron_sweeps(ultimos_eventos):
    """Detecta 2 sweeps consecutivos en la misma dirección."""
    if len(ultimos_eventos) < 2:
        return False
    ultimos = list(ultimos_eventos)[-2:]
    return ultimos[0] == ultimos[1] and "sweep" in ultimos[0]

def tiempo_sin_impulso(last_impulse_time, ahora, minutos=TIEMPO_SIN_IMPULSO_MIN):
    if last_impulse_time is None:
        return False
    return (ahora - last_impulse_time).total_seconds() > minutos * 60

def calcular_pre_impulso_score(df_entry, df_oi, zona_referencia, ultimos_eventos, last_impulse_time, ahora):
    score = 0
    if detectar_compresion(df_entry):
        score += 2
    if len(df_entry) >= 20:
        vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]
        vol_actual = df_entry["volume"].iloc[-1]
        if vol_medio > 0:
            ratio = vol_actual / vol_medio
            if ratio > VOL_RATIO_ALTO:
                score += 2
            elif ratio > VOL_RATIO_MEDIO:
                score += 1
    if detectar_buildup(zona_referencia, df_oi):
        score += 2
    if detectar_patron_sweeps(ultimos_eventos):
        score += 2
    if tiempo_sin_impulso(last_impulse_time, ahora):
        score += 1
    return min(score, 10)

# =========================
# RÉGIMEN Y ZONAS MACRO (50 velas 1h)
# =========================

def detectar_estructura_y_zonas(df_1h):
    VELAS_MACRO = 50
    if df_1h.empty or len(df_1h) < VELAS_MACRO:
        if not hasattr(detectar_estructura_y_zonas, "warning_sent"):
            print(f"⚠️ Menos de {VELAS_MACRO} velas en df_1h, régimen NEUTRAL temporal")
            enviar(f"⚠️ Advertencia: menos de {VELAS_MACRO} velas 1h disponibles. Régimen = NEUTRAL.")
            detectar_estructura_y_zonas.warning_sent = True
        return "NEUTRAL", []

    highs = df_1h["high"].values[-VELAS_MACRO:]
    lows  = df_1h["low"].values[-VELAS_MACRO:]
    pivot_highs, pivot_lows = [], []
    for i in range(1, len(highs)-1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            pivot_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            pivot_lows.append(lows[i])

    if len(pivot_highs) < 2 and len(pivot_lows) < 2:
        regimen = "NEUTRAL"
    else:
        t_h = all(pivot_highs[i] < pivot_highs[i+1] for i in range(len(pivot_highs)-1)) if len(pivot_highs) >= 2 else False
        t_l = all(pivot_lows[i]  < pivot_lows[i+1]  for i in range(len(pivot_lows)-1))  if len(pivot_lows)  >= 2 else False
        if t_h and t_l:           regimen = "ACUMULACION"
        elif not t_h and not t_l: regimen = "DISTRIBUCION"
        else:                     regimen = "NEUTRAL"

    zh, zl = detectar_zonas_spot(df_1h, lookback=VELAS_MACRO, min_toques=2, rango=0.005)
    zm = sorted(zh + zl, key=lambda x: x["toques"], reverse=True)
    return regimen, zm[:5]

def actualizar_regimen(df_1h, hubo_impulso, resultado_impulso=None):
    global regimen_actual, ultimo_cambio_regimen, estructura_detected_at, zonas_macro
    ahora = datetime.now(UTC)
    if hubo_impulso:
        regimen_actual = "IMPULSO"
        ultimo_cambio_regimen = ahora
        return
    if regimen_actual == "IMPULSO" and ultimo_cambio_regimen and \
       (ahora - ultimo_cambio_regimen) > timedelta(minutes=15):
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

def cluster_oi_por_precio(eventos_oi):
    clusters = []
    for ev in sorted(eventos_oi, key=lambda x: x["precio"]):
        agregado = False
        for c in clusters:
            if abs(ev["precio"] - c["centro"]) / ev["precio"] < OI_CLUSTER_RANGE:
                c["valores"].append(ev["precio"])
                c["centro"]    = sum(c["valores"]) / len(c["valores"])
                c["oi_total"] += ev["oi_incremento"]
                c["min"]       = min(c["min"], ev["precio"])
                c["max"]       = max(c["max"], ev["precio"])
                agregado = True
                break
        if not agregado:
            clusters.append({
                "centro": ev["precio"], "valores": [ev["precio"]],
                "oi_total": ev["oi_incremento"],
                "min": ev["precio"], "max": ev["precio"]
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

    oi_vals     = df_oi["sumOpenInterestValue"].astype(float)
    incrementos = oi_vals.diff().clip(lower=0).fillna(0)
    oi_increment_history.extend(incrementos.tolist())
    if len(oi_increment_history) > OI_WINDOW:
        oi_increment_history[:] = oi_increment_history[-OI_WINDOW:]

    umbral = np.percentile(oi_increment_history, OI_PERCENTILE) \
             if len(oi_increment_history) >= 10 else 20_000_000
    suma_rolling = incrementos.rolling(window=OI_LOOKBACK).sum()

    if not df_spot.empty and 'time' in df_spot.columns:
        df_spot = df_spot.copy()
        df_spot['time_dt'] = pd.to_datetime(df_spot['time'], unit='ms')

    eventos = []
    for i in range(OI_LOOKBACK - 1, len(df_oi)):
        if suma_rolling.iloc[i] > umbral:
            ts = df_oi.iloc[i]["timestamp"]
            precio_zona = None
            if not df_spot.empty:
                idx = (df_spot['time_dt'] - ts).abs().idxmin()
                precio_zona = float(df_spot.loc[idx, 'close'])
            if precio_zona:
                eventos.append({"precio": precio_zona,
                                 "oi_incremento": suma_rolling.iloc[i],
                                 "timestamp": ts})
    if not eventos:
        return []

    clusters = cluster_oi_por_precio(eventos)
    for c in clusters:
        if c["oi_total"] > OI_CONFIANZA_ALTA:    c["confianza"] = "🔥🔥🔥"
        elif c["oi_total"] > OI_CONFIANZA_MEDIA:  c["confianza"] = "🔥🔥"
        else:                                      c["confianza"] = "🔥"
    return clusters

# =========================
# RADARES INTERNOS (1 y 2)
# =========================

def actualizar_zonas_internas(mejor_zona_oi_arriba, mejor_zona_oi_abajo,
                               mejor_zona_spot_arriba, mejor_zona_spot_abajo, precio, hora, bias):
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
        global last_event_time
        if zona is None:
            return
        centro_rd = redondear_centro(zona["centro"])
        key  = (centro_rd, tipo)
        dist = distancia(zona, precio)
        if PROXIMITY <= dist < RADAR1_MIN_DIST:
            with data_lock:
                ultimo = alerted_proximidad.get(key)
                if ultimo is None or (ahora - ultimo) > timedelta(minutes=RADAR2_COOLDOWN_MINUTOS):
                    alerted_proximidad[key] = ahora
                    last_event_time = ahora

    if mejor_zona_arriba: check_and_record(mejor_zona_arriba, "HIGH")
    if mejor_zona_abajo:  check_and_record(mejor_zona_abajo,  "LOW")

# =========================
# RADAR 0 – IMPULSO (con pre‑impulso score y umbral 0.5%)
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
    return {"accion": accion, "entrada": entrada, "sl": sl, "tp": tp, "confianza": confianza}

def radar_impulse(df_entry, precio_actual, zonas_arriba, zonas_abajo, bias, pre_score=0):
    global last_impulse_time, last_event_time, regimen_actual
    if df_entry.empty or len(df_entry) < 3:
        return False

    vela = df_entry.iloc[-1]
    try:
        open_price  = float(vela["open"])
        close_price = float(vela["close"])
        high_price  = float(vela["high"])
        low_price   = float(vela["low"])
        volume      = float(vela["volume"])
        vol_medio   = df_entry["volume"].rolling(20).mean().iloc[-1]
    except:
        return False

    high_change = (high_price - open_price) / open_price * 100
    low_change  = (open_price - low_price)  / open_price * 100
    price_change = max(high_change, low_change)

    if high_change >= low_change:
        direccion = "ALCISTA"
        emoji     = "🟢"
    else:
        direccion = "BAJISTA"
        emoji     = "🔴"

    cierre_confirmado = (direccion == "ALCISTA" and close_price > open_price) or \
                        (direccion == "BAJISTA" and close_price < open_price)

    if price_change < IMPULSE_PRICE_CHANGE:
        return False

    ahora = datetime.now(UTC)
    if last_impulse_time and (ahora - last_impulse_time).seconds < IMPULSE_COOLDOWN:
        return False

    zona_relevante = None
    if direccion == "ALCISTA" and zonas_arriba:
        zona_relevante = zonas_arriba[0]
    elif direccion == "BAJISTA" and zonas_abajo:
        zona_relevante = zonas_abajo[0]

    prob      = obtener_probabilidad(volume)
    prob_text = f"📊 Probabilidad: {prob}%" if prob else ""

    evento = {
        "timestamp": ahora.isoformat(), "tipo": "impulso", "direccion": direccion,
        "precio": precio_actual, "score_abs": 0, "score_norm": 0,
        "volumen": volume, "volumen_ratio": volume / vol_medio if vol_medio else 1.0,
        "zona_centro": zona_relevante['centro'] if zona_relevante else None,
        "resultado_scalp": None, "resultado_tend": None, "resultado_largo": None,
        "evaluado_scalp": False, "evaluado_tend": False, "evaluado_largo": False, "evaluado": False
    }
    with data_lock:
        historial_eventos.append(evento)

    score_norm = 0
    setup      = None
    if zona_relevante:
        peso_zona = calcular_peso_zona(
            zona_relevante if 'toques'   in zona_relevante else None,
            zona_relevante if 'oi_total' in zona_relevante else None,
            precio_actual
        )
        validacion_spot = False
        if 'oi_total' in zona_relevante:
            for z in zonas_arriba + zonas_abajo:
                if 'toques' in z and \
                   abs(zona_relevante['centro'] - z['centro']) / zona_relevante['centro'] < SPOT_FUTUROS_TOLERANCIA:
                    validacion_spot = True
                    break
        score_abs  = calcular_score_evento("impulso", direccion, bias, peso_zona, validacion_spot, volume)
        score_norm = normalizar_score(score_abs)
        evento["score_abs"]  = score_abs
        evento["score_norm"] = score_norm
        if volume > VOL_MIN_SWEEP or score_norm >= SCORE_UMBRAL_ACCION_IMPULSO:
            setup = generar_setup_impulso(zona_relevante, precio_actual, direccion, score_norm)

    confirmacion_str = "✅ cierre confirmado" if cierre_confirmado else "⚡ mechazo sin cierre"
    pre_score_text = f"🔮 Pre‑impulso: {pre_score}/10" if pre_score > 0 else ""

    msg  = f"🚀 **IMPULSO {direccion} {emoji}** – Score {score_norm}/10\n\n"
    msg += f"Precio: {fmt(precio_actual)} - Hora UTC: {ahora.strftime('%H:%M')}\n"
    msg += f"Mechazo: {price_change:.2f}% ({confirmacion_str})\n"
    msg += f"High: {fmt(high_price)} | Low: {fmt(low_price)}\n"
    msg += f"Volumen: {volume:.0f} BTC\n"
    msg += f"Bias: {bias}\n\n"
    if pre_score_text:
        msg += f"{pre_score_text}\n"
    if prob_text:
        msg += f"{prob_text}\n"
    if zona_relevante:
        msg += f"🧲 Z objetivo: {fmt(zona_relevante['centro'])} ({distancia(zona_relevante, precio_actual)*100:.1f}%)"
    if setup:
        msg += f"\n\n👉 **SETUP**\n"
        msg += f"Entrada: {fmt(setup['entrada'])}\n"
        msg += f"SL: {fmt(setup['sl'])} ({(setup['entrada']-setup['sl'])/setup['entrada']*100:.2f}%)\n"
        msg += f"TP: {fmt(setup['tp'])} ({(setup['tp']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
        msg += f"Confianza: {setup['confianza']}"
    enviar(msg)

    last_impulse_time = ahora
    last_event_time   = ahora
    return True

# =========================
# RADAR 3 (SWEEP) – sin cambios
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
    return {"accion": accion, "entrada": entrada, "sl": sl, "tp": tp, "confianza": confianza}

def radar_sweep(df_entry, mejor_zona_arriba, mejor_zona_abajo, precio_actual, zonas_arriba, zonas_abajo, bias):
    global sweep_pendiente, last_event_time, regimen_actual
    if df_entry.empty or len(df_entry) < 2:
        return

    vela_actual = df_entry.iloc[-1]
    if sweep_pendiente:
        zona, tipo_sweep = sweep_pendiente
        if tipo_sweep == "HIGH" and vela_actual["close"] < vela_actual["open"]:
            direccion_rev = "BAJISTA"
        elif tipo_sweep == "LOW" and vela_actual["close"] > vela_actual["open"]:
            direccion_rev = "ALCISTA"
        else:
            sweep_pendiente = None
            return

        vela_sweep = df_entry.iloc[-2]
        vol_sweep  = vela_sweep["volume"]
        prob       = obtener_probabilidad(vol_sweep)
        prob_text  = f"📊 Probabilidad: {prob}%" if prob else ""

        peso_zona = calcular_peso_zona(
            zona if 'toques'   in zona else None,
            zona if 'oi_total' in zona else None,
            precio_actual
        )
        validacion_spot = False
        if 'oi_total' in zona:
            for z in zonas_arriba + zonas_abajo:
                if 'toques' in z and abs(zona['centro'] - z['centro']) / zona['centro'] < SPOT_FUTUROS_TOLERANCIA:
                    validacion_spot = True
                    break

        score_abs  = calcular_score_evento("sweep", direccion_rev, bias, peso_zona, validacion_spot, vol_sweep)
        score_norm = normalizar_score(score_abs)

        ahora  = datetime.now(UTC)
        evento = {
            "timestamp": ahora.isoformat(), "tipo": "sweep", "direccion": direccion_rev,
            "precio": precio_actual, "score_abs": score_abs, "score_norm": score_norm,
            "volumen": vol_sweep, "zona_centro": zona['centro'],
            "resultado_scalp": None, "resultado_tend": None, "resultado_largo": None,
            "evaluado_scalp": False, "evaluado_tend": False, "evaluado_largo": False, "evaluado": False
        }
        with data_lock:
            historial_eventos.append(evento)

        if vol_sweep > VOL_MIN_SWEEP or score_norm >= SCORE_UMBRAL_SWEEP:
            setup = generar_setup_sweep(zona, precio_actual, direccion_rev, score_norm)
            if setup:
                msg  = f"🔄 **SWEEP {direccion_rev} (SETUP)** – Score {score_norm}/10\n\n"
                msg += f"Entrada: {fmt(setup['entrada'])}\n"
                msg += f"SL: {fmt(setup['sl'])} ({(setup['entrada']-setup['sl'])/setup['entrada']*100:.2f}%)\n"
                msg += f"TP: {fmt(setup['tp'])} ({(setup['tp']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
                msg += f"Confianza: {setup['confianza']}\n\n"
                msg += f"Z barrida: {fmt(zona['centro'])} | P. actual: {fmt(precio_actual)} | Hora: {ahora.strftime('%H:%M')}\n"
                msg += f"Volumen: {vol_sweep:.0f} BTC\n"
                if prob_text:
                    msg += f"{prob_text}\n"
                msg += f"Bias: {bias}"
                enviar(msg)
            registrar_evento_para_patron("sweep", direccion_rev)

        last_event_time = ahora
        sweep_pendiente = None
        return

    vela_anterior = df_entry.iloc[-2]
    vol_medio     = df_entry["volume"].rolling(20).mean().iloc[-1]

    for zona in [z for z in [mejor_zona_arriba, mejor_zona_abajo] if z]:
        peso = calcular_peso_zona(
            zona if 'toques'   in zona else None,
            zona if 'oi_total' in zona else None,
            precio_actual
        )
        if peso < SWEEP_PESO_MINIMO:
            continue
        if 'max' in zona and \
           vela_anterior["high"] > zona["max"] * (1 + SWEEP_BREAK_MARGIN) and \
           vela_anterior["close"] < zona["centro"] and \
           vela_anterior["volume"] > vol_medio * SWEEP_VOLUME_FACTOR:
            sweep_pendiente = (zona, "HIGH")
            break
        elif 'min' in zona and \
             vela_anterior["low"] < zona["min"] * (1 - SWEEP_BREAK_MARGIN) and \
             vela_anterior["close"] > zona["centro"] and \
             vela_anterior["volume"] > vol_medio * SWEEP_VOLUME_FACTOR:
            sweep_pendiente = (zona, "LOW")
            break

# =========================
# RADAR 4 (BREAKOUT) – sin cambios
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
    return {"accion": accion, "entrada": entrada, "sl": sl, "tp": tp, "confianza": confianza}

def _procesar_breakout(zona, direccion, close, vol_break, zonas_arriba, zonas_abajo, bias, ahora):
    peso_zona = calcular_peso_zona(
        zona if 'toques'   in zona else None,
        zona if 'oi_total' in zona else None,
        close
    )
    if peso_zona < BREAKOUT_PESO_MINIMO:
        return False

    validacion_spot = False
    if 'oi_total' in zona:
        for z in zonas_arriba + zonas_abajo:
            if 'toques' in z and abs(zona['centro'] - z['centro']) / zona['centro'] < SPOT_FUTUROS_TOLERANCIA:
                validacion_spot = True
                break

    score_abs  = calcular_score_evento("breakout", direccion, bias, peso_zona, validacion_spot, vol_break)
    score_norm = normalizar_score(score_abs)

    if not (vol_break > VOL_MIN_SWEEP or score_norm >= SCORE_UMBRAL_SWEEP):
        return False

    key = ("break", redondear_centro(zona["centro"]))
    alerted_liquidity_ts[key] = ahora.timestamp()

    evento = {
        "timestamp": ahora.isoformat(), "tipo": "breakout", "direccion": direccion,
        "precio": close, "score_abs": score_abs, "score_norm": score_norm,
        "volumen": vol_break, "zona_centro": zona['centro'],
        "resultado_scalp": None, "resultado_tend": None, "resultado_largo": None,
        "evaluado_scalp": False, "evaluado_tend": False, "evaluado_largo": False, "evaluado": False
    }
    with data_lock:
        historial_eventos.append(evento)

    registrar_evento_para_patron("breakout", direccion)

    setup = generar_setup_breakout(zona, close, direccion, score_norm)
    if setup:
        prob      = obtener_probabilidad(vol_break)
        prob_text = f"📊 Probabilidad: {prob}%" if prob else ""
        sl_pct    = abs(setup['entrada'] - setup['sl']) / setup['entrada'] * 100
        tp_pct    = abs(setup['tp'] - setup['entrada']) / setup['entrada'] * 100
        msg  = f"🌋 **BREAKOUT {direccion} (SETUP)** – Score {score_norm}/10\n\n"
        msg += f"Entrada: {fmt(setup['entrada'])}\n"
        msg += f"SL: {fmt(setup['sl'])} ({sl_pct:.2f}%)\n"
        msg += f"TP: {fmt(setup['tp'])} ({tp_pct:.2f}%)\n"
        msg += f"Confianza: {setup['confianza']}\n\n"
        msg += f"Z rota: {fmt(zona['centro'])} | Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}\n"
        msg += f"Volumen: {vol_break:.0f} BTC\n"
        if prob_text:
            msg += f"{prob_text}\n"
        msg += f"Bias: {bias}"
        enviar(msg)
    return True

def radar_breakout(df_entry, mejor_zona_arriba, mejor_zona_abajo, precio_actual, zonas_arriba, zonas_abajo, bias):
    global zona_consumida, last_event_time
    if df_entry.empty or len(df_entry) < BREAKOUT_RETEST_CANDLES + 1:
        return

    vela      = df_entry.iloc[-1]
    close     = vela["close"]
    vol_break = vela["volume"]
    ahora     = datetime.now(UTC)

    def hubo_retest(zona):
        if BREAKOUT_RETEST_CANDLES == 0:
            return True
        for i in range(2, BREAKOUT_RETEST_CANDLES + 2):
            if i > len(df_entry):
                break
            v = df_entry.iloc[-i]
            if zona["min"] < v["close"] < zona["max"]:
                return True
        return False

    with data_lock:
        if mejor_zona_arriba:
            key    = ("break", redondear_centro(mejor_zona_arriba["centro"]))
            limite = mejor_zona_arriba.get("max", mejor_zona_arriba["centro"] * 1.01) * (1 + BREAKOUT_MARGIN)
            if key not in alerted_liquidity_ts and close > limite and hubo_retest(mejor_zona_arriba):
                if _procesar_breakout(mejor_zona_arriba, "ALCISTA", close, vol_break, zonas_arriba, zonas_abajo, bias, ahora):
                    zona_consumida  = True
                    last_event_time = ahora
                    return

        if mejor_zona_abajo:
            key    = ("break", redondear_centro(mejor_zona_abajo["centro"]))
            limite = mejor_zona_abajo.get("min", mejor_zona_abajo["centro"] * 0.99) * (1 - BREAKOUT_MARGIN)
            if key not in alerted_liquidity_ts and close < limite and hubo_retest(mejor_zona_abajo):
                if _procesar_breakout(mejor_zona_abajo, "BAJISTA", close, vol_break, zonas_arriba, zonas_abajo, bias, ahora):
                    zona_consumida  = True
                    last_event_time = ahora

# =========================
# RADAR 5 – ESTRUCTURA LENTA
# =========================

def generar_setup_lento(zona, precio_actual, direccion):
    if zona is None:
        return None
    if direccion == "LONG":
        entrada = round(precio_actual, 1)
        sl = round(zona["min"] * 0.995, 1)
        tp = round(zona["max"] * 1.02, 1)
        accion = "COMPRA (LONG)"
    else:
        entrada = round(precio_actual, 1)
        sl = round(zona["max"] * 1.005, 1)
        tp = round(zona["min"] * 0.98, 1)
        accion = "VENTA (SHORT)"
    return {"accion": accion, "entrada": entrada, "sl": sl, "tp": tp, "confianza": "MEDIA"}

def radar_estructura_lenta(precio_actual, zonas_macro, regimen):
    global last_event_time, alerted_macro
    if regimen not in ["ACUMULACION", "DISTRIBUCION"] or not zonas_macro:
        return

    ahora = datetime.now(UTC)
    cercanas = [(z, abs(z["centro"] - precio_actual) / precio_actual)
                for z in zonas_macro
                if abs(z["centro"] - precio_actual) / precio_actual < ZONA_MACRO_ALERTA_DIST]
    if not cercanas:
        return

    zona, dist = min(cercanas, key=lambda x: x[1])
    direccion  = "LONG" if regimen == "ACUMULACION" else "SHORT"
    key        = (redondear_centro(zona["centro"], 200), direccion)

    ultimo = alerted_macro.get(key)
    if ultimo and (ahora - ultimo) < timedelta(hours=ZONA_MACRO_COOLDOWN_HORAS):
        return
    if dist >= ZONA_MACRO_SETUP_DIST:
        return

    alerted_macro[key] = ahora

    evento = {
        "timestamp": ahora.isoformat(), "tipo": "estructura_lenta", "direccion": direccion,
        "precio": precio_actual, "score_abs": 0, "score_norm": 5, "volumen": 0,
        "zona_centro": zona['centro'],
        "resultado_scalp": None, "resultado_tend": None, "resultado_largo": None,
        "evaluado_scalp": False, "evaluado_tend": False, "evaluado_largo": False, "evaluado": False
    }
    with data_lock:
        historial_eventos.append(evento)

    emoji = "🟢" if regimen == "ACUMULACION" else "🔴"
    setup = generar_setup_lento(zona, precio_actual, direccion)
    if setup:
        msg  = f"🐢 **RADAR 5 - {regimen} {emoji}**\n\n"
        msg += f"🧲 Z macro: {fmt(zona['centro'])} | Distancia: {dist*100:.2f}%\n"
        msg += f"P. actual: {fmt(precio_actual)}\n"
        msg += f"\n👉 **SETUP**\n"
        msg += f"Entrada: {fmt(setup['entrada'])}\n"
        msg += f"SL: {fmt(setup['sl'])} ({(setup['entrada']-setup['sl'])/setup['entrada']*100:.2f}%)\n"
        msg += f"TP: {fmt(setup['tp'])} ({(setup['tp']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
        msg += f"Confianza: {setup['confianza']}"
        enviar(msg)
        last_event_time = ahora

# =========================
# ALERTAS DE SISTEMA, HEARTBEAT HORARIO CHILE, DERIVA SILENCIOSA
# =========================

def heartbeat():
    """
    Envía heartbeat solo a las 9:00 y 16:00 hora Chile (UTC-4).
    Usa un offset fijo de -4 horas (ajustable).
    """
    global last_heartbeat_time
    ahora_utc = datetime.now(UTC)
    hora_chile = ahora_utc - timedelta(hours=4)
    hora_str = hora_chile.strftime('%H:%M')
    es_hora = (hora_str == "09:00" or hora_str == "16:00")
    if not es_hora:
        return

    if last_heartbeat_time is None:
        pass
    else:
        if (ahora_utc - last_heartbeat_time) < timedelta(hours=1):
            return

    precio = obtener_precio_actual() or 0
    msg = (f"🤖 BOT DE ARTURO FUNCIONANDO (V13.13)\n"
           f"Hora Chile (UTC-4): {hora_chile.strftime('%H:%M')}\n"
           f"Precio: {fmt(precio)}\nRégimen: {regimen_actual}")
    enviar(msg)
    last_heartbeat_time = ahora_utc

def sin_eventos():
    global last_event_time
    ahora = datetime.now(UTC)
    if last_event_time is None:
        last_event_time = ahora
        return
    if (ahora - last_event_time) > timedelta(hours=NO_EVENT_HOURS):
        precio = obtener_precio_actual() or 0
        msg = (f"⚠️ MERCADO SIN EVENTOS RELEVANTES\n"
               f"Tiempo sin señales: {NO_EVENT_HOURS}h\n"
               f"Precio: {fmt(precio)} | Hora UTC: {ahora.strftime('%H:%M')}\n"
               f"Estado: lateral / baja volatilidad")
        enviar(msg)
        last_event_time = ahora

def alerta_deriva_silenciosa(precio_actual, ahora):
    global ultima_deriva_time, ultimo_precio_deriva, last_event_time
    if ultima_deriva_time is None:
        ultima_deriva_time   = ahora
        ultimo_precio_deriva = precio_actual
        return
    if (ahora - ultima_deriva_time) >= timedelta(hours=ALERTA_DERIVA_HORAS):
        variacion = abs(precio_actual - ultimo_precio_deriva) / ultimo_precio_deriva * 100
        if variacion >= ALERTA_DERIVA_PORCENTAJE:
            if precio_actual > ultimo_precio_deriva:
                nombre, emoji = "ALZA SILENCIOSA", "🟢"
            else:
                nombre, emoji = "BAJA SILENCIOSA", "🔴"
            msg  = f"**{nombre} {emoji}** – {variacion:.1f}% en {ALERTA_DERIVA_HORAS*60:.0f} min\n"
            msg += f"Precio: {fmt(ultimo_precio_deriva)} → {fmt(precio_actual)}\n"
            msg += f"Dirección: {nombre.split()[0]} ({regimen_actual})"
            enviar(msg)
            last_event_time = ahora
        ultima_deriva_time   = ahora
        ultimo_precio_deriva = precio_actual

# =========================
# EVALUACIÓN DE RESULTADOS
# =========================

def _evaluar_horizonte(evento, precio_actual, campo_evaluado, campo_resultado, minutos, rango):
    if evento.get(campo_evaluado, False):
        return
    ts_evento = datetime.fromisoformat(evento["timestamp"])
    if (datetime.now(UTC) - ts_evento) <= timedelta(minutes=minutos):
        return
    precio_inicial = evento["precio"]
    direccion      = evento["direccion"]
    delta          = abs(precio_actual - precio_inicial) / precio_inicial
    if direccion == "ALCISTA":
        if precio_actual >= precio_inicial * (1 + rango):   resultado = "EXITO"
        elif precio_actual <= precio_inicial * (1 - rango): resultado = "FRACASO"
        elif delta < 0.001:                                  resultado = "NEUTRO"
        else:                                                resultado = "FRACASO"
    else:
        if precio_actual <= precio_inicial * (1 - rango):   resultado = "EXITO"
        elif precio_actual >= precio_inicial * (1 + rango): resultado = "FRACASO"
        elif delta < 0.001:                                  resultado = "NEUTRO"
        else:                                                resultado = "FRACASO"
    evento[campo_resultado] = resultado
    evento[campo_evaluado]  = True

def evaluar_eventos_pendientes(precio_actual):
    with data_lock:
        eventos_copia = list(historial_eventos)
    for evento in eventos_copia:
        _evaluar_horizonte(evento, precio_actual, "evaluado_scalp", "resultado_scalp", EVALUACION_VELAS_SCALP * 5, RANGO_EXITO_SCALP)
        _evaluar_horizonte(evento, precio_actual, "evaluado_tend",  "resultado_tend",  EVALUACION_VELAS_TEND  * 5, RANGO_EXITO_TEND)
        _evaluar_horizonte(evento, precio_actual, "evaluado_largo", "resultado_largo", EVALUACION_VELAS_LARGA * 5, RANGO_EXITO_LARGA)
        if evento.get("evaluado_scalp") and evento.get("evaluado_tend") and evento.get("evaluado_largo"):
            evento["evaluado"] = True

def generar_informe_resultados_como_texto():
    with data_lock:
        if not historial_eventos:
            return None
        df = pd.DataFrame(list(historial_eventos))
    df_scalp = df[df["evaluado_scalp"] == True]
    df_tend  = df[df["evaluado_tend"]  == True]
    df_largo = df[df["evaluado_largo"] == True]
    if df_scalp.empty and df_tend.empty and df_largo.empty:
        return None

    informe = "📊 **INFORME DE RESULTADOS (V13.13)**\n\n"
    for tipo in df["tipo"].unique():
        informe += f"🔹 {tipo.upper()}\n"

        def _bloque(subset, col_res, label):
            if subset.empty:
                return ""
            total    = len(subset)
            exitos   = len(subset[subset[col_res] == "EXITO"])
            fracasos = len(subset[subset[col_res] == "FRACASO"])
            neutros  = len(subset[subset[col_res] == "NEUTRO"])
            tasa     = exitos / total * 100 if total else 0
            out      = f"   {label}: {total} ev | Éxito: {exitos} ({tasa:.1f}%) | Fracaso: {fracasos} | Neutro: {neutros}\n"
            if "volumen" in subset.columns and col_res == "resultado_scalp":
                for etq, cond in [
                    ("Vol <300",    subset["volumen"] < 300),
                    ("Vol 300-600", (subset["volumen"] >= 300) & (subset["volumen"] <= 600)),
                    ("Vol >600",    subset["volumen"] > 600)
                ]:
                    sub = subset[cond]
                    if not sub.empty:
                        ex  = len(sub[sub[col_res] == "EXITO"])
                        out += f"      📊 {etq}: {len(sub)} ops, éxito {ex/len(sub)*100:.1f}%\n"
                ex_s  = subset[subset[col_res] == "EXITO"]["score_norm"]   if "score_norm" in subset.columns else pd.Series(dtype=float)
                fra_s = subset[subset[col_res] == "FRACASO"]["score_norm"] if "score_norm" in subset.columns else pd.Series(dtype=float)
                if not ex_s.empty or not fra_s.empty:
                    out += f"      🎯 Score promedio: éxito {ex_s.mean():.1f} / fracaso {fra_s.mean():.1f}\n"
            return out

        informe += _bloque(df_scalp[df_scalp["tipo"] == tipo], "resultado_scalp", "⏱️ Scalping (2v)")
        informe += _bloque(df_tend [df_tend ["tipo"] == tipo], "resultado_tend",  "📈 Tendencia (6v)")
        informe += _bloque(df_largo[df_largo["tipo"] == tipo], "resultado_largo", "🐢 Largo plazo (12v)")
        informe += "\n"
    return informe

# =========================
# GUARDADO Y LIMPIEZA
# =========================

def guardar_historial_si_necesario(ahora):
    global ultimo_guardado
    if (ahora - ultimo_guardado) >= timedelta(minutes=10) or ahora.minute == 0:
        try:
            with data_lock:
                snapshot = list(historial_eventos)
            with open(HISTORIAL_FILE, "w") as f:
                json.dump(snapshot, f)
            ultimo_guardado = ahora
        except Exception as e:
            print(f"Error guardando historial: {e}")

def limpiar_alerted_liquidity_si_necesario(ahora):
    global ultima_limpieza_liquidity
    if (ahora - ultima_limpieza_liquidity) >= timedelta(hours=24):
        limite = (ahora - timedelta(days=7)).timestamp()
        with data_lock:
            claves = [k for k, ts in alerted_liquidity_ts.items() if ts < limite]
            for k in claves:
                alerted_liquidity_ts.pop(k, None)
        ultima_limpieza_liquidity = ahora

# =========================
# COMANDOS TELEGRAM
# =========================

ultimo_update_id = None

def obtener_mensajes():
    global ultimo_update_id
    params_get = {"timeout": 30, "offset": ultimo_update_id}
    url_upd = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    try:
        r    = requests.get(url_upd, params=params_get, timeout=35)
        data = r.json()
        if data["ok"]:
            for update in data["result"]:
                ultimo_update_id = update["update_id"] + 1
                if "message" in update and "text" in update["message"]:
                    texto   = update["message"]["text"]
                    chat_id = update["message"]["chat"]["id"]
                    if texto.startswith("/"):
                        procesar_comando(texto, chat_id)
    except Exception as e:
        print(f"Error getUpdates: {e}")

def procesar_comando(texto, chat_id):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    if texto in ("/stats", "/reporte"):
        informe = generar_informe_resultados_como_texto()
        msg     = informe if informe else "No hay datos suficientes aún."
        requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=10)

# =========================
# FUNCIÓN PRINCIPAL EVALUAR
# =========================

def evaluar():
    global zona_actual, zona_consumida, last_event_time, regimen_actual, zonas_macro, alerted_proximidad
    global last_pre_alert_time

    ahora    = datetime.now(UTC)
    hora_str = ahora.strftime('%H:%M')

    df_1h, df_4h, df_entry, df_oi, precio = obtener_todos_los_datos()

    if df_entry.empty or precio is None:
        return

    evaluar_eventos_pendientes(precio)
    bias = calcular_bias(df_1h, df_4h, precio)

    zonas_high_spot, zonas_low_spot = detectar_zonas_spot(df_1h) if not df_1h.empty else ([], [])
    mejor_spot_arriba, mejor_spot_abajo = seleccionar_mejores_zonas_spot(zonas_high_spot, zonas_low_spot, precio)

    clusters_oi     = detectar_zonas_oi(df_oi, df_1h) if not df_oi.empty else []
    mejor_oi_arriba = mejor_oi_abajo = None
    if clusters_oi:
        arriba_oi = sorted([c for c in clusters_oi if c["centro"] > precio], key=lambda x: x["oi_total"], reverse=True)
        abajo_oi  = sorted([c for c in clusters_oi if c["centro"] < precio], key=lambda x: x["oi_total"], reverse=True)
        mejor_oi_arriba = arriba_oi[0] if arriba_oi else None
        mejor_oi_abajo  = abajo_oi[0]  if abajo_oi  else None

    actualizar_zonas_internas(mejor_oi_arriba, mejor_oi_abajo, mejor_spot_arriba, mejor_spot_abajo, precio, hora_str, bias)

    zona_ref_arriba = mejor_oi_arriba or mejor_spot_arriba
    zona_ref_abajo  = mejor_oi_abajo  or mejor_spot_abajo
    radar_proximidad_interno(zona_ref_arriba, zona_ref_abajo, precio, hora_str, bias)

    zonas_arriba = [z for z in [mejor_oi_arriba, mejor_spot_arriba] if z]
    zonas_abajo  = [z for z in [mejor_oi_abajo,  mejor_spot_abajo]  if z]

    # Calcular pre‑impulso score (usando la mejor zona relevante, si existe)
    zona_referencia = zonas_arriba[0] if zonas_arriba else (zonas_abajo[0] if zonas_abajo else None)
    with data_lock:
        snapshot_ultimos = list(ultimos_eventos)
    pre_score = calcular_pre_impulso_score(df_entry, df_oi, zona_referencia, snapshot_ultimos, last_impulse_time, ahora)

    # Alerta separada si pre_score alto y respeta cooldown
    if pre_score >= PRE_SCORE_ALERTA_UMBRAL:
        if last_pre_alert_time is None or (ahora - last_pre_alert_time) > timedelta(minutes=PRE_SCORE_ALERTA_COOLDOWN):
            enviar(f"⚠️ MERCADO LISTO PARA IMPULSO (score {pre_score}/10) – Hora UTC: {ahora.strftime('%H:%M')}")
            last_pre_alert_time = ahora

    hubo_impulso = radar_impulse(df_entry, precio, zonas_arriba, zonas_abajo, bias, pre_score)

    resultado_impulso = None
    if hubo_impulso:
        with data_lock:
            ultimo_imp = next((e for e in reversed(historial_eventos)
                               if e["tipo"] == "impulso" and e.get("evaluado_scalp")), None)
        if ultimo_imp:
            resultado_impulso = ultimo_imp.get("resultado_scalp")

    actualizar_regimen(df_1h, hubo_impulso, resultado_impulso)
    radar_estructura_lenta(precio, zonas_macro, regimen_actual)
    radar_sweep(df_entry, zona_ref_arriba, zona_ref_abajo, precio, zonas_arriba, zonas_abajo, bias)
    radar_breakout(df_entry, zona_ref_arriba, zona_ref_abajo, precio, zonas_arriba, zonas_abajo, bias)

    with data_lock:
        viejos = [k for k, ts in alerted_proximidad.items() if (ahora - ts) > timedelta(hours=2)]
        for k in viejos:
            alerted_proximidad.pop(k, None)

    alerta_deriva_silenciosa(precio, ahora)
    heartbeat()
    sin_eventos()
    guardar_historial_si_necesario(ahora)
    limpiar_alerted_liquidity_si_necesario(ahora)

# =========================
# BUCLE PRINCIPAL
# =========================

if __name__ == "__main__":
    print("🚀 Iniciando BOT V13.13 (pre‑impulso score, alerta separada, heartbeat horario Chile, umbral 0.5%)...")

    precio_inicial = None
    for intento in range(5):
        precio_inicial = obtener_precio_actual()
        if precio_inicial is not None:
            break
        print(f"⚠️ Precio inicial None (intento {intento+1}/5), reintentando en 5s...")
        time.sleep(5)

    hora_actual = datetime.now(UTC).strftime('%H:%M')
    precio_str  = fmt(precio_inicial) if precio_inicial is not None else "N/D"
    enviar(f"🤖 BOT DE ARTURO FUNCIONANDO (V13.13)\nHora UTC: {hora_actual}\nPrecio: {precio_str}")

    last_heartbeat_time       = datetime.now(UTC)
    last_pre_alert_time       = None
    last_event_time           = datetime.now(UTC)
    ultimo_guardado           = datetime.now(UTC)
    ultima_limpieza_liquidity = datetime.now(UTC)

    if os.path.exists(HISTORIAL_FILE):
        try:
            with open(HISTORIAL_FILE, "r") as f:
                data = json.load(f)
            with data_lock:
                historial_eventos.extend(data[-2000:])
            print(f"✅ Historial cargado: {len(data)} eventos.")
        except Exception as e:
            print(f"⚠️ Error cargando historial '{HISTORIAL_FILE}': {e}")

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
