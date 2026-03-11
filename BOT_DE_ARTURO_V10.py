# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os
from datetime import datetime, timedelta, UTC

# =========================
# CONFIGURACIÓN INICIAL (Variables globales ajustables)
# =========================

# Tokens de Telegram (obligatorio configurarlas como variables de entorno)
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Par de trading
SYMBOL = "BTCUSDT"

# Intervalos de tiempo
INTERVAL_MACRO = "1h"      # Temporalidad mayor para detectar zonas de liquidez
INTERVAL_ENTRY = "5m"      # Temporalidad menor para análisis fino

# Parámetros para detección de zonas de liquidez
LOOKBACK = 168              # 7 días en 1h para mejor contexto semanal
MIN_TOUCHES = 3             # Mínimo de toques (3-4 para equilibrio)
CLUSTER_RANGE = 0.0025      # 0.25% - un poco más amplio para mejores clusters
PROXIMITY = 0.003           # 0.3% - distancia para alertar proximidad (RADAR 2)
ZONA_EQUIVALENTE = 0.001    # 0.1% - tolerancia para misma zona

# Parámetros para RADAR 0 (impulsos)
IMPULSE_RANGE = 1.5         # % mínimo de rango (más exigente que 1.3)
IMPULSE_VOLUME = 1.3        # Volumen mínimo relativo (más exigente)
IMPULSE_LOOKBACK = 12       # Velas para evaluar ruptura de microestructura
IMPULSE_COOLDOWN = 300      # 5 minutos entre alertas de impulso

# Heartbeat y alertas de sistema
HEARTBEAT_HOURS = 4         # Enviar heartbeat cada 4 horas
NO_EVENT_HOURS = 6          # Alertar si no hay eventos en 6 horas

# Variables de estado internas (no modificar)
last_impulse_time = None
last_heartbeat_time = None
last_event_time = None
zona_actual = None
zona_alertada_proximidad = False
zona_consumida = False
alerted_liquidity = set()    # Para evitar repetir alertas de la misma zona
sweep_pendiente = None       # Para confirmación de sweep en vela siguiente


# =========================
# FUNCIÓN PARA ENVIAR MENSAJES POR TELEGRAM
# =========================
def enviar(msg):
    """Envía un mensaje de texto al chat de Telegram configurado."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": msg
        }, timeout=10)
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")


# =========================
# FUNCIÓN AUXILIAR PARA FORMATEAR NÚMEROS
# =========================
def fmt(n):
    """Formatea números con separadores de miles."""
    return f"{int(n):,}"


# =========================
# OBTENER VELAS DE BINANCE
# =========================
def obtener_candles(interval, limit=200):
    """
    Descarga velas de Binance con timeout y manejo de errores.
    """
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "limit": limit
    }
    try:
        data = requests.get(url, params=params, timeout=10).json()
    except Exception as e:
        print(f"Error obteniendo velas de Binance: {e}")
        return pd.DataFrame(columns=["time","open","high","low","close","volume"])
    
    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "_","_","_","_","_","_"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df


# =========================
# OBTENER PRECIO ACTUAL (tiempo real)
# =========================
def obtener_precio_actual():
    """Obtiene el precio actual de Binance (no el cierre de vela)."""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}"
        r = requests.get(url, timeout=10)
        return float(r.json()["price"])
    except:
        return None


# =========================
# AGRUPAR PRECIOS EN CLÚSTERES (MEJORADO)
# =========================
def cluster(lista):
    """
    Agrupa una lista de precios en clústeres según CLUSTER_RANGE.
    Versión optimizada.
    """
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
            clusters.append({
                "centro": p,
                "valores": [p]
            })
    return clusters


# =========================
# DETECTAR ZONAS DE LIQUIDEZ (MEJORADO)
# =========================
def detectar_zonas(df):
    """
    Analiza el DataFrame macro para identificar zonas de liquidez.
    Retorna dos listas: zonas_high y zonas_low (ordenadas por toques).
    """
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
    
    # Ordenar por toques (mayor primero)
    zonas_high.sort(key=lambda x: x["toques"], reverse=True)
    zonas_low.sort(key=lambda x: x["toques"], reverse=True)
    
    return zonas_high, zonas_low


# =========================
# SELECCIONAR ZONAS RELEVANTES (NUEVO)
# =========================
def seleccionar_zonas_relevantes(zonas_high, zonas_low, precio):
    """
    Selecciona las 2 zonas más relevantes arriba y abajo del precio,
    priorizando fuerza (toques) y cercanía.
    """
    # Zonas arriba del precio
    arriba = [z for z in zonas_high if z["centro"] > precio] + \
             [z for z in zonas_low if z["centro"] > precio]
    
    # Zonas abajo del precio
    abajo = [z for z in zonas_low if z["centro"] < precio] + \
            [z for z in zonas_high if z["centro"] < precio]
    
    # Calcular distancia y score (toques*1000 - distancia)
    for z in arriba:
        z["distancia"] = z["centro"] - precio
        z["score"] = z["toques"] * 1000 - z["distancia"]
    for z in abajo:
        z["distancia"] = precio - z["centro"]
        z["score"] = z["toques"] * 1000 - z["distancia"]
    
    # Ordenar por score y tomar hasta 2
    arriba.sort(key=lambda x: x["score"], reverse=True)
    abajo.sort(key=lambda x: x["score"], reverse=True)
    
    return arriba[:2], abajo[:2]


# =========================
# FORMATEAR LÍNEA DE LIQUIDEZ (NUEVO - FORMATO COMPACTO)
# =========================
def formatear_liquidez(zona, precio, es_arriba=True):
    """
    Formato compacto de una línea para mostrar liquidez.
    Ej: "🟢 Liquidez arriba: 43850 (1.5%) ⬆ | 5 toques 🔥"
    """
    direccion = "⬆" if es_arriba else "⬇"
    distancia = abs(zona["centro"] - precio) / precio * 100
    linea = f"{'🟢' if es_arriba else '🔴'} Liquidez {'arriba' if es_arriba else 'abajo'}: {fmt(zona['centro'])} ({distancia:.1f}%) {direccion} | {zona['toques']} toques"
    if zona["toques"] >= 5:
        linea += " 🔥"
    return linea


# =========================
# COMPARAR SI DOS ZONAS SON LA MISMA
# =========================
def misma_zona(z1, z2):
    if z1 is None or z2 is None:
        return False
    return abs(z1["centro"] - z2["centro"]) / z1["centro"] < ZONA_EQUIVALENTE


# =========================
# RADAR 0 - IMPULSO (MEJORADO)
# =========================
def radar_impulse(df_entry, precio_actual, zonas_arriba, zonas_abajo):
    """
    Detecta impulsos con:
    - Rango > IMPULSE_RANGE * rango promedio
    - Volumen > IMPULSE_VOLUME * volumen promedio
    - Ruptura de microestructura (últimas IMPULSE_LOOKBACK velas)
    """
    global last_impulse_time, last_event_time
    
    if df_entry.empty or len(df_entry) < max(20, IMPULSE_LOOKBACK + 1):
        return
    
    vela = df_entry.iloc[-1]
    rango = (vela["high"] - vela["low"]) / vela["close"] * 100
    rango_medio = ((df_entry["high"] - df_entry["low"]) / df_entry["close"]).rolling(20).mean().iloc[-1] * 100
    vol_actual = vela["volume"]
    vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]
    
    # Verificar condiciones básicas
    if rango < IMPULSE_RANGE * rango_medio:
        return
    if vol_actual < IMPULSE_VOLUME * vol_medio:
        return
    
    # Verificar ruptura de microestructura
    ventana = df_entry.iloc[-IMPULSE_LOOKBACK-1:-1]
    max_anterior = ventana["high"].max()
    min_anterior = ventana["low"].min()
    
    if vela["close"] > vela["open"]:  # Vela alcista
        if vela["high"] <= max_anterior:
            return
        direccion = "ALCISTA"
        emoji = "🟢"
    else:  # Vela bajista
        if vela["low"] >= min_anterior:
            return
        direccion = "BAJISTA"
        emoji = "🔴"
    
    # Cooldown
    ahora = datetime.now(UTC)
    if last_impulse_time and (ahora - last_impulse_time).seconds < IMPULSE_COOLDOWN:
        return
    
    # Verificar si está cerca de alguna zona (para combinar alertas)
    cerca_de = None
    for z in zonas_arriba + zonas_abajo:
        dist = abs(precio_actual - z["centro"]) / precio_actual
        if dist < PROXIMITY:
            tipo = "arriba" if z["centro"] > precio_actual else "abajo"
            cerca_de = f"CERCA DE LIQUIDEZ {tipo.upper()}"
            break
    
    # Construir mensaje
    titulo = f"⚡ IMPULSO {direccion} DETECTADO"
    if cerca_de:
        titulo = f"⚡ IMPULSO {direccion} {cerca_de}"
    
    msg = f"{titulo}\n\n"
    msg += f"Precio: {fmt(precio_actual)}\n"
    msg += f"Volumen: {vol_actual:.2f} BTC ({(vol_actual/vol_medio):.1f}x media)\n\n"
    
    # Añadir contexto de liquidez
    for z in zonas_arriba:
        msg += formatear_liquidez(z, precio_actual, es_arriba=True) + "\n"
    for z in zonas_abajo:
        msg += formatear_liquidez(z, precio_actual, es_arriba=False) + "\n"
    
    enviar(msg)
    last_impulse_time = ahora
    last_event_time = ahora


# =========================
# RADAR 3 - SWEEP CON CONFIRMACIÓN (NUEVO)
# =========================
def radar_sweep(df_entry, zonas_arriba, zonas_abajo, precio_actual):
    """
    Detecta sweep en la vela actual y lo confirma con la siguiente.
    """
    global sweep_pendiente, last_event_time
    
    if df_entry.empty or len(df_entry) < 2:
        return
    
    vela_actual = df_entry.iloc[-1]
    
    # Si hay sweep pendiente de confirmar
    if sweep_pendiente:
        zona, tipo_sweep = sweep_pendiente
        
        # Confirmar con vela actual
        if tipo_sweep == "HIGH" and vela_actual["close"] < vela_actual["open"]:
            direccion = "🔻 BAJISTA"
        elif tipo_sweep == "LOW" and vela_actual["close"] > vela_actual["open"]:
            direccion = "🔺 ALCISTA"
        else:
            sweep_pendiente = None
            return
        
        # Enviar alerta
        msg = f"🚨 SWEEP DE LIQUIDEZ {'ARRIBA' if tipo_sweep=='HIGH' else 'ABAJO'} CONFIRMADO\n\n"
        msg += f"Precio: {fmt(precio_actual)}\n"
        msg += f"Dirección probable: {direccion}\n\n"
        
        for z in zonas_arriba:
            msg += formatear_liquidez(z, precio_actual, es_arriba=True) + "\n"
        for z in zonas_abajo:
            msg += formatear_liquidez(z, precio_actual, es_arriba=False) + "\n"
        
        enviar(msg)
        last_event_time = datetime.now(UTC)
        sweep_pendiente = None
        return
    
    # Buscar nuevo sweep en la vela actual
    vol_medio = df_entry["volume"].rolling(20).mean().iloc[-1]
    vela = df_entry.iloc[-2] if len(df_entry) >= 2 else None  # Usamos la anterior para evitar look-ahead bias
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


# =========================
# RADAR 4 - BREAKOUT (MEJORADO)
# =========================
def radar_breakout(df_entry, zonas_arriba, zonas_abajo, precio_actual):
    """
    Detecta breakout cuando el precio supera una zona con margen.
    """
    global zona_consumida, last_event_time
    
    if df_entry.empty:
        return
    
    vela = df_entry.iloc[-1]
    close = vela["close"]
    
    # Breakout alcista
    for z in zonas_arriba:
        key = ("break", round(z["centro"]))
        if key in alerted_liquidity:
            continue
        if close > z["max"] * 1.003:  # Margen 0.3%
            alerted_liquidity.add(key)
            zona_consumida = True
            
            msg = f"📡 BREAKOUT ALCISTA CONFIRMADO\n\n"
            msg += f"Liquidez arriba consumida: {fmt(z['centro'])}\n"
            msg += f"Precio: {fmt(close)}\n\n"
            
            # Mostrar siguientes zonas
            for zz in zonas_arriba:
                if zz["centro"] > z["centro"]:
                    msg += formatear_liquidez(zz, close, es_arriba=True) + "\n"
            for zz in zonas_abajo:
                msg += formatear_liquidez(zz, close, es_arriba=False) + "\n"
            
            enviar(msg)
            last_event_time = datetime.now(UTC)
            break
    
    # Breakout bajista
    for z in zonas_abajo:
        key = ("break", round(z["centro"]))
        if key in alerted_liquidity:
            continue
        if close < z["min"] * 0.997:  # Margen 0.3%
            alerted_liquidity.add(key)
            zona_consumida = True
            
            msg = f"📡 BREAKOUT BAJISTA CONFIRMADO\n\n"
            msg += f"Liquidez abajo consumida: {fmt(z['centro'])}\n"
            msg += f"Precio: {fmt(close)}\n\n"
            
            for zz in zonas_arriba:
                msg += formatear_liquidez(zz, close, es_arriba=True) + "\n"
            for zz in zonas_abajo:
                if zz["centro"] < z["centro"]:
                    msg += formatear_liquidez(zz, close, es_arriba=False) + "\n"
            
            enviar(msg)
            last_event_time = datetime.now(UTC)
            break


# =========================
# HEARTBEAT (NUEVO)
# =========================
def heartbeat():
    """Envía heartbeat cada HEARTBEAT_HOURS."""
    global last_heartbeat_time
    
    ahora = datetime.now(UTC)
    if last_heartbeat_time is None:
        last_heartbeat_time = ahora
        return
    
    if (ahora - last_heartbeat_time) > timedelta(hours=HEARTBEAT_HOURS):
        precio = obtener_precio_actual()
        if precio is None:
            precio = 0
        
        msg = f"💓 HEARTBEAT BOT ACTIVO\n\n"
        msg += f"Hora UTC: {ahora.strftime('%H:%M')}\n"
        msg += f"Activo: {SYMBOL}\n"
        msg += f"Precio: {fmt(precio)}"
        enviar(msg)
        last_heartbeat_time = ahora


# =========================
# ALERTA SIN EVENTOS (NUEVO)
# =========================
def sin_eventos():
    """Alerta si no hay eventos en más de NO_EVENT_HOURS."""
    global last_event_time
    
    ahora = datetime.now(UTC)
    if last_event_time is None:
        last_event_time = ahora
        return
    
    if (ahora - last_event_time) > timedelta(hours=NO_EVENT_HOURS):
        precio = obtener_precio_actual()
        if precio is None:
            precio = 0
        
        msg = f"⚠️ MERCADO SIN EVENTOS RELEVANTES\n\n"
        msg += f"Tiempo sin señales: {NO_EVENT_HOURS}h\n"
        msg += f"Precio actual: {fmt(precio)}\n"
        msg += f"Estado: lateral / baja volatilidad"
        enviar(msg)
        last_event_time = ahora


# =========================
# FUNCIÓN PRINCIPAL DE EVALUACIÓN (MEJORADA)
# =========================
def evaluar():
    """
    Ejecuta el flujo completo del bot con todas las mejoras.
    """
    global zona_actual, zona_alertada_proximidad, zona_consumida, alerted_liquidity, last_event_time
    
    # Obtener datos
    df_macro = obtener_candles(INTERVAL_MACRO)
    df_entry = obtener_candles(INTERVAL_ENTRY)
    precio_actual = obtener_precio_actual()
    
    if df_macro.empty or df_entry.empty or precio_actual is None:
        print("Datos insuficientes, reintentando...")
        return
    
    # Detectar zonas de liquidez
    zonas_high, zonas_low = detectar_zonas(df_macro)
    zonas_arriba, zonas_abajo = seleccionar_zonas_relevantes(zonas_high, zonas_low, precio_actual)
    
    # RADAR 1 - Nueva zona (si cambió la estructura principal)
    zona_principal = (zonas_arriba[0]["centro"] if zonas_arriba else None,
                     zonas_abajo[0]["centro"] if zonas_abajo else None)
    
    if zona_principal != zona_actual:
        zona_actual = zona_principal
        zona_alertada_proximidad = False
        zona_consumida = False
        alerted_liquidity.clear()
        
        msg = f"💰 MAPA DE LIQUIDEZ\n\n"
        msg += f"Precio actual: {fmt(precio_actual)}\n\n"
        for z in zonas_arriba:
            msg += formatear_liquidez(z, precio_actual, es_arriba=True) + "\n"
        for z in zonas_abajo:
            msg += formatear_liquidez(z, precio_actual, es_arriba=False) + "\n"
        
        enviar(msg)
        last_event_time = datetime.now(UTC)
    
    # RADAR 2 - Proximidad
    for z in zonas_arriba + zonas_abajo:
        dist = abs(precio_actual - z["centro"]) / precio_actual
        if dist < PROXIMITY and not zona_alertada_proximidad:
            key = ("prox", round(z["centro"]))
            if key not in alerted_liquidity:
                alerted_liquidity.add(key)
                tipo = "arriba" if z["centro"] > precio_actual else "abajo"
                
                msg = f"🎯 PRECIO CERCA DE LIQUIDEZ {tipo.upper()}\n\n"
                msg += f"Precio: {fmt(precio_actual)}\n"
                msg += f"Nivel: {fmt(z['centro'])} ({(dist*100):.2f}%)\n\n"
                
                for zz in zonas_arriba:
                    msg += formatear_liquidez(zz, precio_actual, es_arriba=True) + "\n"
                for zz in zonas_abajo:
                    msg += formatear_liquidez(zz, precio_actual, es_arriba=False) + "\n"
                
                enviar(msg)
                last_event_time = datetime.now(UTC)
                zona_alertada_proximidad = True
                break
    
    # RADAR 0 - Impulso
    radar_impulse(df_entry, precio_actual, zonas_arriba, zonas_abajo)
    
    # RADAR 3 - Sweep con confirmación
    radar_sweep(df_entry, zonas_arriba, zonas_abajo, precio_actual)
    
    # RADAR 4 - Breakout
    radar_breakout(df_entry, zonas_arriba, zonas_abajo, precio_actual)
    
    # Alertas de sistema
    heartbeat()
    sin_eventos()


# =========================
# MENSAJE DE INICIO
# =========================
print("🚀 BOT DE ARTURO V12.1 MEJORADO iniciado...")
enviar("🟢 BOT STOP HUNT ENGINE V12.1 ONLINE")
time.sleep(2)  # Pequeña pausa para que el mensaje se envíe


# =========================
# BUCLE PRINCIPAL
# =========================
if __name__ == "__main__":
    last_heartbeat_time = datetime.now(UTC)
    last_event_time = datetime.now(UTC)
    
    while True:
        try:
            evaluar()
        except Exception as e:
            print(f"Error en el ciclo principal: {e}")
            enviar(f"⚠️ ERROR EN BOT: {str(e)[:100]}")
            time.sleep(60)
        time.sleep(60)
