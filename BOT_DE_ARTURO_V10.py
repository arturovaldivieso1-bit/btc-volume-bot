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
PROXIMITY = 0.003          # 0.3% - umbral de proximidad
ZONA_EQUIVALENTE = 0.005    # 0.5% - para unificar zonas cercanas

# Radar 0 - Impulso (ahora basado en variación de precio)
IMPULSE_PRICE_CHANGE = 0.65   # Variación mínima en % (condición principal)
IMPULSE_LOOKBACK = 12          # Velas para ruptura de microestructura
IMPULSE_COOLDOWN = 300         # 5 minutos entre alertas

# Umbrales de volumen para probabilidades (en BTC) - ajustados según tu análisis
VOL1_MED = 400
VOL1_P75 = 692
VOL1_P90 = 1134
PROB1 = [68, 82, 87]      # probabilidades para >med, >p75, >p90 (1 vela)

VOL2_MED = 712
VOL2_P75 = 1189
VOL2_P90 = 1876
PROB2 = [72, 84, 88]      # para 2 velas

VOL3_MED = 1023
VOL3_P75 = 1645
VOL3_P90 = 2534
PROB3 = [75, 86, 90]      # para 3 velas

HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6
MAPA_COOLDOWN_MINUTOS = 60
RADAR2_COOLDOWN_MINUTOS = 60

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

def redondear_centro(centro, base=100):
    return round(centro / base) * base

def seleccionar_mejores_zonas(zonas_high, zonas_low, precio):
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

def formatear_nivel_simple(zona, precio):
    if zona is None:
        return ""
    dist = abs(zona["centro"] - precio) / precio * 100
    return f"{fmt(zona['centro'])} ({dist:.1f}%)"

def misma_zona(z1, z2):
    if z1 is None or z2 is None:
        return False
    c1_arriba, c1_abajo = z1
    c2_arriba, c2_abajo = z2
    if c1_arriba is None or c2_arriba is None or c1_abajo is None or c2_abajo is None:
        return False
    diff_arriba = abs(c1_arriba - c2_arriba) / c1_arriba if c1_arriba != 0 else 0
    diff_abajo = abs(c1_abajo - c2_abajo) / c1_abajo if c1_abajo != 0 else 0
    return diff_arriba < ZONA_EQUIVALENTE and diff_abajo < ZONA_EQUIVALENTE

def calcular_probabilidad(volumen, umbrales, probabilidades):
    """Devuelve la probabilidad según el volumen y los umbrales dados."""
    if volumen > umbrales[2]:  # > p90
        return probabilidades[2]
    elif volumen > umbrales[1]:  # > p75
        return probabilidades[1]
    elif volumen > umbrales[0]:  # > mediana
        return probabilidades[0]
    else:
        return None  # No alcanza el mínimo

# =========================
# RADARES
# =========================

def radar_impulse(df_entry, precio_actual):
    """🚀 Radar 0 - Impulso basado en variación de precio >= 0.65% y ruptura de microestructura.
       Muestra volúmenes acumulados y probabilidades de retroceso."""
    global last_impulse_time, last_event_time
    if df_entry.empty or len(df_entry) < max(20, IMPULSE_LOOKBACK + 1):
        return
    vela = df_entry.iloc[-1]
    # Calcular variación de precio
    price_change = abs(vela["close"] - vela["open"]) / vela["open"] * 100

    # Condición principal: variación >= umbral
    if price_change < IMPULSE_PRICE_CHANGE:
        return

    # Verificar ruptura de microestructura (rompe máximos/mínimos de últimas IMPULSE_LOOKBACK velas)
    ventana = df_entry.iloc[-IMPULSE_LOOKBACK-1:-1]
    max_anterior = ventana["high"].max()
    min_anterior = ventana["low"].min()
    if vela["close"] > vela["open"]:  # vela alcista
        if vela["high"] <= max_anterior:
            return
        direccion = "alcista"
        emoji = "🟢"
    else:  # vela bajista
        if vela["low"] >= min_anterior:
            return
        direccion = "bajista"
        emoji = "🔴"

    ahora = datetime.now(UTC)
    if last_impulse_time and (ahora - last_impulse_time).seconds < IMPULSE_COOLDOWN:
        return

    # Calcular volúmenes acumulados
    vol1 = vela["volume"]
    vol2 = vol1 + (df_entry.iloc[-2]["volume"] if len(df_entry) >= 2 else 0)
    vol3 = vol2 + (df_entry.iloc[-3]["volume"] if len(df_entry) >= 3 else 0)

    # Calcular probabilidades
    prob1 = calcular_probabilidad(vol1, (VOL1_MED, VOL1_P75, VOL1_P90), PROB1)
    prob2 = calcular_probabilidad(vol2, (VOL2_MED, VOL2_P75, VOL2_P90), PROB2)
    prob3 = calcular_probabilidad(vol3, (VOL3_MED, VOL3_P75, VOL3_P90), PROB3)

    # Construir mensaje
    titulo = f"🚀 Radar 0 - Impulso {direccion} {emoji}"
    msg = f"{titulo}\n\n"
    msg += f"Precio: {fmt(precio_actual)} - Hora UTC: {ahora.strftime('%H:%M')}\n"
    msg += f"Variación: {price_change:.2f}%\n"
    msg += f"Volumen 1v: {vol1:.2f} BTC"
    if prob1:
        msg += f" (prob retroceso {prob1}%)"
    msg += "\n"
    msg += f"Volumen 2v: {vol2:.2f} BTC"
    if prob2:
        msg += f" (prob retroceso {prob2}%)"
    msg += "\n"
    msg += f"Volumen 3v: {vol3:.2f} BTC"
    if prob3:
        msg += f" (prob retroceso {prob3}%)"

    enviar(msg)
    last_impulse_time = ahora
    last_event_time = ahora

def radar_sweep(df_entry, mejor_arriba, mejor_abajo, precio_actual):
    """🔄 Radar 3 – SWEEP [HIGH/LOW] [color] (nivel barrido)"""
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
    if mejor_arriba and mejor_arriba["tipo"] == "HIGH" and vela_anterior["high"] > mejor_arriba["max"] and vela_anterior["close"] < mejor_arriba["centro"]:
        if vela_anterior["volume"] > vol_medio * 1.5:
            sweep_pendiente = (mejor_arriba, "HIGH")
    elif mejor_abajo and mejor_abajo["tipo"] == "LOW" and vela_anterior["low"] < mejor_abajo["min"] and vela_anterior["close"] > mejor_abajo["centro"]:
        if vela_anterior["volume"] > vol_medio * 1.5:
            sweep_pendiente = (mejor_abajo, "LOW")

def radar_breakout(df_entry, mejor_arriba, mejor_abajo, precio_actual):
    """🌋 Radar 4 – BREAKOUT [dirección] [color] (nivel barrido)"""
    global zona_consumida, last_event_time, alerted_liquidity
    if df_entry.empty:
        return
    vela = df_entry.iloc[-1]
    close = vela["close"]
    ahora = datetime.now(UTC)
    if mejor_arriba:
        key = ("break", mejor_arriba["centro_rd"])
        if key not in alerted_liquidity and close > mejor_arriba["max"] * 1.003:
            alerted_liquidity.add(key)
            zona_consumida = True
            titulo = f"🌋 Radar 4 – BREAKOUT ALCISTA 🟢 ({fmt(mejor_arriba['centro'])})"
            msg = f"{titulo}\n\n"
            msg += f"Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}"
            enviar(msg)
            last_event_time = ahora
            return
    if mejor_abajo:
        key = ("break", mejor_abajo["centro_rd"])
        if key not in alerted_liquidity and close < mejor_abajo["min"] * 0.997:
            alerted_liquidity.add(key)
            zona_consumida = True
            titulo = f"🌋 Radar 4 – BREAKOUT BAJISTA 🔴 ({fmt(mejor_abajo['centro'])})"
            msg = f"{titulo}\n\n"
            msg += f"Precio: {fmt(close)} | Hora: {ahora.strftime('%H:%M')}"
            enviar(msg)
            last_event_time = ahora
            return

# =========================
# ALERTAS DE SISTEMA
# =========================

def enviar_liquidez_detectada(mejor_arriba, mejor_abajo, precio, hora):
    """💰 Radar 1 – LIQUIDEZ [HIGH/LOW] [color] [nivel] (distancia%)"""
    if mejor_arriba:
        nivel_str = formatear_nivel_simple(mejor_arriba, precio)
        titulo = f"💰 Radar 1 – LIQUIDEZ HIGH 🟢 {nivel_str}"
        msg = f"{titulo}\n\n"
        msg += f"Precio actual: {fmt(precio)} | Hora: {hora}\n"
        msg += f"{mejor_arriba['toques']} toques{' 🔥' if mejor_arriba['toques'] >= 5 else ''}"
        enviar(msg)
    if mejor_abajo:
        nivel_str = formatear_nivel_simple(mejor_abajo, precio)
        titulo = f"💰 Radar 1 – LIQUIDEZ LOW 🔴 {nivel_str}"
        msg = f"{titulo}\n\n"
        msg += f"Precio actual: {fmt(precio)} | Hora: {hora}\n"
        msg += f"{mejor_abajo['toques']} toques{' 🔥' if mejor_abajo['toques'] >= 5 else ''}"
        enviar(msg)

def radar_proximidad(mejor_arriba, mejor_abajo, precio, hora):
    """🔍 Radar 2 – CERCA [nivel] (distancia%) [color] con cooldown de 60 min por zona redondeada y tipo"""
    ahora = datetime.now(UTC)
    
    def check_and_send(zona, color_emoji):
        key = (zona["centro_rd"], zona["tipo"])
        dist = abs(precio - zona["centro"]) / precio
        if dist < PROXIMITY:
            ultimo = alerted_proximidad.get(key)
            if ultimo is None or (ahora - ultimo) > timedelta(minutes=RADAR2_COOLDOWN_MINUTOS):
                alerted_proximidad[key] = ahora
                nivel_str = formatear_nivel_simple(zona, precio)
                titulo = f"🔍 Radar 2 – CERCA {nivel_str} {color_emoji}"
                msg = f"{titulo}\n\n"
                msg += f"Precio: {fmt(precio)} | Hora: {hora}"
                enviar(msg)
                last_event_time = ahora
                return True
        return False
    
    if mejor_arriba:
        check_and_send(mejor_arriba, "🟢")
    if mejor_abajo:
        check_and_send(mejor_abajo, "🔴")

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

    df_macro = obtener_candles(INTERVAL_MACRO)
    df_entry = obtener_candles(INTERVAL_ENTRY)
    precio = obtener_precio_actual()
    if df_macro.empty or df_entry.empty or precio is None:
        return

    zonas_high, zonas_low = detectar_zonas(df_macro)
    mejor_arriba, mejor_abajo = seleccionar_mejores_zonas(zonas_high, zonas_low, precio)

    centro_arriba_rd = mejor_arriba["centro_rd"] if mejor_arriba else None
    centro_abajo_rd = mejor_abajo["centro_rd"] if mejor_abajo else None
    nueva_zona = (centro_arriba_rd, centro_abajo_rd)

    if not misma_zona(zona_actual, nueva_zona):
        zona_actual = nueva_zona
        zona_consumida = False
        alerted_liquidity.clear()
        if last_mapa_time is None or (ahora - last_mapa_time) > timedelta(minutes=MAPA_COOLDOWN_MINUTOS):
            enviar_liquidez_detectada(mejor_arriba, mejor_abajo, precio, hora_str)
            last_mapa_time = ahora
        last_event_time = ahora

    # Limpieza de timestamps antiguos en proximidad
    keys_a_remover = []
    for key, ts in alerted_proximidad.items():
        if (ahora - ts) > timedelta(hours=2):
            keys_a_remover.append(key)
    for key in keys_a_remover:
        alerted_proximidad.pop(key, None)

    radar_proximidad(mejor_arriba, mejor_abajo, precio, hora_str)
    radar_impulse(df_entry, precio)
    radar_sweep(df_entry, mejor_arriba, mejor_abajo, precio)
    radar_breakout(df_entry, mejor_arriba, mejor_abajo, precio)
    heartbeat()
    sin_eventos()

# =========================
# INICIO
# =========================

if __name__ == "__main__":
    print("🚀 Iniciando BOT V10.19...")
    precio_inicial = obtener_precio_actual()
    hora_actual = datetime.now(UTC).strftime('%H:%M')
    df_temp = obtener_candles(INTERVAL_MACRO, limit=LOOKBACK)
    if not df_temp.empty and precio_inicial:
        zh, zl = detectar_zonas(df_temp)
        ma, mb = seleccionar_mejores_zonas(zh, zl, precio_inicial)
        msg = f"🤖 BOT DE ARTURO FUNCIONANDO\nHora UTC: {hora_actual}\nPrecio: {fmt(precio_inicial)}"
        if ma:
            nivel_str = formatear_nivel_simple(ma, precio_inicial)
            msg += f"\n\n🔼 Liquidez arriba: {nivel_str} | {ma['toques']} toques{' 🔥' if ma['toques'] >= 5 else ''}"
        if mb:
            nivel_str = formatear_nivel_simple(mb, precio_inicial)
            msg += f"\n🔻 Liquidez abajo: {nivel_str} | {mb['toques']} toques{' 🔥' if mb['toques'] >= 5 else ''}"
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
