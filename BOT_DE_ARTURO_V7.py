# -*- coding: utf-8 -*-
import requests
import pandas as pd
import time
import os
import numpy as np

print("BOT_DE_ARTURO V7 iniciado 🚀")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"
INTERVAL = "5m"

ultimo_radar1 = None
ultimo_radar2 = None
ultimo_radar3 = None
ultimo_radar4 = None
ultimo_radar5 = None  # Sweep Probability Engine


def enviar_mensaje(msg):
    """Envía mensaje a Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print(f"Error enviando mensaje: {e}")


def obtener_candles():
    """Obtiene velas de Binance"""
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": 200}
    
    try:
        data = requests.get(url, params=params).json()
        df = pd.DataFrame(data, columns=[
            "time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "number_of_trades",
            "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"
        ])
        
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        
        return df
    except Exception as e:
        print(f"Error obteniendo velas: {e}")
        return None


def calcular_liquidity_score(df, lookback=50):
    """
    Calcula un score de liquidez para highs y lows recientes.
    """
    try:
        if df is None or len(df) < lookback:
            return {}, {}
            
        highs = df['high'].tail(lookback)
        lows = df['low'].tail(lookback)
        score_highs = {}
        score_lows = {}

        mean_high = highs.mean()
        mean_low = lows.mean()
        tolerance = df['close'].iloc[-1] * 0.001  # 0.1% tolerancia

        # Highs
        for h in highs.unique():
            touches = sum(highs == h)
            distance = abs(df['close'].iloc[-1] - h)
            visibility = touches
            # Evitar división por cero en el score
            score = min(9, int(touches + max(0, (tolerance - distance) * 1000) + visibility))
            score_highs[h] = score

        # Lows
        for l in lows.unique():
            touches = sum(lows == l)
            distance = abs(df['close'].iloc[-1] - l)
            visibility = touches
            score = min(9, int(touches + max(0, (tolerance - distance) * 1000) + visibility))
            score_lows[l] = score

        return score_highs, score_lows
    except Exception as e:
        print(f"Error en calcular_liquidity_score: {e}")
        return {}, {}


def detectar_compresion(df, lookback=20):
    """
    Detecta compresión: lower highs / higher lows y reducción de volatilidad.
    """
    try:
        if df is None or len(df) < lookback:
            return False, 0
            
        df_recent = df.tail(lookback)
        high_range = df_recent['high'].max()
        low_range = df_recent['low'].min()
        rango = high_range - low_range
        vol_sma = df_recent['volume'].mean()
        vol_actual = df_recent['volume'].iloc[-1]
        compresion = (rango < (df_recent['close'].iloc[-1] * 0.002)) and (vol_actual < vol_sma)
        return compresion, rango
    except Exception as e:
        print(f"Error en detectar_compresion: {e}")
        return False, 0


def evaluar():
    global ultimo_radar1, ultimo_radar2, ultimo_radar3, ultimo_radar4, ultimo_radar5

    df = obtener_candles()
    if df is None or len(df) < 50:  # Validar que tengamos suficientes datos
        print("Datos insuficientes, esperando...")
        return

    vela = df.iloc[-1]

    open_actual = vela["open"]
    high_actual = vela["high"]
    low_actual = vela["low"]
    close_actual = vela["close"]
    volumen = vela["volume"]
    tiempo_actual = vela["time"]

    # =================================
    # RADAR 1: Sweep básico
    # =================================
    try:
        high_prev = df["high"].rolling(20).max().iloc[-2]
        low_prev = df["low"].rolling(20).min().iloc[-2]
        sweep_high = high_actual > high_prev and close_actual < high_prev
        sweep_low = low_actual < low_prev and close_actual > low_prev

        sma_vol = df["volume"].rolling(20).mean().iloc[-1]
        volumen_fuerte = volumen > sma_vol * 1.3

        if volumen_fuerte and ultimo_radar1 != tiempo_actual:
            if sweep_high:
                enviar_mensaje(f"⚠️ RADAR 1\nSweep HIGH detectado\nVolumen institucional\nBTC {INTERVAL}\nPrecio: {close_actual:.2f}")
                ultimo_radar1 = tiempo_actual
            elif sweep_low:
                enviar_mensaje(f"⚠️ RADAR 1\nSweep LOW detectado\nVolumen institucional\nBTC {INTERVAL}\nPrecio: {close_actual:.2f}")
                ultimo_radar1 = tiempo_actual
    except Exception as e:
        print(f"Error en RADAR 1: {e}")

    # =================================
    # RADAR 2: Rechazo fuerte
    # =================================
    try:
        rango = high_actual - low_actual
        cuerpo = abs(close_actual - open_actual)
        wick_ratio = rango / cuerpo if cuerpo != 0 else 0  # CORREGIDO: evitar división por cero
        rechazo_fuerte = wick_ratio > 2.5

        if volumen_fuerte and rechazo_fuerte and ultimo_radar2 != tiempo_actual:
            if sweep_high:
                enviar_mensaje(f"🚨 RADAR 2\nSweep HIGH confirmado\nRechazo fuerte\nBTC {INTERVAL}\nPrecio: {close_actual:.2f}")
                ultimo_radar2 = tiempo_actual
            elif sweep_low:
                enviar_mensaje(f"🚨 RADAR 2\nSweep LOW confirmado\nRechazo fuerte\nBTC {INTERVAL}\nPrecio: {close_actual:.2f}")
                ultimo_radar2 = tiempo_actual
    except Exception as e:
        print(f"Error en RADAR 2: {e}")

    # =================================
    # RADAR 3: Liquidity Magnet Score
    # =================================
    try:
        score_highs, score_lows = calcular_liquidity_score(df)
        precio_actual = close_actual
        threshold = precio_actual * 0.001

        if ultimo_radar3 != tiempo_actual:
            for h, s in score_highs.items():
                if abs(precio_actual - h) < threshold and s >= 6:
                    enviar_mensaje(f"🧲 RADAR 3\nImán de liquidez ARRIBA\nScore: {s}\nBTC {INTERVAL}\nPrecio actual: {precio_actual:.2f}\nZona liquidez: {h:.2f}")
                    ultimo_radar3 = tiempo_actual
                    break
            for l, s in score_lows.items():
                if abs(precio_actual - l) < threshold and s >= 6:
                    enviar_mensaje(f"🧲 RADAR 3\nImán de liquidez ABAJO\nScore: {s}\nBTC {INTERVAL}\nPrecio actual: {precio_actual:.2f}\nZona liquidez: {l:.2f}")
                    ultimo_radar3 = tiempo_actual
                    break
    except Exception as e:
        print(f"Error en RADAR 3: {e}")

    # =================================
    # RADAR 4: Pool de Liquidez (mejorado)
    # =================================
    try:
        tolerance = close_actual * 0.0007
        highs = df['high'].tail(30)
        lows = df['low'].tail(30)
        
        # Detectar highs iguales (dentro de tolerancia)
        equal_highs = []
        for i in range(len(highs)):
            for j in range(i+1, len(highs)):
                if abs(highs.iloc[i] - highs.iloc[j]) < tolerance:
                    equal_highs.append(highs.iloc[i])
        
        equal_lows = []
        for i in range(len(lows)):
            for j in range(i+1, len(lows)):
                if abs(lows.iloc[i] - lows.iloc[j]) < tolerance:
                    equal_lows.append(lows.iloc[i])

        if ultimo_radar4 != tiempo_actual:
            if len(set(equal_highs)) >= 3:  # Al menos 3 niveles únicos de iguales
                enviar_mensaje(f"💰 RADAR 4\nPOOL DE LIQUIDEZ ARRIBA\nEqual Highs detectados\nBTC {INTERVAL}")
                ultimo_radar4 = tiempo_actual
            elif len(set(equal_lows)) >= 3:
                enviar_mensaje(f"💰 RADAR 4\nPOOL DE LIQUIDEZ ABAJO\nEqual Lows detectados\nBTC {INTERVAL}")
                ultimo_radar4 = tiempo_actual
    except Exception as e:
        print(f"Error en RADAR 4: {e}")

    # =================================
    # RADAR 5: Sweep Probability Engine (v7)
    # =================================
    try:
        compresion, rango_compresion = detectar_compresion(df)
        
        # CORREGIDO: Calcular distancias de forma segura
        distancia_high = float('inf')
        distancia_low = float('inf')
        
        if score_highs:
            distancias_high = [abs(precio_actual - h) for h in score_highs.keys()]
            distancia_high = min(distancias_high) if distancias_high else float('inf')
        
        if score_lows:
            distancias_low = [abs(precio_actual - l) for l in score_lows.keys()]
            distancia_low = min(distancias_low) if distancias_low else float('inf')

        sweep_prob_high = 0
        sweep_prob_low = 0

        if score_highs:
            max_score_high = max(score_highs.values())
            # CORREGIDO: evitar división por cero en distancia
            factor_distancia = (1 + 1000/(distancia_high + 1e-10)) if distancia_high != float('inf') else 1
            sweep_prob_high = min(100, int(max_score_high * (1 + int(compresion)) * factor_distancia))
        
        if score_lows:
            max_score_low = max(score_lows.values())
            factor_distancia = (1 + 1000/(distancia_low + 1e-10)) if distancia_low != float('inf') else 1
            sweep_prob_low = min(100, int(max_score_low * (1 + int(compresion)) * factor_distancia))

        if ultimo_radar5 != tiempo_actual:
            if sweep_prob_high >= 60:
                enviar_mensaje(f"📊 RADAR 5\nSweep Probability HIGH: {sweep_prob_high}%\nBTC {INTERVAL}\nPrecio actual: {precio_actual:.2f}")
                ultimo_radar5 = tiempo_actual
            elif sweep_prob_low >= 60:
                enviar_mensaje(f"📊 RADAR 5\nSweep Probability LOW: {sweep_prob_low}%\nBTC {INTERVAL}\nPrecio actual: {precio_actual:.2f}")
                ultimo_radar5 = tiempo_actual
    except Exception as e:
        print(f"Error en RADAR 5: {e}")


# =================================
# LOOP PRINCIPAL
# =================================
if __name__ == "__main__":
    print("Bot iniciado. Verificando cada 60 segundos...")
    
    # Verificar credenciales
    if not TOKEN or not CHAT_ID:
        print("❌ ERROR: TOKEN o CHAT_ID no configurados")
        print("Asegúrate de tener las variables de entorno configuradas")
    else:
        enviar_mensaje("🤖 Bot de Arturo V7 iniciado")
        
    while True:
        try:
            evaluar()
        except Exception as e:
            print(f"Error en loop principal: {e}")
        time.sleep(60)
