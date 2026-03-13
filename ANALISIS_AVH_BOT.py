import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import logging

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Variables de entorno
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not TOKEN or not CHAT_ID:
    raise ValueError("Faltan TOKEN o CHAT_ID en las variables de entorno")

# Parámetros del análisis
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
LIMIT = 1000  # Máximo por request de Binance
TOTAL_VELAS = 1000  # REDUCIDO TEMPORALMENTE para prueba rápida (aprox 3.5 días de datos 5m)
MOVIMIENTO_MIN = 0.7  # % mínimo de movimiento neto
RELACION_CUERPO_MIN = 0.7  # Relación mínima cuerpo/rango

# Función para obtener velas de Binance
def obtener_velas(symbol, interval, limit=1000, end_time=None):
    """
    Obtiene velas de Binance. Si se proporciona end_time (timestamp en ms),
    obtiene velas hasta esa fecha.
    """
    base_url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    if end_time:
        params["endTime"] = end_time
    
    try:
        logger.info(f"Solicitando velas con end_time={end_time}")
        response = requests.get(base_url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        if not data:
            logger.warning("Respuesta vacía de Binance")
            return None
        # Convertir a DataFrame
        df = pd.DataFrame(data, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])
        # Seleccionar columnas útiles y convertir tipos
        df = df[['open_time', 'open', 'high', 'low', 'close', 'volume']].copy()
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        logger.info(f"Obtenidas {len(df)} velas")
        return df
    except requests.exceptions.Timeout:
        logger.error("Timeout en la solicitud a Binance")
        return None
    except Exception as e:
        logger.error(f"Error al obtener velas: {e}")
        return None

# Función para recolectar muchas velas (paginar hacia atrás)
def recolectar_velas(symbol, interval, total_velas, limit=1000):
    """
    Recolecta un total aproximado de velas hacia atrás en el tiempo.
    """
    all_velas = []
    end_time = None  # empezamos desde ahora (sin end_time)
    intentos_fallidos = 0
    
    while len(all_velas) < total_velas:
        logger.info(f"Progreso: {len(all_velas)} velas obtenidas de {total_velas}")
        df = obtener_velas(symbol, interval, limit, end_time)
        if df is None or df.empty:
            intentos_fallidos += 1
            if intentos_fallidos >= 3:
                logger.error("Demasiados intentos fallidos, abortando recolección")
                break
            logger.warning(f"Intento fallido {intentos_fallidos}/3, esperando 2 segundos...")
            time.sleep(2)
            continue
        intentos_fallidos = 0
        all_velas.append(df)
        # Actualizar end_time al open_time de la primera vela (la más antigua de este lote)
        # Necesitamos el timestamp en ms de la primera vela - 1ms para evitar duplicados
        primer_open = df.iloc[0]['open_time']
        end_time = int(primer_open.timestamp() * 1000) - 1
        # Pequeña pausa para no saturar la API
        time.sleep(0.2)
    
    if not all_velas:
        return None
    
    # Combinar todos los DataFrames y eliminar duplicados
    logger.info("Combinando todos los lotes...")
    df_total = pd.concat(all_velas, ignore_index=True)
    df_total.drop_duplicates(subset=['open_time'], keep='first', inplace=True)
    df_total.sort_values('open_time', ascending=True, inplace=True)
    # Tomar las últimas 'total_velas'
    df_total = df_total.tail(total_velas)
    logger.info(f"Total final de velas: {len(df_total)}")
    return df_total

# Función para calcular métricas y filtrar velas
def analizar_velas(df):
    """
    Calcula movimiento porcentual, relación cuerpo/rango y filtra las velas
    que cumplen las condiciones.
    Retorna un DataFrame con las velas filtradas y las estadísticas de volumen.
    """
    if df is None or df.empty:
        return None, None
    
    # Calcular movimiento neto en porcentaje respecto a la apertura
    df['mov_pct'] = (df['close'] - df['open']) / df['open'] * 100
    # Rango en porcentaje
    df['rango_pct'] = (df['high'] - df['low']) / df['open'] * 100
    # Relación cuerpo/rango (evitar división por cero)
    df['rel_cuerpo'] = abs(df['close'] - df['open']) / (df['high'] - df['low'])
    df['rel_cuerpo'].replace([np.inf, -np.inf], 0, inplace=True)
    df['rel_cuerpo'].fillna(0, inplace=True)
    
    # Filtrar
    condicion = (abs(df['mov_pct']) >= MOVIMIENTO_MIN) & (df['rel_cuerpo'] >= RELACION_CUERPO_MIN)
    velas_filtradas = df[condicion].copy()
    
    if velas_filtradas.empty:
        logger.warning("No se encontraron velas que cumplan las condiciones")
        return velas_filtradas, None
    
    # Calcular estadísticas de volumen
    stats = {
        'total_velas_analizadas': len(df),
        'velas_que_cumplen': len(velas_filtradas),
        'media_vol': velas_filtradas['volume'].mean(),
        'mediana_vol': velas_filtradas['volume'].median(),
        'percentil_25': velas_filtradas['volume'].quantile(0.25),
        'percentil_75': velas_filtradas['volume'].quantile(0.75),
        'percentil_90': velas_filtradas['volume'].quantile(0.90),
        'max_vol': velas_filtradas['volume'].max(),
        'min_vol': velas_filtradas['volume'].min(),
    }
    
    return velas_filtradas, stats

# Función para enviar mensaje por Telegram
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        'chat_id': CHAT_ID,
        'text': mensaje,
        'parse_mode': 'HTML'
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Mensaje enviado correctamente por Telegram")
    except Exception as e:
        logger.error(f"Error al enviar mensaje por Telegram: {e}")

# Función principal
def main():
    logger.info("=== INICIANDO ANÁLISIS DE VOLUMEN ===")
    logger.info(f"Configuración: {SYMBOL} {INTERVAL}, velas a obtener: {TOTAL_VELAS}")
    
    # Recolectar datos
    logger.info("Iniciando descarga de velas desde Binance...")
    df_velas = recolectar_velas(SYMBOL, INTERVAL, TOTAL_VELAS, LIMIT)
    if df_velas is None:
        logger.error("No se pudieron obtener datos")
        enviar_telegram("❌ Error: No se pudieron obtener datos de Binance")
        return
    
    logger.info(f"Velas obtenidas correctamente: {len(df_velas)}")
    logger.info("Procesando datos y calculando estadísticas...")
    
    # Analizar
    velas_filtradas, stats = analizar_velas(df_velas)
    
    if stats is None:
        msg = f"📊 <b>Análisis de volumen para {SYMBOL} ({INTERVAL})</b>\n\n"
        msg += f"Período analizado: {df_velas['open_time'].min().strftime('%Y-%m-%d')} a {df_velas['open_time'].max().strftime('%Y-%m-%d')}\n"
        msg += f"Total velas: {len(df_velas)}\n"
        msg += "No se encontraron velas que cumplan las condiciones de movimiento decidido."
        enviar_telegram(msg)
        logger.info("No se encontraron velas que cumplan condiciones")
        return
    
    # Construir mensaje
    fecha_inicio = df_velas['open_time'].min().strftime('%Y-%m-%d %H:%M')
    fecha_fin = df_velas['open_time'].max().strftime('%Y-%m-%d %H:%M')
    
    msg = f"📊 <b>Análisis de volumen para {SYMBOL} ({INTERVAL})</b>\n\n"
    msg += f"Período: {fecha_inicio} a {fecha_fin}\n"
    msg += f"Velas analizadas: {stats['total_velas_analizadas']}\n"
    msg += f"Velas con movimiento ≥{MOVIMIENTO_MIN}% y cuerpo/rango ≥{RELACION_CUERPO_MIN}: {stats['velas_que_cumplen']}\n\n"
    msg += "<b>Estadísticas de volumen (BTC) en esas velas:</b>\n"
    msg += f"• Media: {stats['media_vol']:.2f}\n"
    msg += f"• Mediana: {stats['mediana_vol']:.2f}\n"
    msg += f"• Percentil 25: {stats['percentil_25']:.2f}\n"
    msg += f"• Percentil 75: {stats['percentil_75']:.2f}\n"
    msg += f"• Percentil 90: {stats['percentil_90']:.2f}\n"
    msg += f"• Mínimo: {stats['min_vol']:.2f}\n"
    msg += f"• Máximo: {stats['max_vol']:.2f}\n\n"
    
    # Interpretación orientada a tu observación de 400 BTC
    if stats['percentil_75'] >= 400:
        msg += "🔍 <b>Observación:</b> El valor de 400 BTC se encuentra por debajo del percentil 75, "
        msg += "lo que indica que aproximadamente el 75% de los movimientos decididos ocurren con volumen ≤ 400 BTC. "
        msg += "Si buscas mayor probabilidad, considera usar el percentil 75 como umbral."
    else:
        msg += "🔍 <b>Observación:</b> El valor de 400 BTC está por encima del percentil 75, "
        msg += "lo que significa que más del 75% de los movimientos decididos requieren menos volumen. "
        msg += "Podrías reducir el umbral para capturar más operaciones."
    
    enviar_telegram(msg)
    logger.info("=== ANÁLISIS COMPLETADO ===")

if __name__ == "__main__":
    main()
