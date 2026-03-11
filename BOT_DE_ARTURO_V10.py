# -*- coding: utf-8 -*-

import requests
import pandas as pd
import time
import os
from datetime import datetime, timedelta, UTC

# =============================================================================
# CONFIGURACIÓN INICIAL – AJUSTA ESTOS VALORES SEGÚN TU CRITERIO
# =============================================================================

# Tokens de Telegram (obligatorio configurarlos como variables de entorno)
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Par de trading
SYMBOL = "BTCUSDT"

# Timeframes
TIMEFRAME_ESTRUCTURA = "1h"      # Para detectar zonas de liquidez
TIMEFRAME_EVENTOS   = "5m"       # Para impulsos, sweeps, breakouts

# Ventana de análisis de liquidez (en velas del timeframe estructura)
LOOKBACK_LIQUIDEZ = 168           # 7 días en 1h → 168 velas

# Parámetros de clustering (agrupación de precios en zonas)
CLUSTER_RANGE = 0.0025            # 0.25% – radios de agrupación (más alto = zonas más amplias)
MIN_TOUCHES   = 3                 # Mínimo de toques para considerar una zona

# Fuerza de la zona (se muestra con 🔥 a partir de este umbral)
TOQUES_FUERTE = 5

# Distancia para Radar 2 (proximidad a liquidez)
PROXIMITY_THRESHOLD = 0.003       # 0.3%

# Margen para breakout (evita falsas rupturas)
BREAKOUT_MARGIN = 0.003           # 0.3%

# Parámetros del Radar 0 (impulso / mercado despierta)
IMPULSE_RANGE_FACTOR = 1.5        # Vela debe ser 1.5x el rango promedio
IMPULSE_VOLUME_FACTOR = 1.3       # Volumen debe ser 1.3x la media móvil
IMPULSE_LOOKBACK = 12             # Nº de velas para evaluar ruptura de microestructura
IMPULSE_COOLDOWN = 300            # Segundos entre alertas de impulso (5 min)

# Heartbeat y alertas de sistema
HEARTBEAT_HOURS = 4                # Enviar heartbeat cada 4 horas
NO_EVENT_HOURS  = 6                # Alertar si no hay eventos en 6 horas

# Control de reintentos en caso de error de conexión
MAX_RETRIES = 5
RETRY_DELAY = 10                    # segundos entre reintentos

# =============================================================================
# VARIABLES DE ESTADO (NO MODIFICAR)
# =============================================================================

ultimo_impulse_time = None
ultimo_heartbeat_time = datetime.now(UTC)
ultimo_evento_time = datetime.now(UTC)
zona_actual = None                 # Última zona principal detectada
zona_alertada_proximidad = False
zona_consumida = False
sweep_confirmado_pendiente = None  # Guarda sweep de la vela anterior para confirmar

# Conjunto para evitar repetir alertas de la misma zona (proximidad, sweep, breakout)
alerted_liquidity = set()

# =============================================================================
# FUNCIONES AUXILIARES
# =============================================================================

def enviar_telegram(msg):
    """Envía un mensaje por Telegram con timeout y control de errores."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Error enviando a Telegram: {e}")

def formatear_numero(n):
    """Formatea un número con separadores de miles (ej. 43,200)."""
    return f"{int(n):,}"

def obtener_candles(interval, limit=200):
    """
    Descarga velas de Binance.
    En caso de error, retorna DataFrame vacío y el sistema reintentará después.
    """
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": interval, "limit": limit}
    for intento in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code != 200:
                print(f"Binance respondió con código {r.status_code}, reintento {intento+1}")
                time.sleep(RETRY_DELAY)
                continue
            data = r.json()
            df = pd.DataFrame(data, columns=[
                "time","open","high","low","close","volume",
                "_","_","_","_","_","_"
            ])
            for col in ["open","high","low","close","volume"]:
                df[col] = df[col].astype(float)
            return df
        except Exception as e:
            print(f"Error en obtener_candles (intento {intento+1}): {e}")
            time.sleep(RETRY_DELAY)
    # Si falla todo, devolvemos DataFrame vacío y notificamos
    enviar_telegram("⚠️ No se pudieron obtener datos de Binance tras varios intentos.")
    return pd.DataFrame()

def cluster_precios(lista_precios):
    """
    Agrupa precios en clústeres según CLUSTER_RANGE.
    Retorna lista de clústeres con centro, valores y toques.
    """
    clusters = []
    for p in sorted(lista_precios):
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

def detectar_zonas_liquidez(df):
    """
    A partir del DataFrame de estructura (1h) extrae las zonas de liquidez
    (clústeres de máximos y mínimos) con al menos MIN_TOUCHES.
    Retorna dos listas: zonas_high y zonas_low, cada una ordenada por toques.
    """
    if df.empty or len(df) < LOOKBACK_LIQUIDEZ:
        return [], []

    highs = df["high"].tail(LOOKBACK_LIQUIDEZ).tolist()
    lows  = df["low"].tail(LOOKBACK_LIQUIDEZ).tolist()

    clusters_high = cluster_precios(highs)
    clusters_low  = cluster_precios(lows)

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

    # Ordenar por número de toques (mayor primero)
    zonas_high.sort(key=lambda x: x["toques"], reverse=True)
    zonas_low.sort(key=lambda x: x["toques"], reverse=True)
    return zonas_high, zonas_low

def seleccionar_zonas_relevantes(zonas_high, zonas_low, precio_actual):
    """
    De las zonas detectadas, elige las dos más cercanas al precio
    pero priorizando las de mayor toques (fuerza).
    Retorna hasta 2 zonas arriba y 2 abajo.
    """
    # Filtrar zonas según su posición respecto al precio
    arriba = [z for z in zonas_high if z["centro"] > precio_actual] + \
             [z for z in zonas_low if z["centro"] > precio_actual]
    abajo  = [z for z in zonas_low if z["centro"] < precio_actual] + \
             [z for z in zonas_high if z["centro"] < precio_actual]

    # Ordenar por cercanía (distancia absoluta) pero manteniendo un peso por toques
    # Usamos un ranking simple: (toques * 1000) - distancia
    for z in arriba:
        z["distancia"] = z["centro"] - precio_actual
        z["score"] = z["toques"] * 1000 - z["distancia"]
    for z in abajo:
        z["distancia"] = precio_actual - z["centro"]
        z["score"] = z["toques"] * 1000 - z["distancia"]

    arriba.sort(key=lambda x: x["score"], reverse=True)
    abajo.sort(key=lambda x: x["score"], reverse=True)

    return arriba[:2], abajo[:2]

def formatear_linea_liquidez(zona, precio_actual, es_arriba=True):
    """
    Devuelve una línea de texto con el formato compacto acordado.
    Ejemplo: "🟢 Liquidez arriba: 44000 (0.5%) | 5 toques 🔥"
    """
    flecha = "⬆" if es_arriba else "⬇"
    distancia = abs(zona["centro"] - precio_actual) / precio_actual * 100
    linea = f"{'🟢' if es_arriba else '🔴'} Liquidez {'arriba' if es_arriba else 'abajo'}: {formatear_numero(zona['centro'])} ({distancia:.1f}%) {flecha} | {zona['toques']} toques"
    if zona["toques"] >= TOQUES_FUERTE:
        linea += " 🔥"
    return linea

# =============================================================================
# RADARES
# =============================================================================

def radar_0_impulso(df_eventos, precio_actual, zonas_arriba, zonas_abajo):
    """
    Detecta si la última vela de eventos (5m) es un impulso significativo:
      - Rango > factor * rango promedio
      - Volumen > factor * volumen promedio
      - Rompe el máximo/mínimo de las últimas IMPULSE_LOOKBACK velas
    Además respeta cooldown y combina con proximidad si aplica.
    """
    global ultimo_impulse_time

    if df_eventos.empty or len(df_eventos) < IMPULSE_LOOKBACK + 1:
        return

    vela = df_eventos.iloc[-1]
    rango_actual = vela["high"] - vela["low"]
    rango_medio = (df_eventos["high"] - df_eventos["low"]).rolling(20).mean().iloc[-1]
    vol_actual = vela["volume"]
    vol_medio = df_eventos["volume"].rolling(20).mean().iloc[-1]

    # Condiciones de impulso
    if rango_actual < rango_medio * IMPULSE_RANGE_FACTOR:
        return
    if vol_actual < vol_medio * IMPULSE_VOLUME_FACTOR:
        return

    # Ruptura de microestructura (últimas IMPULSE_LOOKBACK velas, excluyendo la actual)
    ventana = df_eventos.iloc[-IMPULSE_LOOKBACK-1:-1]   # velas anteriores
    max_anterior = ventana["high"].max()
    min_anterior = ventana["low"].min()
    if vela["close"] > vela["open"]:   # vela alcista
        if vela["high"] <= max_anterior:
            return
        direccion = "ALCISTA"
        emoji = "🟢"
    else:                              # vela bajista
        if vela["low"] >= min_anterior:
            return
        direccion = "BAJISTA"
        emoji = "🔴"

    # Cooldown
    ahora = datetime.now(UTC)
    if ultimo_impulse_time and (ahora - ultimo_impulse_time).seconds < IMPULSE_COOLDOWN:
        return

    # Determinar si está cerca de alguna zona (para combinar alertas)
    cerca_de = None
    for z in zonas_arriba + zonas_abajo:
        dist = abs(precio_actual - z["centro"]) / precio_actual
        if dist < PROXIMITY_THRESHOLD:
            tipo = "arriba" if z["centro"] > precio_actual else "abajo"
            cerca_de = f"cerca de liquidez {tipo}"
            break

    # Construir mensaje
    titulo = f"⚡ IMPULSO {direccion} DETECTADO"
    if cerca_de:
        titulo = f"⚡ IMPULSO {direccion} {cerca_de.upper()}"

    msg = f"{titulo}\n\n"
    msg += f"Precio: {formatear_numero(precio_actual)}\n"
    msg += f"Volumen: {vol_actual:.2f} BTC ({(vol_actual/vol_medio):.1f}x media)\n"
    # Añadir contexto de liquidez
    for z in zonas_arriba:
        msg += formatear_linea_liquidez(z, precio_actual, es_arriba=True) + "\n"
    for z in zonas_abajo:
        msg += formatear_linea_liquidez(z, precio_actual, es_arriba=False) + "\n"

    enviar_telegram(msg)
    ultimo_impulse_time = ahora
    global ultimo_evento_time
    ultimo_evento_time = ahora

def radar_1_nueva_zona(zonas_arriba, zonas_abajo, precio_actual):
    """
    Se llama cuando cambia la zona principal (por nueva detección).
    Envía el mapa de liquidez completo.
    """
    msg = "💰 MAPA DE LIQUIDEZ\n\n"
    msg += f"Precio actual: {formatear_numero(precio_actual)}\n\n"
    for z in zonas_arriba:
        msg += formatear_linea_liquidez(z, precio_actual, es_arriba=True) + "\n"
    for z in zonas_abajo:
        msg += formatear_linea_liquidez(z, precio_actual, es_arriba=False) + "\n"
    enviar_telegram(msg)

def radar_2_proximidad(precio_actual, zonas_arriba, zonas_abajo):
    """
    Si el precio está a menos de PROXIMITY_THRESHOLD de alguna zona no alertada,
    envía alerta de proximidad (combinada si también hay impulso reciente).
    """
    global zona_alertada_proximidad, ultimo_evento_time

    for z in zonas_arriba + zonas_abajo:
        dist = abs(precio_actual - z["centro"]) / precio_actual
        if dist < PROXIMITY_THRESHOLD:
            key = ("prox", round(z["centro"]))
            if key in alerted_liquidity:
                continue
            alerted_liquidity.add(key)
            tipo = "arriba" if z["centro"] > precio_actual else "abajo"
            msg = f"🎯 PRECIO CERCA DE LIQUIDEZ {tipo.upper()}\n\n"
            msg += f"Precio: {formatear_numero(precio_actual)}\n"
            msg += f"Nivel: {formatear_numero(z['centro'])} ({(dist*100):.2f}%)\n"
            # Mostrar contexto de otras zonas
            for zz in zonas_arriba:
                msg += formatear_linea_liquidez(zz, precio_actual, es_arriba=True) + "\n"
            for zz in zonas_abajo:
                msg += formatear_linea_liquidez(zz, precio_actual, es_arriba=False) + "\n"
            enviar_telegram(msg)
            ultimo_evento_time = datetime.now(UTC)
            break  # solo una alerta de proximidad por ciclo

def radar_3_sweep(df_eventos, zonas_arriba, zonas_abajo, precio_actual):
    """
    Detecta sweep con confirmación en la vela siguiente.
    Guarda el sweep de la vela actual y, si la siguiente confirma, envía alerta.
    """
    global sweep_confirmado_pendiente, ultimo_evento_time

    if df_eventos.empty or len(df_eventos) < 2:
        return

    vela_actual = df_eventos.iloc[-1]
    vela_anterior = df_eventos.iloc[-2] if len(df_eventos) >= 2 else None

    # Si hay un sweep pendiente de confirmar, verificamos la vela actual
    if sweep_confirmado_pendiente:
        zona, tipo_sweep = sweep_confirmado_pendiente  # tipo_sweep: "HIGH" o "LOW"
        # Confirmación: vela actual cierra en dirección contraria al sweep
        if tipo_sweep == "HIGH" and vela_actual["close"] < vela_actual["open"]:
            direccion_probable = "🔻 bajista"
        elif tipo_sweep == "LOW" and vela_actual["close"] > vela_actual["open"]:
            direccion_probable = "🔺 alcista"
        else:
            sweep_confirmado_pendiente = None
            return

        # Construir alerta
        msg = f"🚨 SWEEP DE LIQUIDEZ {'ARRIBA' if tipo_sweep=='HIGH' else 'ABAJO'} DETECTADO\n\n"
        msg += f"Precio: {formatear_numero(precio_actual)}\n"
        msg += f"Confirmación: {direccion_probable}\n\n"
        for z in zonas_arriba:
            msg += formatear_linea_liquidez(z, precio_actual, es_arriba=True) + "\n"
        for z in zonas_abajo:
            msg += formatear_linea_liquidez(z, precio_actual, es_arriba=False) + "\n"
        enviar_telegram(msg)
        ultimo_evento_time = datetime.now(UTC)
        sweep_confirmado_pendiente = None
        return

    # Buscar sweep en la vela actual (para confirmar en la siguiente)
    for z in zonas_arriba + zonas_abajo:
        tipo_zona = z["tipo"]  # HIGH o LOW
        if tipo_zona == "HIGH" and vela_actual["high"] > z["max"] and vela_actual["close"] < z["centro"]:
            # Sweep de liquidez arriba
            sweep_confirmado_pendiente = (z, "HIGH")
            break
        if tipo_zona == "LOW" and vela_actual["low"] < z["min"] and vela_actual["close"] > z["centro"]:
            sweep_confirmado_pendiente = (z, "LOW")
            break

def radar_4_breakout(df_eventos, zonas_arriba, zonas_abajo, precio_actual):
    """
    Detecta breakout cuando el precio supera una zona con el margen definido
    y la zona no ha sido consumida antes.
    """
    global zona_consumida, ultimo_evento_time

    if df_eventos.empty:
        return

    vela = df_eventos.iloc[-1]
    close = vela["close"]

    # Revisar zonas arriba para breakout alcista
    for z in zonas_arriba:
        key = ("break", round(z["centro"]))
        if key in alerted_liquidity:
            continue
        if close > z["max"] * (1 + BREAKOUT_MARGIN):
            alerted_liquidity.add(key)
            msg = f"📡 BREAKOUT ALCISTA CONFIRMADO\n\n"
            msg += f"Liquidez arriba consumida: {formatear_numero(z['centro'])}\n"
            msg += f"Precio: {formatear_numero(close)}\n\n"
            # Mostrar siguientes zonas
            for zz in zonas_arriba:
                if zz["centro"] > z["centro"]:
                    msg += formatear_linea_liquidez(zz, close, es_arriba=True) + "\n"
            for zz in zonas_abajo:
                msg += formatear_linea_liquidez(zz, close, es_arriba=False) + "\n"
            enviar_telegram(msg)
            ultimo_evento_time = datetime.now(UTC)
            break

    # Revisar zonas abajo para breakout bajista
    for z in zonas_abajo:
        key = ("break", round(z["centro"]))
        if key in alerted_liquidity:
            continue
        if close < z["min"] * (1 - BREAKOUT_MARGIN):
            alerted_liquidity.add(key)
            msg = f"📡 BREAKOUT BAJISTA CONFIRMADO\n\n"
            msg += f"Liquidez abajo consumida: {formatear_numero(z['centro'])}\n"
            msg += f"Precio: {formatear_numero(close)}\n\n"
            for zz in zonas_arriba:
                msg += formatear_linea_liquidez(zz, close, es_arriba=True) + "\n"
            for zz in zonas_abajo:
                if zz["centro"] < z["centro"]:
                    msg += formatear_linea_liquidez(zz, close, es_arriba=False) + "\n"
            enviar_telegram(msg)
            ultimo_evento_time = datetime.now(UTC)
            break

# =============================================================================
# ALERTAS DE SISTEMA
# =============================================================================

def heartbeat():
    """Envía un mensaje cada HEARTBEAT_HOURS para confirmar que el bot está vivo."""
    global ultimo_heartbeat_time
    ahora = datetime.now(UTC)
    if (ahora - ultimo_heartbeat_time) > timedelta(hours=HEARTBEAT_HOURS):
        # Obtenemos precio actual con una llamada simple
        try:
            r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}", timeout=10)
            precio = float(r.json()["price"])
        except:
            precio = 0
        msg = f"💓 HEARTBEAT BOT ACTIVO\n\n"
        msg += f"Hora UTC: {ahora.strftime('%H:%M')}\n"
        msg += f"Activo: {SYMBOL}\n"
        msg += f"Precio: {formatear_numero(precio)}\n"
        msg += f"Estado: monitoreando mercado"
        enviar_telegram(msg)
        ultimo_heartbeat_time = ahora

def sin_eventos():
    """Si no hay eventos en más de NO_EVENT_HOURS, envía un aviso."""
    global ultimo_evento_time
    ahora = datetime.now(UTC)
    if (ahora - ultimo_evento_time) > timedelta(hours=NO_EVENT_HOURS):
        # Obtenemos precio actual
        try:
            r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}", timeout=10)
            precio = float(r.json()["price"])
        except:
            precio = 0
        msg = f"⚠️ MERCADO SIN EVENTOS RELEVANTES\n\n"
        msg += f"Tiempo sin señales: {NO_EVENT_HOURS}h\n"
        msg += f"Precio actual: {formatear_numero(precio)}\n"
        msg += f"Estado: lateral / baja volatilidad"
        enviar_telegram(msg)
        ultimo_evento_time = ahora   # reiniciamos para no spamear cada ciclo

# =============================================================================
# BUCLE PRINCIPAL
# =============================================================================

def startup():
    """Al iniciar el bot, envía una alerta con el contexto actual."""
    enviar_telegram("🟢 BOT STOP HUNT ENGINE V12.1 ONLINE")
    # Esperamos un par de segundos para que las APIs respondan bien
    time.sleep(2)
    # Forzamos una ejecución de evaluar para tener contexto y enviar mapa
    evaluar()
    # Marcamos el heartbeat para que no envíe uno inmediatamente
    global ultimo_heartbeat_time
    ultimo_heartbeat_time = datetime.now(UTC)

def evaluar():
    """
    Función principal que se ejecuta cada minuto.
    Obtiene datos, actualiza zonas y dispara radares.
    """
    global zona_actual, alerted_liquidity, zona_consumida, ultimo_evento_time

    # 1. Obtener datos
    df_estructura = obtener_candles(TIMEFRAME_ESTRUCTURA)
    df_eventos    = obtener_candles(TIMEFRAME_EVENTOS)
    if df_estructura.empty or df_eventos.empty:
        print("Datos insuficientes, reintentando en el próximo ciclo.")
        return

    # 2. Precio actual (usamos el cierre de la última vela de eventos, más actual)
    precio_actual = df_eventos["close"].iloc[-1]

    # 3. Detectar zonas de liquidez
    zonas_high, zonas_low = detectar_zonas_liquidez(df_estructura)
    zonas_arriba, zonas_abajo = seleccionar_zonas_relevantes(zonas_high, zonas_low, precio_actual)

    # 4. Verificar si la zona principal cambió (para Radar 1)
    # Usamos la primera de arriba y la primera de abajo como representativas
    nueva_zona = (zonas_arriba[0]["centro"] if zonas_arriba else None,
                  zonas_abajo[0]["centro"] if zonas_abajo else None)
    if nueva_zona != zona_actual:
        zona_actual = nueva_zona
        alerted_liquidity.clear()          # Nuevas zonas, reiniciamos alertas
        zona_consumida = False
        radar_1_nueva_zona(zonas_arriba, zonas_abajo, precio_actual)

    # 5. Ejecutar radares en orden lógico
    radar_0_impulso(df_eventos, precio_actual, zonas_arriba, zonas_abajo)
    radar_2_proximidad(precio_actual, zonas_arriba, zonas_abajo)
    radar_3_sweep(df_eventos, zonas_arriba, zonas_abajo, precio_actual)
    radar_4_breakout(df_eventos, zonas_arriba, zonas_abajo, precio_actual)

    # 6. Alertas de sistema
    heartbeat()
    sin_eventos()

# =============================================================================
# PUNTO DE ENTRADA
# =============================================================================

if __name__ == "__main__":
    startup()
    while True:
        try:
            evaluar()
        except Exception as e:
            print(f"Error en ciclo principal: {e}")
            enviar_telegram(f"⚠️ ERROR EN BOT: {str(e)[:100]}")
            time.sleep(60)
        time.sleep(60)
