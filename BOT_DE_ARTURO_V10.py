# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os
from datetime import datetime, timedelta, UTC

# =========================
# CONFIGURACIÓN INICIAL (Ajusta estos valores)
# =========================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
SYMBOL = "BTCUSDT"

# Timeframes
INTERVAL_MACRO = "1h"      # Para estructura de liquidez
INTERVAL_ENTRY = "5m"      # Para eventos

# Parámetros de liquidez
LOOKBACK = 168              # 7 días en 1h
MIN_TOUCHES = 3             # Mínimo toques para zona válida
CLUSTER_RANGE = 0.0025      # 0.25% para agrupar
PROXIMITY = 0.003           # 0.3% para alerta de cercanía
ZONA_EQUIVALENTE = 0.002    # 0.2% para considerar misma zona (menos sensible)

# Radar 0 - Impulso
IMPULSE_RANGE = 1.5
IMPULSE_VOLUME = 1.3
IMPULSE_LOOKBACK = 12
IMPULSE_COOLDOWN = 300      # 5 min

# Sistema
HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6
MAPA_COOLDOWN_MINUTOS = 30  # No enviar mapa más de una vez cada 30 min

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

def seleccionar_zonas_relevantes(zonas_high, zonas_low, precio):
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
    return arriba[:2], abajo[:2]

def formatear_liquidez(zona, precio, es_arriba=True, incluir_distancia=True):
    linea = f"{'🟢' if es_arriba else '🔴'} Liquidez {'arriba' if es_arriba else 'abajo'}: {fmt(zona['centro'])}"
    if incluir_distancia:
        dist = abs(zona["centro"] - precio) / precio * 100
        linea += f" ({dist:.1f}%)"
    linea += f" | {zona['toques']} toques"
    if zona["toques"] >= 5:
        linea += " 🔥"
    return linea

def misma_zona(z1, z2):
    if z1 is None or z2 is None:
        return False
    # Comparamos los centros de las zonas principales (arriba y abajo)
    # Para simplificar, consideramos que si la primera zona arriba y abajo son similares, es la misma
    if len(z1) != 2 or len(z2) != 2:
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

def radar_impulse(df_entry, precio_actual, zonas_arriba, zonas_abajo):
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
    # Ver si está cerca de alguna zona
    cerca = None
    for z in zonas_arriba + zonas_abajo:
        if abs(precio_actual - z["centro"]) / precio_actual < PROXIMITY:
            tipo = "arriba" if z["centro"] > precio_actual else "abajo"
            cerca = f"cerca de liquidez {tipo}"
            break
    titulo = f"⚡ IMPULSO {direccion} DETECTADO"
    if cerca:
        titulo = f"⚡ IMPULSO {direccion} {cerca.upper()}"
    msg = f"{titulo}\n\nPrecio: {fmt(precio_actual)}\nVolumen: {vol_actual:.2f} BTC ({(vol_actual/vol_medio):.1f}x media)\n"
    # Solo mostramos la zona más cercana arriba y abajo para no saturar
    if zonas_arriba:
        msg += "\n" + formatear_liquidez(zonas_arriba[0], precio_actual, es_arriba=True)
    if zonas_abajo:
        msg += "\n" + formatear_liquidez(zonas_abajo[0], precio_actual, es_arriba=False)
    enviar(msg)
    last_impulse_time = ahora
    last_event_time = ahora

def radar_sweep(df_entry, zonas_arriba, zonas_abajo, precio_actual):
    global sweep_pendiente, last_event_time
    if df_entry.empty or len(df_entry) < 2:
        return
    vela_actual = df_entry.iloc[-1]
    if sweep_pendiente:
        zona, tipo_sweep = sweep_pendiente
        if tipo_sweep == "HIGH" and vela_actual["close"] < vela_actual["open"]:
            direccion = "🔻 BAJISTA"
        elif tipo_sweep == "LOW" and vela_actual["close"] > vela_actual["open"]:
            direccion = "🔺 ALCISTA"
        else:
            sweep_pendiente = None
            return
        msg = f"🚨 SWEEP DE LIQUIDEZ {'ARRIBA' if tipo_sweep=='HIGH' else 'ABAJO'} CONFIRMADO\n\n"
        msg += f"Precio: {fmt(precio_actual)}\nDirección probable: {direccion}\n"
        if zonas_arriba:
            msg += "\n" + formatear_liquidez(zonas_arriba[0], precio_actual, es_arriba=True)
        if zonas_abajo:
            msg += "\n" + formatear_liquidez(zonas_abajo[0], precio_actual, es_arriba=False)
        enviar(msg)
        last_event_time = datetime.now(UTC)
        sweep_pendiente = None
        return
    vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]
    vela = df_entry.iloc[-2] if len(df_entry) >= 2 else None
    if vela is None:
        return
    for z in zonas_arriba + zonas_abajo:
        if z["tipo"] == "HIGH" and vela["high"] > z["max"] and vela["close"] < z["centro"]:
            if vela["volume"] > vol_medio * 1.5:
                sweep_pendiente = (z, "HIGH")
                break
        elif z["tipo"] == "LOW" and vela["low"] < z["min"] and vela["close"] > z["centro"]:
            if vela["volume"] > vol_medio * 1.5:
                sweep_pendiente = (z, "LOW")
                break

def radar_breakout(df_entry, zonas_arriba, zonas_abajo, precio_actual):
    global zona_consumida, last_event_time, alerted_liquidity
    if df_entry.empty:
        return
    vela = df_entry.iloc[-1]
    close = vela["close"]
    for z in zonas_arriba:
        key = ("break", round(z["centro"]))
        if key in alerted_liquidity:
            continue
        if close > z["max"] * 1.003:
            alerted_liquidity.add(key)
            zona_consumida = True
            msg = f"📡 BREAKOUT ALCISTA CONFIRMADO\n\nLiquidez arriba consumida: {fmt(z['centro'])}\nPrecio: {fmt(close)}\n"
            if zonas_abajo:
                msg += "\n" + formatear_liquidez(zonas_abajo[0], close, es_arriba=False)
            enviar(msg)
            last_event_time = datetime.now(UTC)
            break
    for z in zonas_abajo:
        key = ("break", round(z["centro"]))
        if key in alerted_liquidity:
            continue
        if close < z["min"] * 0.997:
            alerted_liquidity.add(key)
            zona_consumida = True
            msg = f"📡 BREAKOUT BAJISTA CONFIRMADO\n\nLiquidez abajo consumida: {fmt(z['centro'])}\nPrecio: {fmt(close)}\n"
            if zonas_arriba:
                msg += "\n" + formatear_liquidez(zonas_arriba[0], close, es_arriba=True)
            enviar(msg)
            last_event_time = datetime.now(UTC)
            break

# =========================
# ALERTAS DE SISTEMA
# =========================

def enviar_mapa_liquidez(zonas_arriba, zonas_abajo, precio, titulo="💰 MAPA DE LIQUIDEZ"):
    msg = f"{titulo}\n\nPrecio actual: {fmt(precio)}\n"
    for z in zonas_arriba:
        msg += "\n" + formatear_liquidez(z, precio, es_arriba=True)
    for z in zonas_abajo:
        msg += "\n" + formatear_liquidez(z, precio, es_arriba=False)
    enviar(msg)

def heartbeat():
    global last_heartbeat_time
    ahora = datetime.now(UTC)
    if last_heartbeat_time and (ahora - last_heartbeat_time) < timedelta(hours=HEARTBEAT_HOURS):
        return
    precio = obtener_precio_actual() or 0
    msg = f"💓 HEARTBEAT BOT ACTIVO\nHora UTC: {ahora.strftime('%H:%M')}\nActivo: {SYMBOL}\nPrecio: {fmt(precio)}"
    enviar(msg)
    last_heartbeat_time = ahora

def sin_eventos():
    global last_event_time
    ahora = datetime.now(UTC)
    if last_event_time and (ahora - last_event_time) > timedelta(hours=NO_EVENT_HOURS):
        precio = obtener_precio_actual() or 0
        msg = f"⚠️ MERCADO SIN EVENTOS RELEVANTES\nTiempo sin señales: {NO_EVENT_HOURS}h\nPrecio actual: {fmt(precio)}\nEstado: lateral / baja volatilidad"
        enviar(msg)
        last_event_time = ahora

# =========================
# FUNCIÓN PRINCIPAL
# =========================

def evaluar():
    global zona_actual, zona_alertada_proximidad, zona_consumida, alerted_liquidity, last_event_time, last_mapa_time

    df_macro = obtener_candles(INTERVAL_MACRO)
    df_entry = obtener_candles(INTERVAL_ENTRY)
    precio = obtener_precio_actual()
    if df_macro.empty or df_entry.empty or precio is None:
        return

    zonas_high, zonas_low = detectar_zonas(df_macro)
    zonas_arriba, zonas_abajo = seleccionar_zonas_relevantes(zonas_high, zonas_low, precio)

    # Determinar zona principal (primeras de cada lado)
    nueva_zona = (zonas_arriba[0]["centro"] if zonas_arriba else None,
                  zonas_abajo[0]["centro"] if zonas_abajo else None)

    # Si la zona cambió significativamente y ha pasado el cooldown, enviar mapa
    if not misma_zona(zona_actual, nueva_zona):
        zona_actual = nueva_zona
        zona_alertada_proximidad = False
        zona_consumida = False
        alerted_liquidity.clear()
        ahora = datetime.now(UTC)
        if last_mapa_time is None or (ahora - last_mapa_time) > timedelta(minutes=MAPA_COOLDOWN_MINUTOS):
            enviar_mapa_liquidez(zonas_arriba, zonas_abajo, precio)
            last_mapa_time = ahora
        last_event_time = ahora

    # Radar 2 - Proximidad (solo una alerta por zona y más compacta)
    for z in zonas_arriba + zonas_abajo:
        dist = abs(precio - z["centro"]) / precio
        if dist < PROXIMITY and not zona_alertada_proximidad:
            key = ("prox", round(z["centro"]))
            if key not in alerted_liquidity:
                alerted_liquidity.add(key)
                tipo = "arriba" if z["centro"] > precio else "abajo"
                msg = f"🎯 PRECIO CERCA DE LIQUIDEZ {tipo.upper()}\n\n"
                msg += f"Precio: {fmt(precio)}\n"
                msg += f"Nivel: {fmt(z['centro'])} ({dist*100:.2f}%) | {z['toques']} toques"
                if z["toques"] >= 5:
                    msg += " 🔥"
                # Añadir la zona opuesta más relevante
                if tipo == "arriba" and zonas_abajo:
                    msg += "\n\n" + formatear_liquidez(zonas_abajo[0], precio, es_arriba=False, incluir_distancia=False)
                elif tipo == "abajo" and zonas_arriba:
                    msg += "\n\n" + formatear_liquidez(zonas_arriba[0], precio, es_arriba=True, incluir_distancia=False)
                enviar(msg)
                last_event_time = ahora
                zona_alertada_proximidad = True
                break

    # Radars
    radar_impulse(df_entry, precio, zonas_arriba, zonas_abajo)
    radar_sweep(df_entry, zonas_arriba, zonas_abajo, precio)
    radar_breakout(df_entry, zonas_arriba, zonas_abajo, precio)

    # Sistema
    heartbeat()
    sin_eventos()

# =========================
# INICIO
# =========================

if __name__ == "__main__":
    print("🚀 Iniciando BOT V10.4...")
    # Alerta de inicio con todo el contexto
    precio_inicial = obtener_precio_actual()
    df_temp = obtener_candles(INTERVAL_MACRO, limit=LOOKBACK)
    if not df_temp.empty:
        zh, zl = detectar_zonas(df_temp)
        za, zb = seleccionar_zonas_relevantes(zh, zl, precio_inicial or 0)
        msg = f"🟢 BOT STOP HUNT ENGINE V10.4 ONLINE\n"
        msg += f"Activo: {SYMBOL} | Estructura: {INTERVAL_MACRO} | Eventos: {INTERVAL_ENTRY}\n"
        msg += f"Lookback: {LOOKBACK} velas | Min toques: {MIN_TOUCHES} | Cluster: {CLUSTER_RANGE*100:.2f}%\n\n"
        msg += f"Precio actual: {fmt(precio_inicial or 0)}\n"
        for z in za:
            msg += "\n" + formatear_liquidez(z, precio_inicial or 0, es_arriba=True)
        for z in zb:
            msg += "\n" + formatear_liquidez(z, precio_inicial or 0, es_arriba=False)
        enviar(msg)
    else:
        enviar("🟢 BOT STOP HUNT ENGINE V10.4 ONLINE (sin datos iniciales)")

    # Inicializar tiempos
    last_heartbeat_time = datetime.now(UTC)
    last_event_time = datetime.now(UTC)
    last_mapa_time = None

    while True:
        try:
            evaluar()
        except Exception as e:
            print(f"Error: {e}")
            enviar(f"⚠️ ERROR: {str(e)[:100]}")
            time.sleep(60)
        time.sleep(60)
