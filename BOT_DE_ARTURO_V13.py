# -*- coding: utf-8 -*-
# BOT V13.9 – Mejoras sobre V13.8:
#   1. atexit para cierre limpio del executor global
#   2. Bug fix: global oi_increment_history + recorte in-place en detectar_zonas_oi
#   3. print(e) en carga de historial para depuración
#   4. Comentario explícito en _procesar_breakout: se llama siempre dentro de data_lock
#   5. Lock en registrar_evento_para_patron (ultimos_eventos)

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

IMPULSE_PRICE_CHANGE = 0.65
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

VOL2_MED = 712;  VOL2_P75 = 1189; VOL2_P90 = 1876
PROB2_MED = 72;  PROB2_P75 = 84;  PROB2_P90 = 87

VOL3_MED = 1023; VOL3_P75 = 1645; VOL3_P90 = 2534
PROB3_MED = 75;  PROB3_P75 = 86;  PROB3_P90 = 89

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

HEARTBEAT_HOURS        = 4
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

ALERTA_DERIVA_HORAS      = 2
ALERTA_DERIVA_PORCENTAJE = 0.5

# =========================
# ESTADO GLOBAL
# =========================

last_impulse_time  = None
last_heartbeat_time = None
last_event_time    = None
last_mapa_time     = None
zona_actual        = None
zona_consumida     = False
alerted_liquidity_ts = {}
alerted_proximidad   = {}
sweep_pendiente      = None
ultima_zona_arriba   = None
ultima_zona_abajo    = None

# MEJORA 2: oi_increment_history como lista mutable global (recorte in-place)
oi_increment_history = []

historial_eventos = deque(maxlen=2000)
ultimos_eventos   = deque(maxlen=10)   # protegido por data_lock (MEJORA 5)

regimen_actual        = "NEUTRAL"
ultimo_cambio_regimen = None
estructura_detected_at = None
zonas_macro           = []

ultima_deriva_time   = None
ultimo_precio_deriva = None

alerted_macro = {}

# Locks y control de tiempo
data_lock                 = threading.RLock()   # RLock para evitar deadlocks reentrantes
ultimo_guardado           = datetime.now(UTC)
ultima_limpieza_liquidity = datetime.now(UTC)

# MEJORA 1: executor persistente con cierre limpio via atexit
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
        df["sumOpenInterest"]      = pd.to_numeric(df["sumOpenInterest"])
        df["sumOpenInterestValue"] = pd.to_numeric(df["sumOpenInterestValue"])
        df["timestamp"]            = pd.to_datetime(df["timestamp"], unit='ms')
        return df
    except Exception as e:
        print(f"Error OI: {e}")
        return pd.DataFrame()

# =========================
# OBTENCIÓN PARALELA (executor persistente)
# =========================

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

# =========================
# CLUSTERING Y ZONAS
# =========================

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

def obtener_probabilidad(volumen, umbrales, probabilidades):
    if volumen > umbrales[2]:   return probabilidades[2]
    elif volumen > umbrales[1]: return probabilidades[1]
    elif volumen > umbrales[0]: return probabilidades[0]
    else:                       return None

def registrar_evento_para_patron(tipo, direccion):
    """
    MEJORA 5: ultimos_eventos protegido por data_lock.
    Se llama desde radar_breakout (ya dentro de data_lock) y radar_sweep
    (fuera de lock, por eso adquirimos aquí).
    """
    clave = f"{tipo}_{direccion}"
    with data_lock:
        ultimos_eventos.append(clave)
        snapshot = list(ultimos_eventos)
    if len(snapshot) >= 3 and all(e == f"sweep_{direccion}" for e in snapshot[-3:]):
        enviar(f"⚠️ POSIBLE ACUMULACIÓN: 3 sweeps {direccion} consecutivos")

# =========================
# RÉGIMEN Y ZONAS MACRO
# =========================

def detectar_estructura_y_zonas(df_1h):
    if df_1h.empty or len(df_1h) < 336:
        if not hasattr(detectar_estructura_y_zonas, "warning_sent"):
            print("⚠️ Menos de 336 velas en df_1h, régimen NEUTRAL temporal")
            enviar("⚠️ Advertencia: menos de 336 velas 1h disponibles. Régimen = NEUTRAL.")
            detectar_estructura_y_zonas.warning_sent = True
        return "NEUTRAL", []

    highs = df_1h["high"].values[-336:]
    lows  = df_1h["low"].values[-336:]
    pivot_highs, pivot_lows = [], []
    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            pivot_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            pivot_lows.append(lows[i])

    if len(pivot_highs) < 2 and len(pivot_lows) < 2:
        regimen = "NEUTRAL"
    else:
        t_h = all(pivot_highs[i] < pivot_highs[i+1] for i in range(len(pivot_highs)-1)) if len(pivot_highs) >= 2 else False
        t_l = all(pivot_lows[i]  < pivot_lows[i+1]  for i in range(len(pivot_lows)-1))  if len(pivot_lows)  >= 2 else False
        if t_h and t_l:         regimen = "ACUMULACION"
        elif not t_h and not t_l: regimen = "DISTRIBUCION"
        else:                   regimen = "NEUTRAL"

    zh, zl = detectar_zonas_spot(df_1h, lookback=336, min_toques=3, rango=0.005)
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
# MEJORA 2: global explícito + recorte in-place ([:] =) para no crear lista local
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
    # MEJORA 2: declaración global explícita + recorte in-place
    global oi_increment_history

    if df_oi.empty or len(df_oi) < OI_LOOKBACK + 1:
        return []

    # Incrementos positivos usando pandas (O(n))
    oi_vals     = df_oi["sumOpenInterestValue"].astype(float)
    incrementos = oi_vals.diff().clip(lower=0).fillna(0)

    oi_increment_history.extend(incrementos.tolist())

    # Recorte in-place: modifica la misma lista global, no crea una nueva
    if len(oi_increment_history) > OI_WINDOW:
        oi_increment_history[:] = oi_increment_history[-OI_WINDOW:]

    umbral = np.percentile(oi_increment_history, OI_PERCENTILE) \
             if len(oi_increment_history) >= 10 else 20_000_000

    # Suma deslizante O(n) con rolling
    suma_rolling = incrementos.rolling(window=OI_LOOKBACK).sum()

    # Preparar df_spot para búsqueda por timestamp
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
# RADAR 0 – IMPULSO
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

def radar_impulse(df_entry, precio_actual, zonas_arriba, zonas_abajo, bias):
    global last_impulse_time, last_event_time, regimen_actual
    if df_entry.empty or len(df_entry) < 3:
        return False

    vela = df_entry.iloc[-1]
    try:
        open_price  = float(vela["open"])
        close_price = float(vela["close"])
        volume      = float(vela["volume"])
        vol_medio   = df_entry["volume"].rolling(20).mean().iloc[-1]
    except:
        return False

    price_change = abs(close_price - open_price) / open_price * 100
    if price_change < IMPULSE_PRICE_CHANGE:
        return False

    ahora = datetime.now(UTC)
    if last_impulse_time and (ahora - last_impulse_time).seconds < IMPULSE_COOLDOWN:
        return False

    alcista   = close_price > open_price
    direccion = "ALCISTA" if alcista else "BAJISTA"
    emoji     = "🟢" if alcista else "🔴"

    zona_relevante = None
    if alcista and zonas_arriba:      zona_relevante = zonas_arriba[0]
    elif not alcista and zonas_abajo: zona_relevante = zonas_abajo[0]

    prob_lines = []
    vol1  = volume
    prob1 = obtener_probabilidad(vol1, [VOL1_MED, VOL1_P75, VOL1_P90], [PROB1_MED, PROB1_P75, PROB1_P90])
    if prob1:
        prob_lines.append(f"  1v ({vol1:.0f}): {prob1}%")

    if len(df_entry) >= 2:
        vela_ant    = df_entry.iloc[-2]
        alcista_ant = vela_ant["close"] > vela_ant["open"]
        if alcista == alcista_ant:
            vol2  = vol1 + vela_ant["volume"]
            prob2 = obtener_probabilidad(vol2, [VOL2_MED, VOL2_P75, VOL2_P90], [PROB2_MED, PROB2_P75, PROB2_P90])
            if prob2:
                prob_lines.append(f"  2v (misma dir, {vol2:.0f}): {prob2}%")

    if len(df_entry) >= 3:
        vela_ant2    = df_entry.iloc[-3]
        alcista_ant2 = vela_ant2["close"] > vela_ant2["open"]
        if alcista == alcista_ant and alcista == alcista_ant2:
            vol3  = vol1 + df_entry.iloc[-2]["volume"] + df_entry.iloc[-3]["volume"]
            prob3 = obtener_probabilidad(vol3, [VOL3_MED, VOL3_P75, VOL3_P90], [PROB3_MED, PROB3_P75, PROB3_P90])
            if prob3:
                prob_lines.append(f"  3v (misma dir, {vol3:.0f}): {prob3}%")

    if not prob_lines:
        prob_lines.append("  Volumen insuficiente: prob base 68%")

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
            zona_relevante if 'toques'  in zona_relevante else None,
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

    msg  = f"🚀 **IMPULSO {direccion} {emoji}** – Score {score_norm}/10\n\n"
    msg += f"Precio: {fmt(precio_actual)} - Hora UTC: {ahora.strftime('%H:%M')}\n"
    msg += f"Variación: {price_change:.2f}%\n"
    msg += f"Volumen: {vol1:.0f} BTC ({(vol1/vol_medio):.1f}x media)\n"
    msg += f"Bias: {bias}\n\n"
    msg += "📊 Probabilidad:\n" + "\n".join(prob_lines)
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
    last_event_time   = ahora
    return True

# =========================
# RADAR 3 (SWEEP)
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

        prob1     = obtener_probabilidad(vol_sweep, [VOL1_MED, VOL1_P75, VOL1_P90], [PROB1_MED, PROB1_P75, PROB1_P90])
        prob_line = f"  Volumen: {vol_sweep:.0f} BTC -> {prob1}%" if prob1 else "  Volumen bajo, prob base 68%"

        peso_zona = calcular_peso_zona(
            zona if 'toques'  in zona else None,
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
                msg += f"Volumen: {vol_sweep:.0f} BTC\n{prob_line}\n"
                msg += f"Bias: {bias}"
                enviar(msg)
            # MEJORA 5: registrar_evento_para_patron ya usa data_lock internamente
            registrar_evento_para_patron("sweep", direccion_rev)

        last_event_time = ahora
        sweep_pendiente = None
        return

    vela_anterior = df_entry.iloc[-2]
    vol_medio     = df_entry["volume"].rolling(20).mean().iloc[-1]

    for zona in [z for z in [mejor_zona_arriba, mejor_zona_abajo] if z]:
        peso = calcular_peso_zona(
            zona if 'toques'  in zona else None,
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
# RADAR 4 (BREAKOUT)
# MEJORA 4: _procesar_breakout se llama SIEMPRE dentro de with data_lock en radar_breakout.
#           El RLock permite la re-entrada sin deadlock cuando historial_eventos.append
#           también usa data_lock.
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
    """
    MEJORA 4: Esta función DEBE llamarse siempre dentro de 'with data_lock'.
    El RLock (no Lock) garantiza que la re-entrada en data_lock (para historial)
    no produzca deadlock.
    """
    peso_zona = calcular_peso_zona(
        zona if 'toques'  in zona else None,
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
    alerted_liquidity_ts[key] = ahora.timestamp()   # dentro de data_lock (llamador)

    evento = {
        "timestamp": ahora.isoformat(), "tipo": "breakout", "direccion": direccion,
        "precio": close, "score_abs": score_abs, "score_norm": score_norm,
        "volumen": vol_break, "zona_centro": zona['centro'],
        "resultado_scalp": None, "resultado_tend": None, "resultado_largo": None,
        "evaluado_scalp": False, "evaluado_tend": False, "evaluado_largo": False, "evaluado": False
    }
    with data_lock:   # re-entrada segura gracias a RLock
        historial_eventos.append(evento)

    # registrar_evento_para_patron usa data_lock internamente (RLock seguro)
    registrar_evento_para_patron("breakout", direccion)

    setup = generar_setup_breakout(zona, close, direccion, score_norm)
    if setup:
        prob      = obtener_probabilidad(vol_break, [VOL1_MED, VOL1_P75, VOL1_P90], [PROB1_MED, PROB1_P75, PROB1_P90])
        prob_line = f"Probabilidad: {prob}%" if prob else "Probabilidad base 68%"
        sl_pct    = abs(setup['entrada'] - setup['sl']) / setup['entrada'] * 100
        tp_pct    = abs(setup['tp'] - setup['entrada']) / setup['entrada'] * 100
        msg  = f"🌋 **BREAKOUT {direccion} (SETUP)** – Score {score_norm}/10\n\n"
        msg += f"Entrada: {fmt(setup['entrada'])}\n"
        msg += f"SL: {fmt(setup['sl'])} ({sl_pct:.2f}%)\n"
        msg += f"TP: {fmt(setup['tp'])} ({tp_pct:.2f}%)\n"
        msg += f"Confianza: {setup['confianza']}\n\n"
        msg += f"Z rota: {fmt(zona['centro'])} | Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}\n"
        msg += f"Volumen: {vol_break:.0f} BTC\n{prob_line}\n"
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

    msg  = f"🐢 **ALERTA ESTRUCTURA LENTA ({regimen} - {direccion})**\n\n"
    msg += f"🧲 Z macro: {fmt(zona['centro'])} (rango {fmt(zona['min'])}-{fmt(zona['max'])}) | Distancia: {dist*100:.2f}%\n"
    msg += f"P. actual: {fmt(precio_actual)}\n"
    if dist < ZONA_MACRO_SETUP_DIST:
        setup = generar_setup_lento(zona, precio_actual, direccion)
        if setup:
            msg += f"\n👉 **SETUP**\n"
            msg += f"Entrada: {fmt(setup['entrada'])}\n"
            msg += f"SL: {fmt(setup['sl'])} ({(setup['entrada']-setup['sl'])/setup['entrada']*100:.2f}%)\n"
            msg += f"TP: {fmt(setup['tp'])} ({(setup['tp']-setup['entrada'])/setup['entrada']*100:.2f}%)\n"
            msg += f"Confianza: {setup['confianza']}"
    else:
        msg += "\n(aproximación)"

    enviar(msg)
    last_event_time = ahora

# =========================
# ALERTAS DE SISTEMA Y DERIVA SILENCIOSA
# =========================

def heartbeat():
    global last_heartbeat_time
    ahora = datetime.now(UTC)
    if last_heartbeat_time is None:
        last_heartbeat_time = ahora
        return
    if (ahora - last_heartbeat_time) > timedelta(hours=HEARTBEAT_HOURS):
        precio = obtener_precio_actual() or 0
        msg = (f"🤖 BOT DE ARTURO FUNCIONANDO (V13.9)\n"
               f"Hora UTC: {ahora.strftime('%H:%M')}\nPrecio: {fmt(precio)}\nRégimen: {regimen_actual}")
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
        msg = (f"⚠️ MERCADO SIN EVENTOS RELEVANTES\n"
               f"Tiempo sin señales: {NO_EVENT_HOURS}h\n"
               f"Precio: {fmt(precio)} | Hora: {ahora.strftime('%H:%M')}\n"
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
            direccion = "ALCISTA" if precio_actual > ultimo_precio_deriva else "BAJISTA"
            msg  = f"📉 **DERIVA SILENCIOSA** – {variacion:.1f}% en {ALERTA_DERIVA_HORAS}h\n"
            msg += f"Precio: {fmt(ultimo_precio_deriva)} → {fmt(precio_actual)}\n"
            msg += f"Dirección: {direccion} ({regimen_actual})"
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
        if precio_actual >= precio_inicial * (1 + rango):     resultado = "EXITO"
        elif precio_actual <= precio_inicial * (1 - rango):   resultado = "FRACASO"
        elif delta < 0.001:                                    resultado = "NEUTRO"
        else:                                                  resultado = "FRACASO"
    else:
        if precio_actual <= precio_inicial * (1 - rango):     resultado = "EXITO"
        elif precio_actual >= precio_inicial * (1 + rango):   resultado = "FRACASO"
        elif delta < 0.001:                                    resultado = "NEUTRO"
        else:                                                  resultado = "FRACASO"

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

    informe = "📊 **INFORME DE RESULTADOS (V13.9)**\n\n"
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
                    ("Vol <300",   subset["volumen"] < 300),
                    ("Vol 300-600",(subset["volumen"] >= 300) & (subset["volumen"] <= 600)),
                    ("Vol >600",   subset["volumen"] > 600)
                ]:
                    sub = subset[cond]
                    if not sub.empty:
                        ex  = len(sub[sub[col_res] == "EXITO"])
                        out += f"      📊 {etq}: {len(sub)} ops, éxito {ex/len(sub)*100:.1f}%\n"
                ex_s  = subset[subset[col_res] == "EXITO"]["score_norm"]  if "score_norm" in subset.columns else pd.Series(dtype=float)
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
# GUARDADO Y LIMPIEZA PERIÓDICA
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
    url    = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params = {"timeout": 30, "offset": ultimo_update_id}
    try:
        r    = requests.get(url, params=params, timeout=35)
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

    hubo_impulso = radar_impulse(df_entry, precio, zonas_arriba, zonas_abajo, bias)

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

    # Limpieza de alerted_proximidad con lock
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
    print("🚀 Iniciando BOT V13.9 (atexit executor, OI global fix, locks completos, comentarios _procesar_breakout)...")
    precio_inicial = obtener_precio_actual()
    hora_actual    = datetime.now(UTC).strftime('%H:%M')
    enviar(f"🤖 BOT DE ARTURO FUNCIONANDO (V13.9)\nHora UTC: {hora_actual}\nPrecio: {fmt(precio_inicial)}")

    last_heartbeat_time       = datetime.now(UTC)
    last_event_time           = datetime.now(UTC)
    ultimo_guardado           = datetime.now(UTC)
    ultima_limpieza_liquidity = datetime.now(UTC)

    # MEJORA 3: print del error concreto al cargar historial
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
