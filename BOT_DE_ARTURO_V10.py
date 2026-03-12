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
PROXIMITY = 0.003          # 0.3% - umbral de proximidad (lo dejamos)
ZONA_EQUIVALENTE = 0.005    # 0.5% - para unificar zonas cercanas en Radar 2

IMPULSE_RANGE = 1.5
IMPULSE_VOLUME = 1.3
IMPULSE_LOOKBACK = 12
IMPULSE_COOLDOWN = 300

HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6
MAPA_COOLDOWN_MINUTOS = 60
RADAR2_COOLDOWN_MINUTOS = 30  # Cooldown para repetir alerta de la misma zona redondeada

# Variables de estado
last_impulse_time = None
last_heartbeat_time = None
last_event_time = None
last_mapa_time = None
zona_actual = None
zona_consumida = False
alerted_liquidity = set()        # Para breakouts y sweeps
alerted_proximidad = {}           # Diccionario {zona_redondeada: timestamp}
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

def redondear_centro(centro, base=50):
    """Redondea a múltiplos de 'base' (ej. 50) para estabilizar claves"""
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
    """Formato para mostrar nivel con distancia: 70,884 (0.1%)"""
    if zona is None:
        return ""
    dist = abs(zona["centro"] - precio) / precio * 100
    return f"{fmt(zona['centro'])} ({dist:.1f}%)"

def misma_zona(z1, z2):
    """Compara dos tuplas (centro_arriba_rd, centro_abajo_rd) con tolerancia ZONA_EQUIVALENTE"""
    if z1 is None or z2 is None:
        return False
    c1_arriba, c1_abajo = z1
    c2_arriba, c2_abajo = z2
    if c1_arriba is None or c2_arriba is None or c1_abajo is None or c2_abajo is None:
        return False
    diff_arriba = abs(c1_arriba - c2_arriba) / c1_arriba if c1_arriba != 0 else 0
    diff_abajo = abs(c1_abajo - c2_abajo) / c1_abajo if c1_abajo != 0 else 0
    return diff_arriba < ZONA_EQUIVALENTE and diff_abajo < ZONA_EQUIVALENTE

# =========================
# RADARES
# =========================

def radar_impulse(df_entry, precio_actual):
    """🚀 Radar 0 - Impulso [dirección] [color]"""
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
        direccion = "alcista"
        emoji = "🟢"
    else:
        if vela["low"] >= min_anterior:
            return
        direccion = "bajista"
        emoji = "🔴"
    ahora = datetime.now(UTC)
    if last_impulse_time and (ahora - last_impulse_time).seconds < IMPULSE_COOLDOWN:
        return
    titulo = f"🚀 Radar 0 - Impulso {direccion} {emoji}"
    msg = f"{titulo}\n\n"
    msg += f"Precio: {fmt(precio_actual)} - Hora UTC: {ahora.strftime('%H:%M')}\n"
    msg += f"Volumen: {vol_actual:.2f} BTC ({(vol_actual/vol_medio):.1f}x media)"
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
    """🔍 Radar 2 – CERCA [nivel] (distancia%) [color] con cooldown de 30 min por zona redondeada"""
    ahora = datetime.now(UTC)
    
    def check_and_send(zona, color_emoji):
        key = zona["centro_rd"]
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

    # Obtener centros redondeados para la zona actual
    centro_arriba_rd = mejor_arriba["centro_rd"] if mejor_arriba else None
    centro_abajo_rd = mejor_abajo["centro_rd"] if mejor_abajo else None
    nueva_zona = (centro_arriba_rd, centro_abajo_rd)

    # Si la zona cambió significativamente, actualizar y enviar RADAR 1 (con cooldown)
    if not misma_zona(zona_actual, nueva_zona):
        zona_actual = nueva_zona
        zona_consumida = False
        alerted_liquidity.clear()
        # No limpiar alerted_proximidad para mantener cooldown, pero sí eliminar zonas que ya no existen
        # (se hará más abajo con la limpieza)
        if last_mapa_time is None or (ahora - last_mapa_time) > timedelta(minutes=MAPA_COOLDOWN_MINUTOS):
            enviar_liquidez_detectada(mejor_arriba, mejor_abajo, precio, hora_str)
            last_mapa_time = ahora
        last_event_time = ahora

    # Limpiar del diccionario de proximidad las zonas que ya no están presentes
    keys_a_remover = []
    for key in alerted_proximidad:
        # Verificar si alguna de las mejores zonas tiene este centro redondeado
        presente = (mejor_arriba and mejor_arriba["centro_rd"] == key) or (mejor_abajo and mejor_abajo["centro_rd"] == key)
        if not presente:
            keys_a_remover.append(key)
    for key in keys_a_remover:
        alerted_proximidad.pop(key, None)

    # RADAR 2 - Proximidad (con cooldown)
    radar_proximidad(mejor_arriba, mejor_abajo, precio, hora_str)

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
    print("🚀 Iniciando BOT V10.15...")
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
