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

INTERVAL_MACRO = "1h"
INTERVAL_ENTRY = "5m"

LOOKBACK = 168
MIN_TOUCHES = 3
CLUSTER_RANGE = 0.0025
PROXIMITY = 0.003          # 0.3% - lo dejamos igual, pero podemos ajustar
ZONA_EQUIVALENTE = 0.003    # 0.3% - aumentado para unificar zonas cercanas

IMPULSE_RANGE = 1.5
IMPULSE_VOLUME = 1.3
IMPULSE_LOOKBACK = 12
IMPULSE_COOLDOWN = 300

HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6
MAPA_COOLDOWN_MINUTOS = 60  # Aumentado a 60 minutos

# Variables de estado
last_impulse_time = None
last_heartbeat_time = None
last_event_time = None
last_mapa_time = None
zona_actual = None           # Tupla (centro_arriba, centro_abajo)
zona_consumida = False
alerted_liquidity = set()    # Para breakouts y sweeps
alerted_proximidad = set()   # Para controlar alertas de proximidad por zona
sweep_pendiente = None

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

def obtener_candles(interval, limit=200):
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
        print(f"Error Binance: {e}")
        return pd.DataFrame()

def obtener_precio_actual():
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
        r = requests.get(url, timeout=10)
        return float(r.json()["price"])
    except:
        return None

def cluster(lista):
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

def detectar_zonas(df):
    if df.empty or len(df) < LOOKBACK:
        return [], []
    highs = df["high"].tail(LOOKBACK).tolist()
    lows = df["low"].tail(LOOKBACK).tolist()
    clusters_high = cluster(highs)
    clusters_low = cluster(lows)
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

def seleccionar_mejores_zonas(zonas_high, zonas_low, precio):
    arriba = [z for z in zonas_high if z["centro"] > precio] + [z for z in zonas_low if z["centro"] > precio]
    abajo = [z for z in zonas_low if z["centro"] < precio] + [z for z in zonas_high if z["centro"] < precio]

    for z in arriba:
        z["distancia"] = z["centro"] - precio
        z["score"] = z["toques"] * 1000 - z["distancia"]
    for z in abajo:
        z["distancia"] = precio - z["centro"]
        z["score"] = z["toques"] * 1000 - z["distancia"]

    arriba.sort(key=lambda x: x["score"], reverse=True)
    abajo.sort(key=lambda x: x["score"], reverse=True)

    mejor_arriba = arriba[0] if arriba else None
    mejor_abajo = abajo[0] if abajo else None
    return mejor_arriba, mejor_abajo

def formatear_liquidez_simple(zona, precio):
    """Formato para RADAR 1: solo nivel, distancia y toques"""
    if zona is None:
        return ""
    dist = abs(zona["centro"] - precio) / precio * 100
    linea = f"{fmt(zona['centro'])} ({dist:.1f}%) | {zona['toques']} toques"
    if zona["toques"] >= 5:
        linea += " 🔥"
    return linea

def formatear_liquidez_radar2(zona, precio, es_arriba, mostrar_distancia=True):
    """Formato para RADAR 2: con flecha y distancia opcional"""
    if zona is None:
        return ""
    flecha = "⬆" if es_arriba else "⬇"
    linea = f"{'🟢' if es_arriba else '🔴'} {flecha} {fmt(zona['centro'])}"
    if mostrar_distancia:
        dist = abs(zona["centro"] - precio) / precio * 100
        linea += f" ({dist:.1f}%)"
    linea += f" | {zona['toques']} toques"
    if zona["toques"] >= 5:
        linea += " 🔥"
    return linea

def misma_zona(z1, z2):
    if z1 is None or z2 is None:
        return False
    c1_arriba, c1_abajo = z1
    c2_arriba, c2_abajo = z2
    if c1_arriba is None or c2_arriba is None or c1_abajo is None or c2_abajo is None:
        return False
    diff_arriba = abs(c1_arriba - c2_arriba) / c1_arriba
    diff_abajo = abs(c1_abajo - c2_abajo) / c1_abajo
    return diff_arriba < ZONA_EQUIVALENTE and diff_abajo < ZONA_EQUIVALENTE

# =========================
# RADARES
# =========================

def radar_impulse(df_entry, precio_actual):
    """🚀 RADAR 0 – IMPULSO (con color según dirección)"""
    global last_impulse_time, last_event_time
    if df_entry.empty or len(df_entry) < max(20, IMPULSE_LOOKBACK + 1):
        return
    vela = df_entry.iloc[-1]
    rango = (vela["high"] - vela["low"]) / vela["close"] * 100
    rango_medio = ((df_entry["high"] - df_entry["low"]) / df_entry["close"]).rolling(20).mean().iloc[-1] * 100
    vol_actual = vela["volume"]
    vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]
    if rango < IMPULSE_RANGE * rango_medio or vol_actual < IMPULSE_VOLUME * vol_medio:
        return
    ventana = df_entry.iloc[-IMPULSE_LOOKBACK-1:-1]
    max_anterior = ventana["high"].max()
    min_anterior = ventana["low"].min()
    if vela["close"] > vela["open"]:
        if vela["high"] <= max_anterior:
            return
        direccion = "ALCISTA"
        emoji = "🟢"
    else:
        if vela["low"] >= min_anterior:
            return
        direccion = "BAJISTA"
        emoji = "🔴"
    ahora = datetime.now(UTC)
    if last_impulse_time and (ahora - last_impulse_time).seconds < IMPULSE_COOLDOWN:
        return
    titulo = f"🚀 RADAR 0 – IMPULSO {direccion} {emoji}"
    msg = f"{titulo}\n\n"
    msg += f"Precio: {fmt(precio_actual)}\n"
    msg += f"Volumen: {vol_actual:.2f} BTC ({(vol_actual/vol_medio):.1f}x media)\n"
    msg += f"Hora UTC: {ahora.strftime('%H:%M')}\n"
    enviar(msg)
    last_impulse_time = ahora
    last_event_time = ahora

def radar_sweep(df_entry, mejor_arriba, mejor_abajo, precio_actual):
    """🔄 RADAR 3 – SWEEP REVERSIÓN (con color)"""
    global sweep_pendiente, last_event_time
    if df_entry.empty or len(df_entry) < 2:
        return
    vela_actual = df_entry.iloc[-1]
    if sweep_pendiente:
        zona, tipo_sweep = sweep_pendiente
        if tipo_sweep == "HIGH" and vela_actual["close"] < vela_actual["open"]:
            direccion = "BAJISTA"
            emoji = "🔴"
        elif tipo_sweep == "LOW" and vela_actual["close"] > vela_actual["open"]:
            direccion = "ALCISTA"
            emoji = "🟢"
        else:
            sweep_pendiente = None
            return
        ahora = datetime.now(UTC)
        titulo = f"🔄 RADAR 3 – SWEEP REVERSIÓN {direccion} {emoji}"
        msg = f"{titulo}\n\n"
        msg += f"Precio: {fmt(precio_actual)}\n"
        msg += f"Hora UTC: {ahora.strftime('%H:%M')}\n"
        msg += f"Dirección probable: {emoji} {direccion}\n"
        if tipo_sweep == "HIGH" and mejor_abajo:
            msg += f"\n🔻 Liquidez abajo: {formatear_liquidez_radar2(mejor_abajo, precio_actual, es_arriba=False, mostrar_distancia=False)}"
        elif tipo_sweep == "LOW" and mejor_arriba:
            msg += f"\n🔼 Liquidez arriba: {formatear_liquidez_radar2(mejor_arriba, precio_actual, es_arriba=True, mostrar_distancia=False)}"
        enviar(msg)
        last_event_time = ahora
        sweep_pendiente = None
        return
    if len(df_entry) < 2:
        return
    vela_anterior = df_entry.iloc[-2]
    vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]
    if mejor_arriba and mejor_arriba["tipo"] == "HIGH" and vela_anterior["high"] > mejor_arriba["max"] and vela_anterior["close"] < mejor_arriba["centro"]:
        if vela_anterior["volume"] > vol_medio * 1.5:
            sweep_pendiente = (mejor_arriba, "HIGH")
    elif mejor_abajo and mejor_abajo["tipo"] == "LOW" and vela_anterior["low"] < mejor_abajo["min"] and vela_anterior["close"] > mejor_abajo["centro"]:
        if vela_anterior["volume"] > vol_medio * 1.5:
            sweep_pendiente = (mejor_abajo, "LOW")

def radar_breakout(df_entry, mejor_arriba, mejor_abajo, precio_actual):
    """🌋 RADAR 4 – BREAKOUT (con color)"""
    global zona_consumida, last_event_time, alerted_liquidity
    if df_entry.empty:
        return
    vela = df_entry.iloc[-1]
    close = vela["close"]
    ahora = datetime.now(UTC)
    if mejor_arriba:
        key = ("break", round(mejor_arriba["centro"]))
        if key not in alerted_liquidity and close > mejor_arriba["max"] * 1.003:
            alerted_liquidity.add(key)
            zona_consumida = True
            titulo = f"🌋 RADAR 4 – BREAKOUT ALCISTA 🟢"
            msg = f"{titulo}\n\n"
            msg += f"Precio: {fmt(close)}\n"
            msg += f"Liquidez arriba consumida: {fmt(mejor_arriba['centro'])}\n"
            msg += f"Hora UTC: {ahora.strftime('%H:%M')}\n"
            if mejor_abajo:
                msg += f"\n🔻 Liquidez abajo: {formatear_liquidez_radar2(mejor_abajo, close, es_arriba=False, mostrar_distancia=False)}"
            enviar(msg)
            last_event_time = ahora
            return
    if mejor_abajo:
        key = ("break", round(mejor_abajo["centro"]))
        if key not in alerted_liquidity and close < mejor_abajo["min"] * 0.997:
            alerted_liquidity.add(key)
            zona_consumida = True
            titulo = f"🌋 RADAR 4 – BREAKOUT BAJISTA 🔴"
            msg = f"{titulo}\n\n"
            msg += f"Precio: {fmt(close)}\n"
            msg += f"Liquidez abajo consumida: {fmt(mejor_abajo['centro'])}\n"
            msg += f"Hora UTC: {ahora.strftime('%H:%M')}\n"
            if mejor_arriba:
                msg += f"\n🔼 Liquidez arriba: {formatear_liquidez_radar2(mejor_arriba, close, es_arriba=True, mostrar_distancia=False)}"
            enviar(msg)
            last_event_time = ahora
            return

# =========================
# ALERTAS DE SISTEMA
# =========================

def enviar_liquidez_detectada(mejor_arriba, mejor_abajo, precio, hora):
    """📡 RADAR 1 – LIQUIDEZ DETECTADA (HIGH/LOW con color)"""
    if mejor_arriba:
        titulo = f"📡 RADAR 1 – LIQUIDEZ DETECTADA HIGH 🟢"
        nivel = formatear_liquidez_simple(mejor_arriba, precio)
        msg = f"{titulo}\n\nPrecio actual: {fmt(precio)}\nHora UTC: {hora}\n\nNivel: {nivel}"
        enviar(msg)
    if mejor_abajo:
        titulo = f"📡 RADAR 1 – LIQUIDEZ DETECTADA LOW 🔴"
        nivel = formatear_liquidez_simple(mejor_abajo, precio)
        msg = f"{titulo}\n\nPrecio actual: {fmt(precio)}\nHora UTC: {hora}\n\nNivel: {nivel}"
        enviar(msg)

def heartbeat():
    global last_heartbeat_time
    ahora = datetime.now(UTC)
    if last_heartbeat_time is None:
        last_heartbeat_time = ahora
        return
    if (ahora - last_heartbeat_time) > timedelta(hours=HEARTBEAT_HOURS):
        precio = obtener_precio_actual() or 0
        msg = f"🤖 BOT DE ARTURO FUNCIONANDO\nHora UTC: {ahora.strftime('%H:%M')}\nPrecio: {fmt(precio)}"
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
        msg = f"⚠️ MERCADO SIN EVENTOS RELEVANTES\nTiempo sin señales: {NO_EVENT_HOURS}h\nPrecio actual: {fmt(precio)}\nHora UTC: {ahora.strftime('%H:%M')}\nEstado: lateral / baja volatilidad"
        enviar(msg)
        last_event_time = ahora

# =========================
# FUNCIÓN PRINCIPAL
# =========================

def evaluar():
    global zona_actual, zona_consumida, alerted_liquidity, alerted_proximidad, last_event_time, last_mapa_time

    ahora = datetime.now(UTC)
    hora_str = ahora.strftime('%H:%M')

    df_macro = obtener_candles(INTERVAL_MACRO)
    df_entry = obtener_candles(INTERVAL_ENTRY)
    precio = obtener_precio_actual()
    if df_macro.empty or df_entry.empty or precio is None:
        return

    zonas_high, zonas_low = detectar_zonas(df_macro)
    mejor_arriba, mejor_abajo = seleccionar_mejores_zonas(zonas_high, zonas_low, precio)

    centro_arriba = mejor_arriba["centro"] if mejor_arriba else None
    centro_abajo = mejor_abajo["centro"] if mejor_abajo else None
    nueva_zona = (centro_arriba, centro_abajo)

    # Si la zona cambió significativamente, actualizar y enviar RADAR 1 (con cooldown)
    if not misma_zona(zona_actual, nueva_zona):
        zona_actual = nueva_zona
        zona_consumida = False
        alerted_liquidity.clear()
        alerted_proximidad.clear()  # Reiniciar alertas de proximidad al cambiar la zona
        if last_mapa_time is None or (ahora - last_mapa_time) > timedelta(minutes=MAPA_COOLDOWN_MINUTOS):
            enviar_liquidez_detectada(mejor_arriba, mejor_abajo, precio, hora_str)
            last_mapa_time = ahora
        last_event_time = ahora

    # Limpiar alertas de proximidad para zonas que ya no están cerca
    zonas_a_remover = []
    for key in alerted_proximidad:
        # key es el centro de la zona (redondeado)
        # Verificar si esa zona sigue siendo la mejor o al menos existe
        # Simplificamos: si la distancia actual es > PROXIMITY, la removemos
        zona_encontrada = None
        if mejor_arriba and round(mejor_arriba["centro"]) == key:
            zona_encontrada = mejor_arriba
        elif mejor_abajo and round(mejor_abajo["centro"]) == key:
            zona_encontrada = mejor_abajo
        if zona_encontrada:
            dist = abs(precio - zona_encontrada["centro"]) / precio
            if dist >= PROXIMITY:
                zonas_a_remover.append(key)
        else:
            # Si la zona ya no está en las mejores, también la removemos
            zonas_a_remover.append(key)
    for key in zonas_a_remover:
        alerted_proximidad.discard(key)

    # RADAR 2 - Proximidad (solo para la mejor zona que esté cerca y no alertada)
    if mejor_arriba and (round(mejor_arriba["centro"]) not in alerted_proximidad):
        dist = abs(precio - mejor_arriba["centro"]) / precio
        if dist < PROXIMITY:
            alerted_proximidad.add(round(mejor_arriba["centro"]))
            msg = f"🔍 RADAR 2 – PROXIMIDAD\n\n"
            msg += f"Precio: {fmt(precio)}\n"
            msg += f"Hora UTC: {hora_str}\n"
            msg += f"Nivel: {fmt(mejor_arriba['centro'])} ({dist*100:.2f}%) | {mejor_arriba['toques']} toques"
            if mejor_arriba["toques"] >= 5:
                msg += " 🔥"
            if mejor_abajo:
                msg += f"\n\n🔻 Liquidez abajo: {formatear_liquidez_radar2(mejor_abajo, precio, es_arriba=False, mostrar_distancia=False)}"
            enviar(msg)
            last_event_time = ahora

    if mejor_abajo and (round(mejor_abajo["centro"]) not in alerted_proximidad):
        dist = abs(precio - mejor_abajo["centro"]) / precio
        if dist < PROXIMITY:
            alerted_proximidad.add(round(mejor_abajo["centro"]))
            msg = f"🔍 RADAR 2 – PROXIMIDAD\n\n"
            msg += f"Precio: {fmt(precio)}\n"
            msg += f"Hora UTC: {hora_str}\n"
            msg += f"Nivel: {fmt(mejor_abajo['centro'])} ({dist*100:.2f}%) | {mejor_abajo['toques']} toques"
            if mejor_abajo["toques"] >= 5:
                msg += " 🔥"
            if mejor_arriba:
                msg += f"\n\n🔼 Liquidez arriba: {formatear_liquidez_radar2(mejor_arriba, precio, es_arriba=True, mostrar_distancia=False)}"
            enviar(msg)
            last_event_time = ahora

    # RADAR 0
    radar_impulse(df_entry, precio)

    # RADAR 3
    radar_sweep(df_entry, mejor_arriba, mejor_abajo, precio)

    # RADAR 4
    radar_breakout(df_entry, mejor_arriba, mejor_abajo, precio)

    # Alertas de sistema
    heartbeat()
    sin_eventos()

# =========================
# INICIO
# =========================

if __name__ == "__main__":
    print("🚀 Iniciando BOT V10.10...")
    precio_inicial = obtener_precio_actual()
    hora_actual = datetime.now(UTC).strftime('%H:%M')
    df_temp = obtener_candles(INTERVAL_MACRO, limit=LOOKBACK)
    if not df_temp.empty and precio_inicial:
        zh, zl = detectar_zonas(df_temp)
        ma, mb = seleccionar_mejores_zonas(zh, zl, precio_inicial)
        msg = f"🤖 BOT DE ARTURO FUNCIONANDO\n"
        msg += f"Hora UTC: {hora_actual}\n"
        msg += f"Precio: {fmt(precio_inicial)}\n"
        if ma:
            msg += f"\n🔼 Liquidez arriba: {formatear_liquidez_radar2(ma, precio_inicial, es_arriba=True)}"
        if mb:
            msg += f"\n🔻 Liquidez abajo: {formatear_liquidez_radar2(mb, precio_inicial, es_arriba=False)}"
        enviar(msg)
    else:
        enviar("🤖 BOT DE ARTURO FUNCIONANDO (sin datos iniciales)")

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
