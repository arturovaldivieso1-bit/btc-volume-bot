# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os
from datetime import datetime, timedelta, UTC

# =========================
# CONFIGURACIÓN INICIAL
# =========================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOL = "BTCUSDT"

# Timeframes
INTERVAL_MACRO = "1h"      # Para estructura de liquidez (spot)
INTERVAL_ENTRY = "5m"      # Para eventos

# Parámetros de liquidez spot
LOOKBACK = 168
MIN_TOUCHES = 3
CLUSTER_RANGE = 0.0025        # 0.25% para agrupar precios
PROXIMITY = 0.003              # 0.3% umbral inferior para Radar 2
RADAR1_MIN_DIST = 0.01         # 1% umbral mínimo para enviar Radar 1
ZONA_EQUIVALENTE = 0.01        # 1% para considerar misma zona (evita spam)

# Parámetros de Open Interest (futuros) - más sensibles
OI_SURGE_THRESHOLD = 10_000_000        # $10M (pico individual)
OI_ACCUMULATED_THRESHOLD = 20_000_000  # $20M acumulado en 3 velas
OI_LOOKBACK = 3
OI_CLUSTER_RANGE = 0.002               # 0.2% para agrupar zonas de OI
OI_CONFIANZA_ALTA = 200_000_000
OI_CONFIANZA_MEDIA = 100_000_000

# Radar 0 - Impulso (solo variación de precio)
IMPULSE_PRICE_CHANGE = 0.65
IMPULSE_COOLDOWN = 300          # 5 minutos

# Umbrales de volumen para probabilidades (estudio)
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
RADAR2_COOLDOWN_MINUTOS = 120         # 2 horas para evitar spam
REDONDEO_BASE = 200                    # Redondeo a 200 para estabilizar

# Variables de estado
last_impulse_time = None
last_heartbeat_time = None
last_event_time = None
last_mapa_time = None
zona_actual = None                     # Tupla (centro_arriba, centro_abajo) priorizando OI
zona_consumida = False
alerted_liquidity = set()               # Para breakouts y sweeps
alerted_proximidad = {}                  # Diccionario {(centro_rd, tipo): timestamp}
sweep_pendiente = None
ultima_zona_arriba = None               # Centro de la última zona enviada por Radar 1 (arriba)
ultima_zona_abajo = None                 # Centro de la última zona enviada por Radar 1 (abajo)

# =========================
# FUNCIONES AUXILIARES (SPOT)
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
    """Compara dos tuplas (centro_arriba, centro_abajo) con tolerancia ZONA_EQUIVALENTE"""
    if z1 is None or z2 is None:
        return False
    c1_arriba, c1_abajo = z1
    c2_arriba, c2_abajo = z2
    # Si ambos son None, son iguales
    if c1_arriba is None and c2_arriba is None:
        diff_arriba = 0
    elif c1_arriba is None or c2_arriba is None:
        diff_arriba = 1  # diferente
    else:
        diff_arriba = abs(c1_arriba - c2_arriba) / c1_arriba

    if c1_abajo is None and c2_abajo is None:
        diff_abajo = 0
    elif c1_abajo is None or c2_abajo is None:
        diff_abajo = 1
    else:
        diff_abajo = abs(c1_abajo - c2_abajo) / c1_abajo

    return diff_arriba < ZONA_EQUIVALENTE and diff_abajo < ZONA_EQUIVALENTE

# =========================
# FUNCIONES PARA OPEN INTEREST (FUTUROS)
# =========================

def obtener_open_interest_hist(period="5m", limit=100):
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
    clusters.sort(key=lambda x: x["oi_total"], reverse=True)
    return clusters

def detectar_zonas_oi(df_oi, df_spot):
    """
    Retorna una lista de clusters de OI (cada cluster es un dict con centro, min, max, oi_total, confianza)
    Si no hay datos, retorna lista vacía.
    """
    if df_oi.empty or len(df_oi) < OI_LOOKBACK + 1:
        return []

    eventos_oi = []
    for i in range(OI_LOOKBACK, len(df_oi)):
        suma_incrementos = 0
        for j in range(i - OI_LOOKBACK + 1, i + 1):
            oi_actual = float(df_oi.iloc[j]["sumOpenInterestValue"])
            oi_anterior = float(df_oi.iloc[j-1]["sumOpenInterestValue"])
            suma_incrementos += max(0, oi_actual - oi_anterior)
        if suma_incrementos > OI_ACCUMULATED_THRESHOLD:
            # Asociar al precio de la vela spot más cercana en el tiempo
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
# RADAR 1 (NUEVO CON PRIORIDAD OI Y DISTANCIA ≥1%)
# =========================

def enviar_liquidez_detectada(mejor_zona_oi_arriba, mejor_zona_oi_abajo, mejor_zona_spot_arriba, mejor_zona_spot_abajo, precio, hora):
    global ultima_zona_arriba, ultima_zona_abajo

    # Función para enviar una zona
    def enviar_zona(zona, tipo, es_oi):
        if es_oi:
            titulo = f"⚡ RADAR 1 – LIQUIDEZ FUTUROS {'ARRIBA' if tipo=='HIGH' else 'ABAJO'} {'🟣' if tipo=='HIGH' else '🔵'}"
            oi_valor = zona['oi_total'] / 1_000_000
            linea_extra = f"OI acumulado: +{oi_valor:.1f}M USD {zona['confianza']}"
        else:
            titulo = f"💰 RADAR 1 – LIQUIDEZ SPOT {'ARRIBA' if tipo=='HIGH' else 'ABAJO'} {'🟢' if tipo=='HIGH' else '🔴'}"
            linea_extra = f"{zona['toques']} toques{' 🔥' if zona['toques'] >= 5 else ''}"

        centro = fmt(zona['centro'])
        rango = f"{fmt(zona['min'])}-{fmt(zona['max'])}"
        dist = distancia(zona, precio) * 100
        msg = f"{titulo}\n\n"
        msg += f"Centro: {centro} (rango {rango})\n"
        msg += f"Distancia: {dist:.1f}%\n"
        msg += f"{linea_extra}\n"
        msg += f"\nPrecio actual: {fmt(precio)} | Hora: {hora}"
        enviar(msg)

    # Enviar arriba si existe y distancia >= 1%
    if mejor_zona_oi_arriba and distancia(mejor_zona_oi_arriba, precio) >= RADAR1_MIN_DIST:
        enviar_zona(mejor_zona_oi_arriba, "HIGH", True)
        ultima_zona_arriba = mejor_zona_oi_arriba["centro"]
    elif mejor_zona_spot_arriba and distancia(mejor_zona_spot_arriba, precio) >= RADAR1_MIN_DIST:
        enviar_zona(mejor_zona_spot_arriba, "HIGH", False)
        ultima_zona_arriba = mejor_zona_spot_arriba["centro"]
    else:
        ultima_zona_arriba = None

    # Enviar abajo si existe y distancia >= 1%
    if mejor_zona_oi_abajo and distancia(mejor_zona_oi_abajo, precio) >= RADAR1_MIN_DIST:
        enviar_zona(mejor_zona_oi_abajo, "LOW", True)
        ultima_zona_abajo = mejor_zona_oi_abajo["centro"]
    elif mejor_zona_spot_abajo and distancia(mejor_zona_spot_abajo, precio) >= RADAR1_MIN_DIST:
        enviar_zona(mejor_zona_spot_abajo, "LOW", False)
        ultima_zona_abajo = mejor_zona_spot_abajo["centro"]
    else:
        ultima_zona_abajo = None

# =========================
# RADAR 2 (PROXIMIDAD ENTRE 0.3% Y 1%, SOLO ZONAS PRINCIPALES)
# =========================

def radar_proximidad(mejor_zona_arriba, mejor_zona_abajo, precio, hora):
    global last_event_time

    # Solo considerar si coincide con la última zona de Radar 1 (tolerancia 0.1%)
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
                # Mostrar centro redondeado
                titulo = f"🔍 Radar 2 – CERCA {fmt(centro_rd)} ({dist*100:.1f}%) {color_emoji}"
                msg = f"{titulo}\n\nPrecio: {fmt(precio)} | Hora: {hora}"
                enviar(msg)
                last_event_time = ahora
                return True
        return False

    if mejor_zona_arriba:
        check_and_send(mejor_zona_arriba, "🟢", "HIGH")
    if mejor_zona_abajo:
        check_and_send(mejor_zona_abajo, "🔴", "LOW")

# =========================
# RADAR 0 (IMPULSO)
# =========================

def radar_impulse(df_entry, precio_actual):
    global last_impulse_time, last_event_time
    if df_entry.empty or len(df_entry) < 3:
        return

    vela = df_entry.iloc[-1]
    # Asegurar que los datos son numéricos
    try:
        open_price = float(vela["open"])
        close_price = float(vela["close"])
        high = float(vela["high"])
        low = float(vela["low"])
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
    direccion = "alcista" if alcista else "bajista"
    emoji = "🟢" if alcista else "🔴"

    vol1 = volume
    vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]

    prob_lines = []
    prob1 = obtener_probabilidad(vol1, [VOL1_MED, VOL1_P75, VOL1_P90], [PROB1_MED, PROB1_P75, PROB1_P90])
    if prob1:
        prob_lines.append(f"  1 vela ({vol1:.0f} BTC): {prob1}%")

    if len(df_entry) >= 2:
        vela_ant = df_entry.iloc[-2]
        alcista_ant = vela_ant["close"] > vela_ant["open"]
        if alcista == alcista_ant:
            vol2 = vol1 + vela_ant["volume"]
            prob2 = obtener_probabilidad(vol2, [VOL2_MED, VOL2_P75, VOL2_P90], [PROB2_MED, PROB2_P75, PROB2_P90])
            if prob2:
                prob_lines.append(f"  2 velas (misma dir, {vol2:.0f} BTC): {prob2}%")

    if len(df_entry) >= 3:
        vela_ant2 = df_entry.iloc[-3]
        alcista_ant2 = vela_ant2["close"] > vela_ant2["open"]
        if alcista == alcista_ant and alcista == alcista_ant2:
            vol3 = vol1 + df_entry.iloc[-2]["volume"] + df_entry.iloc[-3]["volume"]
            prob3 = obtener_probabilidad(vol3, [VOL3_MED, VOL3_P75, VOL3_P90], [PROB3_MED, PROB3_P75, PROB3_P90])
            if prob3:
                prob_lines.append(f"  3 velas (misma dir, {vol3:.0f} BTC): {prob3}%")

    if not prob_lines:
        prob_lines.append(f"  Volumen insuficiente: probabilidad base 68%")

    titulo = f"🚀 Radar 0 - Impulso {direccion} {emoji}"
    msg = f"{titulo}\n\n"
    msg += f"Precio: {fmt(precio_actual)} - Hora UTC: {ahora.strftime('%H:%M')}\n"
    msg += f"Variación: {price_change:.2f}%\n"
    msg += f"Volumen actual: {vol1:.2f} BTC ({(vol1/vol_medio):.1f}x media)\n\n"
    msg += f"📊 Probabilidad de continuación (sin retroceso):\n"
    msg += "\n".join(prob_lines)

    enviar(msg)
    last_impulse_time = ahora
    last_event_time = ahora

def obtener_probabilidad(volumen, umbrales, probabilidades):
    if volumen > umbrales[2]:
        return probabilidades[2]
    elif volumen > umbrales[1]:
        return probabilidades[1]
    elif volumen > umbrales[0]:
        return probabilidades[0]
    else:
        return None

# =========================
# RADAR 3 (SWEEP)
# =========================

def radar_sweep(df_entry, mejor_zona_arriba, mejor_zona_abajo, precio_actual):
    global sweep_pendiente, last_event_time
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
        ahora = datetime.now(UTC)
        color_zona = "🟢" if tipo_sweep == "HIGH" else "🔴"
        titulo = f"🔄 Radar 3 – SWEEP {tipo_sweep} {color_zona} ({fmt(zona['centro'])})"
        msg = f"{titulo}\n\n"
        msg += f"REVERSIÓN {direccion_rev} {emoji_rev}\n\n"
        msg += f"Precio: {fmt(precio_actual)} | Hora: {ahora.strftime('%H:%M')}"
        enviar(msg)
        last_event_time = ahora
        sweep_pendiente = None
        return
    if len(df_entry) < 2:
        return
    vela_anterior = df_entry.iloc[-2]
    vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]
    if mejor_zona_arriba and vela_anterior["high"] > mejor_zona_arriba.get("max", mejor_zona_arriba["centro"]*1.01) and vela_anterior["close"] < mejor_zona_arriba["centro"]:
        if vela_anterior["volume"] > vol_medio * 1.5:
            sweep_pendiente = (mejor_zona_arriba, "HIGH")
    elif mejor_zona_abajo and vela_anterior["low"] < mejor_zona_abajo.get("min", mejor_zona_abajo["centro"]*0.99) and vela_anterior["close"] > mejor_zona_abajo["centro"]:
        if vela_anterior["volume"] > vol_medio * 1.5:
            sweep_pendiente = (mejor_zona_abajo, "LOW")

# =========================
# RADAR 4 (BREAKOUT)
# =========================

def radar_breakout(df_entry, mejor_zona_arriba, mejor_zona_abajo, precio_actual):
    global zona_consumida, last_event_time, alerted_liquidity
    if df_entry.empty:
        return
    vela = df_entry.iloc[-1]
    close = vela["close"]
    ahora = datetime.now(UTC)
    if mejor_zona_arriba:
        key = ("break", redondear_centro(mejor_zona_arriba["centro"]))
        if key not in alerted_liquidity and close > mejor_zona_arriba.get("max", mejor_zona_arriba["centro"]*1.01) * 1.003:
            alerted_liquidity.add(key)
            zona_consumida = True
            titulo = f"🌋 Radar 4 – BREAKOUT ALCISTA 🟢 ({fmt(mejor_zona_arriba['centro'])})"
            msg = f"{titulo}\n\n"
            msg += f"Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}"
            enviar(msg)
            last_event_time = ahora
            return
    if mejor_zona_abajo:
        key = ("break", redondear_centro(mejor_zona_abajo["centro"]))
        if key not in alerted_liquidity and close < mejor_zona_abajo.get("min", mejor_zona_abajo["centro"]*0.99) * 0.997:
            alerted_liquidity.add(key)
            zona_consumida = True
            titulo = f"🌋 Radar 4 – BREAKOUT BAJISTA 🔴 ({fmt(mejor_zona_abajo['centro'])})"
            msg = f"{titulo}\n\n"
            msg += f"Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}"
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
        msg = f"🤖 BOT DE ARTURO FUNCIONANDO (V11.2)\nHora UTC: {ahora.strftime('%H:%M')}\nPrecio: {fmt(precio)}"
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

    df_spot_macro = obtener_candles_spot(INTERVAL_MACRO)
    df_entry = obtener_candles_spot(INTERVAL_ENTRY)
    df_oi = obtener_open_interest_hist(period="5m", limit=OI_LOOKBACK*3)
    precio = obtener_precio_actual()

    if df_entry.empty or precio is None:
        return

    # Detectar zonas spot
    zonas_high_spot, zonas_low_spot = detectar_zonas_spot(df_spot_macro) if not df_spot_macro.empty else ([], [])
    mejor_spot_arriba, mejor_spot_abajo = seleccionar_mejores_zonas_spot(zonas_high_spot, zonas_low_spot, precio)

    # Detectar zonas OI (obtenemos lista de clusters)
    clusters_oi = detectar_zonas_oi(df_oi, df_spot_macro) if not df_oi.empty else []
    # Separar arriba/abajo según precio actual
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

    # Definir zona actual (prioridad OI)
    centro_arriba = mejor_oi_arriba["centro"] if mejor_oi_arriba else (mejor_spot_arriba["centro"] if mejor_spot_arriba else None)
    centro_abajo = mejor_oi_abajo["centro"] if mejor_oi_abajo else (mejor_spot_abajo["centro"] if mejor_spot_abajo else None)
    nueva_zona = (centro_arriba, centro_abajo)

    # Enviar Radar 1 si la zona cambia significativamente (usando misma_zona)
    if not misma_zona(zona_actual, nueva_zona):
        zona_actual = nueva_zona
        zona_consumida = False
        alerted_liquidity.clear()
        if last_mapa_time is None or (ahora - last_mapa_time) > timedelta(minutes=MAPA_COOLDOWN_MINUTOS):
            enviar_liquidez_detectada(mejor_oi_arriba, mejor_oi_abajo, mejor_spot_arriba, mejor_spot_abajo, precio, hora_str)
            last_mapa_time = ahora
        last_event_time = ahora

    # Limpieza de timestamps antiguos en proximidad
    keys_a_remover = []
    for key, ts in alerted_proximidad.items():
        if (ahora - ts) > timedelta(hours=2):
            keys_a_remover.append(key)
    for key in keys_a_remover:
        alerted_proximidad.pop(key, None)

    # Zonas de referencia para radares (prioridad OI)
    zona_ref_arriba = mejor_oi_arriba if mejor_oi_arriba else mejor_spot_arriba
    zona_ref_abajo = mejor_oi_abajo if mejor_oi_abajo else mejor_spot_abajo

    # Radar 2
    radar_proximidad(zona_ref_arriba, zona_ref_abajo, precio, hora_str)

    # Radar 0
    radar_impulse(df_entry, precio)

    # Radar 3
    radar_sweep(df_entry, zona_ref_arriba, zona_ref_abajo, precio)

    # Radar 4
    radar_breakout(df_entry, zona_ref_arriba, zona_ref_abajo, precio)

    # Sistema
    heartbeat()
    sin_eventos()

# =========================
# INICIO
# =========================

if __name__ == "__main__":
    print("🚀 Iniciando BOT V11.2 (con mejoras de coherencia y OI)...")
    precio_inicial = obtener_precio_actual()
    hora_actual = datetime.now(UTC).strftime('%H:%M')
    msg = f"🤖 BOT DE ARTURO FUNCIONANDO (V11.2)\nHora UTC: {hora_actual}\nPrecio: {fmt(precio_inicial)}"
    enviar(msg)

    last_heartbeat_time = datetime.now(UTC)
    last_event_time = datetime.now(UTC)
    last_mapa_time = None

    while True:
        try:
            evaluar()
        except Exception as e:
            print(f"Error en ciclo principal: {e}")
            enviar(f"⚠️ ERROR: {str(e)[:100]}")
            time.sleep(60)
        time.sleep(60)
