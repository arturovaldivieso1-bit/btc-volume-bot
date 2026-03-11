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
PROXIMITY = 0.003
ZONA_EQUIVALENTE = 0.002

IMPULSE_RANGE = 1.5
IMPULSE_VOLUME = 1.3
IMPULSE_LOOKBACK = 12
IMPULSE_COOLDOWN = 300

HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6
MAPA_COOLDOWN_MINUTOS = 30

# Variables de estado
last_impulse_time = None
last_heartbeat_time = None
last_event_time = None
last_mapa_time = None
zona_actual = None
zona_alertada_proximidad = False
zona_consumida = False
alerted_liquidity = set()
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

def formatear_liquidez(zona, precio, es_arriba=True, incluir_distancia=True, mostrar_distancia=True):
    if zona is None:
        return ""
    linea = f"{'🟢' if es_arriba else '🔴'} "
    linea += f"{'ARRIBA' if es_arriba else 'ABAJO'}: {fmt(zona['centro'])}"
    if incluir_distancia and mostrar_distancia:
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

def radar_impulse(df_entry, precio_actual, mejor_arriba, mejor_abajo):
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
    # Título con color
    titulo = f"{emoji} IMPULSO {direccion} DETECTADO"
    msg = f"{titulo}\n\n"
    msg += f"Precio: {fmt(precio_actual)}\n"
    msg += f"Volumen: {vol_actual:.2f} BTC ({(vol_actual/vol_medio):.1f}x media)\n"
    msg += f"Hora UTC: {ahora.strftime('%H:%M')}\n"
    # No incluimos líneas de liquidez aquí (por petición de Arturo)
    enviar(msg)
    last_impulse_time = ahora
    last_event_time = ahora

def radar_sweep(df_entry, mejor_arriba, mejor_abajo, precio_actual):
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
        titulo = f"{emoji} RADAR 3 – SWEEP REVERSIÓN {direccion}"
        msg = f"{titulo}\n\n"
        msg += f"Precio: {fmt(precio_actual)}\n"
        msg += f"Hora UTC: {ahora.strftime('%H:%M')}\n"
        msg += f"Dirección probable: {emoji} {direccion}\n"
        # Añadir liquidez opuesta como referencia rápida (opcional, pero puede ser útil)
        if tipo_sweep == "HIGH" and mejor_abajo:
            msg += f"\n{formatear_liquidez(mejor_abajo, precio_actual, es_arriba=False, mostrar_distancia=False)}"
        elif tipo_sweep == "LOW" and mejor_arriba:
            msg += f"\n{formatear_liquidez(mejor_arriba, precio_actual, es_arriba=True, mostrar_distancia=False)}"
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
            titulo = f"🟢 RADAR 4 – BREAKOUT ALCISTA"
            msg = f"{titulo}\n\n"
            msg += f"Precio: {fmt(close)}\n"
            msg += f"Liquidez arriba consumida: {fmt(mejor_arriba['centro'])}\n"
            msg += f"Hora UTC: {ahora.strftime('%H:%M')}\n"
            if mejor_abajo:
                msg += f"\n{formatear_liquidez(mejor_abajo, close, es_arriba=False, mostrar_distancia=False)}"
            enviar(msg)
            last_event_time = ahora
            return
    if mejor_abajo:
        key = ("break", round(mejor_abajo["centro"]))
        if key not in alerted_liquidity and close < mejor_abajo["min"] * 0.997:
            alerted_liquidity.add(key)
            zona_consumida = True
            titulo = f"🔴 RADAR 4 – BREAKOUT BAJISTA"
            msg = f"{titulo}\n\n"
            msg += f"Precio: {fmt(close)}\n"
            msg += f"Liquidez abajo consumida: {fmt(mejor_abajo['centro'])}\n"
            msg += f"Hora UTC: {ahora.strftime('%H:%M')}\n"
            if mejor_arriba:
                msg += f"\n{formatear_liquidez(mejor_arriba, close, es_arriba=True, mostrar_distancia=False)}"
            enviar(msg)
            last_event_time = ahora
            return

# =========================
# ALERTAS DE SISTEMA
# =========================

def enviar_liquidez_detectada(mejor_arriba, mejor_abajo, precio, titulo="📡 RADAR 1 – LIQUIDEZ DETECTADA"):
    msg = f"{titulo}\n\nPrecio actual: {fmt(precio)}\n"
    if mejor_arriba:
        msg += "\n" + formatear_liquidez(mejor_arriba, precio, es_arriba=True)
    if mejor_abajo:
        msg += "\n" + formatear_liquidez(mejor_abajo, precio, es_arriba=False)
    enviar(msg)

def heartbeat():
    global last_heartbeat_time
    ahora = datetime.now(UTC)
    if last_heartbeat_time is None:
        last_heartbeat_time = ahora
        return
    if (ahora - last_heartbeat_time) > timedelta(hours=HEARTBEAT_HOURS):
        precio = obtener_precio_actual() or 0
        msg = f"💓 HEARTBEAT BOT ACTIVO\nHora UTC: {ahora.strftime('%H:%M')}\nActivo: {SYMBOL}\nPrecio: {fmt(precio)}"
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
        msg = f"⚠️ MERCADO SIN EVENTOS RELEVANTES\nTiempo sin señales: {NO_EVENT_HOURS}h\nPrecio actual: {fmt(precio)}\nEstado: lateral / baja volatilidad"
        enviar(msg)
        last_event_time = ahora

# =========================
# FUNCIÓN PRINCIPAL
# =========================

def evaluar():
    global zona_actual, zona_alertada_proximidad, zona_consumida, alerted_liquidity, last_event_time, last_mapa_time

    ahora = datetime.now(UTC)

    df_macro = obtener_candles(INTERVAL_MACRO)
    df_entry = obtener_candles(INTERVAL_ENTRY)
    precio = obtener_precio_actual()
    if df_macro.empty or df_entry.empty or precio is None:
        return

    zonas_high, zonas_low = detectar_zonas(df_macro)
    mejor_arriba, mejor_abajo = seleccionar_mejores_zonas(zonas_high, zonas_low, precio)

    # Definir zona actual como tupla de centros
    centro_arriba = mejor_arriba["centro"] if mejor_arriba else None
    centro_abajo = mejor_abajo["centro"] if mejor_abajo else None
    nueva_zona = (centro_arriba, centro_abajo)

    # Si cambió la zona significativamente, enviar RADAR 1 (con cooldown)
    if not misma_zona(zona_actual, nueva_zona):
        zona_actual = nueva_zona
        zona_alertada_proximidad = False
        zona_consumida = False
        alerted_liquidity.clear()
        if last_mapa_time is None or (ahora - last_mapa_time) > timedelta(minutes=MAPA_COOLDOWN_MINUTOS):
            enviar_liquidez_detectada(mejor_arriba, mejor_abajo, precio)
            last_mapa_time = ahora
        last_event_time = ahora

    # RADAR 2 - Proximidad (solo si no se ha alertado ya y estamos cerca)
    cerca_arriba = mejor_arriba and abs(precio - mejor_arriba["centro"]) / precio < PROXIMITY
    cerca_abajo = mejor_abajo and abs(precio - mejor_abajo["centro"]) / precio < PROXIMITY
    if not (cerca_arriba or cerca_abajo):
        zona_alertada_proximidad = False

    if not zona_alertada_proximidad:
        if cerca_arriba:
            key = ("prox", round(mejor_arriba["centro"]))
            if key not in alerted_liquidity:
                alerted_liquidity.add(key)
                dist = abs(precio - mejor_arriba["centro"]) / precio * 100
                msg = f"🔍 RADAR 2 – PROXIMIDAD\n\n"
                msg += f"Precio: {fmt(precio)}\n"
                msg += f"Nivel: {fmt(mejor_arriba['centro'])} ({dist:.2f}%) | {mejor_arriba['toques']} toques"
                if mejor_arriba["toques"] >= 5:
                    msg += " 🔥"
                if mejor_abajo:
                    msg += "\n\n" + formatear_liquidez(mejor_abajo, precio, es_arriba=False, mostrar_distancia=False)
                enviar(msg)
                last_event_time = ahora
                zona_alertada_proximidad = True
        elif cerca_abajo:
            key = ("prox", round(mejor_abajo["centro"]))
            if key not in alerted_liquidity:
                alerted_liquidity.add(key)
                dist = abs(precio - mejor_abajo["centro"]) / precio * 100
                msg = f"🔍 RADAR 2 – PROXIMIDAD\n\n"
                msg += f"Precio: {fmt(precio)}\n"
                msg += f"Nivel: {fmt(mejor_abajo['centro'])} ({dist:.2f}%) | {mejor_abajo['toques']} toques"
                if mejor_abajo["toques"] >= 5:
                    msg += " 🔥"
                if mejor_arriba:
                    msg += "\n\n" + formatear_liquidez(mejor_arriba, precio, es_arriba=True, mostrar_distancia=False)
                enviar(msg)
                last_event_time = ahora
                zona_alertada_proximidad = True

    # RADAR 0
    radar_impulse(df_entry, precio, mejor_arriba, mejor_abajo)

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
    print("🚀 Iniciando BOT V10.7...")
    precio_inicial = obtener_precio_actual()
    df_temp = obtener_candles(INTERVAL_MACRO, limit=LOOKBACK)
    if not df_temp.empty and precio_inicial:
        zh, zl = detectar_zonas(df_temp)
        ma, mb = seleccionar_mejores_zonas(zh, zl, precio_inicial)
        msg = f"🟢 BOT STOP HUNT ENGINE V10.7 ONLINE\n"
        msg += f"Activo: {SYMBOL} | Estructura: {INTERVAL_MACRO} | Eventos: {INTERVAL_ENTRY}\n"
        msg += f"Lookback: {LOOKBACK} velas | Min toques: {MIN_TOUCHES} | Cluster: {CLUSTER_RANGE*100:.2f}%\n\n"
        msg += f"Precio actual: {fmt(precio_inicial)}\n"
        if ma:
            msg += "\n" + formatear_liquidez(ma, precio_inicial, es_arriba=True)
        if mb:
            msg += "\n" + formatear_liquidez(mb, precio_inicial, es_arriba=False)
        enviar(msg)
    else:
        enviar("🟢 BOT STOP HUNT ENGINE V10.7 ONLINE (sin datos iniciales)")

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
