# -*- coding: utf-8 -*-
import requests
import pandas as pd
import time
import os
import numpy as np
from collections import defaultdict

print("BOT_DE_ARTURO V7 (mejorado) iniciado 🚀")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"
INTERVAL = "5m"

# Variables para control de cooldown
ultimo_radar1 = None
ultimo_radar2 = None
ultimo_radar3_tiempo = 0          # último timestamp de cualquier alerta RADAR 3
ultimo_radar3_niveles = {}         # dict: {nivel: timestamp} para cooldown por nivel
ultimo_radar4_niveles = {}         # dict: {nivel: timestamp} para cooldown por pool
ultimo_radar5 = None               # cooldown para stop hunt
ultimo_radar6 = None               # cooldown para probabilidad

# Para detectar sweep en velas anteriores (usado en RADAR 5)
historial_sweeps = []  # lista de (tiempo, direccion) donde direccion = 'HIGH' o 'LOW'

# Cooldowns en segundos
COOLDOWN_RADAR3_GLOBAL = 300       # 5 minutos
COOLDOWN_RADAR3_NIVEL = 1800       # 30 minutos
COOLDOWN_RADAR4_NIVEL = 3600       # 60 minutos
COOLDOWN_RADAR5 = 1800             # 30 minutos
COOLDOWN_RADAR6 = 60                # 1 minuto (por vela)

def enviar_mensaje(msg):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print(f"Error enviando mensaje: {e}")

def obtener_candles():
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

def calcular_atr(df, periodo=14):
    """Calcula el ATR (Average True Range)"""
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(periodo).mean().iloc[-1]
    return atr

def calcular_liquidity_score(df, lookback=50):
    try:
        if df is None or len(df) < lookback:
            return {}, {}
        highs = df['high'].tail(lookback)
        lows = df['low'].tail(lookback)
        score_highs = {}
        score_lows = {}
        tolerance = df['close'].iloc[-1] * 0.001  # 0.1%

        for h in highs.unique():
            touches = sum(highs == h)
            distance = abs(df['close'].iloc[-1] - h)
            score = min(9, int(touches + max(0, (tolerance - distance) * 1000) + touches))
            score_highs[h] = score

        for l in lows.unique():
            touches = sum(lows == l)
            distance = abs(df['close'].iloc[-1] - l)
            score = min(9, int(touches + max(0, (tolerance - distance) * 1000) + touches))
            score_lows[l] = score

        return score_highs, score_lows
    except Exception as e:
        print(f"Error en calcular_liquidity_score: {e}")
        return {}, {}

def detectar_compresion(df, lookback=20):
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
    global ultimo_radar1, ultimo_radar2, ultimo_radar3_tiempo, ultimo_radar3_niveles
    global ultimo_radar4_niveles, ultimo_radar5, ultimo_radar6, historial_sweeps

    df = obtener_candles()
    if df is None or len(df) < 50:
        print("Datos insuficientes")
        return

    vela = df.iloc[-1]
    open_actual = vela["open"]
    high_actual = vela["high"]
    low_actual = vela["low"]
    close_actual = vela["close"]
    volumen = vela["volume"]
    tiempo_actual = vela["time"]

    # Calcular medias y ATR
    sma_vol = df["volume"].rolling(20).mean().iloc[-1]
    volumen_fuerte = volumen > sma_vol * 1.3
    atr = calcular_atr(df)

    # =================================
    # RADAR 1: Sweep básico + volumen
    # =================================
    try:
        high_prev = df["high"].rolling(20).max().iloc[-2]
        low_prev = df["low"].rolling(20).min().iloc[-2]
        sweep_high = high_actual > high_prev and close_actual < high_prev
        sweep_low = low_actual < low_prev and close_actual > low_prev

        # Guardar sweep en historial para RADAR 5
        if sweep_high and volumen_fuerte:
            historial_sweeps.append((tiempo_actual, 'HIGH'))
        if sweep_low and volumen_fuerte:
            historial_sweeps.append((tiempo_actual, 'LOW'))
        # Limpiar historial antiguo (mayor a 3 velas)
        historial_sweeps = [(t, d) for t, d in historial_sweeps if tiempo_actual - t < 3 * 60 * 1000]  # 3 velas en ms

        if volumen_fuerte and ultimo_radar1 != tiempo_actual:
            if sweep_high:
                enviar_mensaje(f"⚠️ RADAR 1\nSweep HIGH detectado\nVolumen institucional\nBTC {INTERVAL}\nPrecio: {close_actual:.2f}")
                ultimo_radar1 = tiempo_actual
            elif sweep_low:
                enviar_mensaje(f"⚠️ RADAR 1\nSweep LOW detectado\nVolumen institucional\nBTC {INTERVAL}\nPrecio: {close_actual:.2f}")
                ultimo_radar1 = tiempo_actual
    except Exception as e:
        print(f"Error RADAR 1: {e}")

    # =================================
    # RADAR 2: Sweep + rechazo fuerte
    # =================================
    try:
        rango = high_actual - low_actual
        cuerpo = abs(close_actual - open_actual)
        wick_ratio = rango / cuerpo if cuerpo != 0 else 0
        rechazo_fuerte = wick_ratio > 2.5

        if volumen_fuerte and rechazo_fuerte and ultimo_radar2 != tiempo_actual:
            if sweep_high:
                enviar_mensaje(f"🚨 RADAR 2\nSweep HIGH confirmado\nRechazo fuerte\nBTC {INTERVAL}\nPrecio: {close_actual:.2f}")
                ultimo_radar2 = tiempo_actual
            elif sweep_low:
                enviar_mensaje(f"🚨 RADAR 2\nSweep LOW confirmado\nRechazo fuerte\nBTC {INTERVAL}\nPrecio: {close_actual:.2f}")
                ultimo_radar2 = tiempo_actual
    except Exception as e:
        print(f"Error RADAR 2: {e}")

    # =================================
    # RADAR 3: Imán de liquidez (con cooldown por nivel y global)
    # =================================
    try:
        score_highs, score_lows = calcular_liquidity_score(df)
        precio_actual = close_actual
        threshold = precio_actual * 0.001  # 0.1%

        # Verificar cooldown global (5 minutos)
        if time.time() - ultimo_radar3_tiempo > COOLDOWN_RADAR3_GLOBAL:
            # Revisar niveles altos
            for h, s in score_highs.items():
                if abs(precio_actual - h) < threshold and s >= 6:
                    nivel = round(h, 2)
                    # Cooldown por nivel
                    if nivel not in ultimo_radar3_niveles or (time.time() - ultimo_radar3_niveles[nivel] > COOLDOWN_RADAR3_NIVEL):
                        enviar_mensaje(f"🧲 RADAR 3\nImán de liquidez ARRIBA\nScore: {s}\nBTC {INTERVAL}\nPrecio actual: {precio_actual:.2f}\nZona liquidez: {nivel}")
                        ultimo_radar3_tiempo = time.time()
                        ultimo_radar3_niveles[nivel] = time.time()
                        break  # Solo una alerta por ciclo (la primera encontrada)
            # Revisar niveles bajos (si no se activó arriba)
            for l, s in score_lows.items():
                if abs(precio_actual - l) < threshold and s >= 6:
                    nivel = round(l, 2)
                    if nivel not in ultimo_radar3_niveles or (time.time() - ultimo_radar3_niveles[nivel] > COOLDOWN_RADAR3_NIVEL):
                        enviar_mensaje(f"🧲 RADAR 3\nImán de liquidez ABAJO\nScore: {s}\nBTC {INTERVAL}\nPrecio actual: {precio_actual:.2f}\nZona liquidez: {nivel}")
                        ultimo_radar3_tiempo = time.time()
                        ultimo_radar3_niveles[nivel] = time.time()
                        break
    except Exception as e:
        print(f"Error RADAR 3: {e}")

    # =================================
    # RADAR 4: Pool de liquidez (con nivel y frecuencia)
    # =================================
    try:
        tolerance = close_actual * 0.0007  # 0.07%
        # Tomar últimas 30 velas
        highs = df['high'].tail(30).round(2)
        lows = df['low'].tail(30).round(2)

        # Función para agrupar niveles cercanos
        def agrupar_niveles(series, tol):
            if len(series) == 0:
                return []
            # Ordenar y agrupar
            series = series.sort_values().values
            grupos = []
            grupo_actual = [series[0]]
            for val in series[1:]:
                if val - grupo_actual[-1] <= tol:
                    grupo_actual.append(val)
                else:
                    grupos.append(grupo_actual)
                    grupo_actual = [val]
            grupos.append(grupo_actual)
            # Filtrar grupos con al menos 3 valores
            grupos = [g for g in grupos if len(g) >= 3]
            # Calcular nivel promedio y frecuencia
            resultados = []
            for g in grupos:
                nivel_prom = sum(g) / len(g)
                frecuencia = len(g)
                resultados.append((nivel_prom, frecuencia))
            return resultados

        pools_high = agrupar_niveles(highs, tolerance)
        pools_low = agrupar_niveles(lows, tolerance)

        # Cooldown por nivel
        for nivel, freq in pools_high:
            nivel_redondo = round(nivel, 2)
            if nivel_redondo not in ultimo_radar4_niveles or (time.time() - ultimo_radar4_niveles[nivel_redondo] > COOLDOWN_RADAR4_NIVEL):
                enviar_mensaje(f"💰 RADAR 4\nPOOL DE LIQUIDEZ ARRIBA\nNivel: {nivel_redondo}\nVeces tocado: {freq}\nBTC {INTERVAL}")
                ultimo_radar4_niveles[nivel_redondo] = time.time()
                break  # Solo una alerta por ciclo

        for nivel, freq in pools_low:
            nivel_redondo = round(nivel, 2)
            if nivel_redondo not in ultimo_radar4_niveles or (time.time() - ultimo_radar4_niveles[nivel_redondo] > COOLDOWN_RADAR4_NIVEL):
                enviar_mensaje(f"💰 RADAR 4\nPOOL DE LIQUIDEZ ABAJO\nNivel: {nivel_redondo}\nVeces tocado: {freq}\nBTC {INTERVAL}")
                ultimo_radar4_niveles[nivel_redondo] = time.time()
                break
    except Exception as e:
        print(f"Error RADAR 4: {e}")

    # =================================
    # RADAR 5: Stop Hunt Institucional (sweep + displacement)
    # =================================
    try:
        # Buscar si hubo un sweep en la vela actual o anterior (dentro de las últimas 2 velas)
        sweep_reciente = None
        for t, direc in historial_sweeps:
            if tiempo_actual - t <= 2 * 60 * 1000:  # dentro de 2 velas
                sweep_reciente = direc
                break

        if sweep_reciente and (ultimo_radar5 is None or time.time() - ultimo_radar5 > COOLDOWN_RADAR5):
            # Verificar desplazamiento en dirección opuesta
            if sweep_reciente == 'HIGH':
                # Sweep arriba, esperamos desplazamiento bajista
                # Tomamos vela actual y anterior
                vela_anterior = df.iloc[-2]
                # Condición: cierre actual < mínimo de la vela de sweep o cierre anterior < ...?
                # Simplemente, que el precio haya bajado al menos un 0.3% desde el máximo del sweep
                high_sweep = df[df['time'] == t]['high'].values[0] if t in df['time'].values else None
                if high_sweep:
                    # Ver si el precio actual es al menos 0.3% más bajo que el high_sweep
                    if close_actual < high_sweep * 0.997:  # 0.3% abajo
                        # Además, volumen fuerte en la vela de desplazamiento
                        if volumen_fuerte:
                            enviar_mensaje(f"🔥 RADAR 5\nSTOP HUNT INSTITUCIONAL\nSweep HIGH + desplazamiento bajista\nLiquidez tomada: {high_sweep:.2f}\nPrecio actual: {close_actual:.2f}\nBTC {INTERVAL}")
                            ultimo_radar5 = time.time()
            elif sweep_reciente == 'LOW':
                low_sweep = df[df['time'] == t]['low'].values[0] if t in df['time'].values else None
                if low_sweep:
                    if close_actual > low_sweep * 1.003:  # 0.3% arriba
                        if volumen_fuerte:
                            enviar_mensaje(f"🔥 RADAR 5\nSTOP HUNT INSTITUCIONAL\nSweep LOW + desplazamiento alcista\nLiquidez tomada: {low_sweep:.2f}\nPrecio actual: {close_actual:.2f}\nBTC {INTERVAL}")
                            ultimo_radar5 = time.time()
    except Exception as e:
        print(f"Error RADAR 5: {e}")

    # =================================
    # RADAR 6: Sweep Probability Engine (mejorado)
    # =================================
    try:
        compresion, _ = detectar_compresion(df)
        score_highs, score_lows = calcular_liquidity_score(df)
        precio_actual = close_actual

        # Calcular distancia mínima a zonas de score alto
        def prob_direccion(scores, tipo):
            if not scores:
                return 0
            # Filtrar scores >= 5
            scores_filt = {k: v for k, v in scores.items() if v >= 5}
            if not scores_filt:
                return 0
            # Encontrar la zona con mayor score (o más cercana)
            mejor_score = max(scores_filt.values())
            # Tomar la zona con ese score más cercana
            candidatos = [k for k, v in scores_filt.items() if v == mejor_score]
            distancias = [abs(precio_actual - k) for k in candidatos]
            mejor_dist = min(distancias)
            # Normalizar distancia usando ATR
            if atr == 0:
                atr = precio_actual * 0.001  # fallback
            dist_norm = mejor_dist / atr  # distancia en términos de ATR
            # Probabilidad base: (mejor_score/9)*100
            prob_base = (mejor_score / 9) * 100
            # Factor de compresión: +10% si hay compresión
            factor_comp = 1.1 if compresion else 1.0
            # Factor de distancia: si está muy cerca, aumenta; si está lejos, disminuye
            # Usamos una función sigmoide inversa: 1 / (1 + dist_norm)
            factor_dist = 1 / (1 + dist_norm)
            prob = prob_base * factor_comp * factor_dist
            prob = min(100, int(prob))
            return prob

        prob_high = prob_direccion(score_highs, 'HIGH')
        prob_low = prob_direccion(score_lows, 'LOW')

        if (prob_high >= 60 or prob_low >= 60) and (ultimo_radar6 is None or time.time() - ultimo_radar6 > COOLDOWN_RADAR6):
            if prob_high >= 60:
                enviar_mensaje(f"📊 RADAR 6\nSweep Probability HIGH: {prob_high}%\nBTC {INTERVAL}\nPrecio actual: {precio_actual:.2f}")
                ultimo_radar6 = time.time()
            elif prob_low >= 60:
                enviar_mensaje(f"📊 RADAR 6\nSweep Probability LOW: {prob_low}%\nBTC {INTERVAL}\nPrecio actual: {precio_actual:.2f}")
                ultimo_radar6 = time.time()
    except Exception as e:
        print(f"Error RADAR 6: {e}")

# =================================
# LOOP PRINCIPAL
# =================================
if __name__ == "__main__":
    print("Bot iniciado. Verificando cada 60 segundos...")
    if not TOKEN or not CHAT_ID:
        print("❌ ERROR: TOKEN o CHAT_ID no configurados")
    else:
        enviar_mensaje("🤖 Bot de Arturo V7 (mejorado) iniciado")
    while True:
        try:
            evaluar()
        except Exception as e:
            print(f"Error en loop principal: {e}")
        time.sleep(60)
