import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import time
import logging

# ================= CONFIGURACIÓN INICIAL =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Variables de entorno (deben estar definidas en Railway)
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not TOKEN or not CHAT_ID:
    raise ValueError("Faltan las variables de entorno TOKEN o CHAT_ID")

# Parámetros del análisis (puedes modificarlos)
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
TOTAL_VELAS = 1000          # Número de velas a analizar (para prueba rápida, luego puedes subir a 10000)
MOVIMIENTO_MIN = 0.7         # % mínimo de movimiento neto (cierre - apertura)
RELACION_CUERPO_MIN = 0.7    # Relación mínima cuerpo / rango total

# ================= FUNCIONES =================

def obtener_velas(symbol, interval, limit=1000, end_time=None):
    """
    Obtiene un lote de velas desde Binance.
    Si se proporciona end_time (timestamp en ms), obtiene velas hasta esa fecha.
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
        logger.debug(f"Consultando Binance con end_time={end_time}")
        response = requests.get(base_url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        if not data:
            logger.warning("Binance devolvió una lista vacía")
            return None

        # Convertir a DataFrame
        df = pd.DataFrame(data, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])
        df = df[['open_time', 'open', 'high', 'low', 'close', 'volume']].copy()
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')

        return df

    except requests.exceptions.Timeout:
        logger.error("Timeout al conectar con Binance")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error en la petición a Binance: {e}")
        return None
    except Exception as e:
        logger.error(f"Error inesperado al procesar velas: {e}")
        return None


def recolectar_velas(symbol, interval, total_velas, limit=1000):
    """
    Recolecta exactamente 'total_velas' velas (o las máximas posibles)
    navegando hacia atrás en el tiempo.
    """
    inicio_total = time.time()
    lotes = []
    end_time = None
    acumulado = 0
    intentos_fallidos = 0
    max_intentos_fallidos = 3

    logger.info(f"Iniciando recolección de {total_velas} velas {interval} de {symbol}")

    while acumulado < total_velas and intentos_fallidos < max_intentos_fallidos:
        logger.info(f"Solicitando lote con end_time={end_time if end_time else 'ahora'}")
        df = obtener_velas(symbol, interval, limit, end_time)

        if df is None or df.empty:
            intentos_fallidos += 1
            logger.warning(f"Intento fallido {intentos_fallidos}/{max_intentos_fallidos}")
            time.sleep(1)
            continue

        # Si es el primer lote o hay continuidad, añadimos
        lotes.append(df)
        acumulado += len(df)
        logger.info(f"Lote obtenido: {len(df)} velas. Acumulado: {acumulado}/{total_velas}")

        # Preparamos el próximo end_time (anterior a la primera vela de este lote)
        primer_open = df.iloc[0]['open_time']
        end_time = int(primer_open.timestamp() * 1000) - 1
        intentos_fallidos = 0  # Reiniciamos contador de fallos

        # Si la API devolvió menos velas de las que pedimos (último lote histórico), salimos
        if len(df) < limit:
            logger.info("Se alcanzó el final de los datos históricos disponibles.")
            break

        time.sleep(0.1)  # Pequeña pausa para no saturar la API

    if not lotes:
        logger.error("No se pudo obtener ninguna vela")
        return None

    # Combinar todos los lotes y limpiar duplicados
    logger.info("Combinando lotes y eliminando duplicados...")
    df_total = pd.concat(lotes, ignore_index=True)
    df_total.drop_duplicates(subset=['open_time'], keep='first', inplace=True)
    df_total.sort_values('open_time', ascending=True, inplace=True)

    # Ajustar al número exacto solicitado (tomar las más recientes)
    if len(df_total) > total_velas:
        df_total = df_total.tail(total_velas)
        logger.info(f"Recortado a las últimas {total_velas} velas.")
    else:
        logger.info(f"Solo se pudieron obtener {len(df_total)} velas (menos de las solicitadas).")

    fin_total = time.time()
    logger.info(f"Recolección completada en {fin_total - inicio_total:.2f} segundos. Velas finales: {len(df_total)}")
    return df_total


def analizar_velas(df):
    """
    Calcula el movimiento porcentual, la relación cuerpo/rango y filtra
    las velas que cumplen las condiciones. Retorna estadísticas de volumen.
    """
    if df is None or df.empty:
        return None, None

    # Evitar división por cero en open (muy raro pero por si acaso)
    df = df[df['open'] != 0].copy()

    # Movimiento neto en porcentaje
    df['mov_pct'] = (df['close'] - df['open']) / df['open'] * 100

    # Rango total en porcentaje
    df['rango_pct'] = (df['high'] - df['low']) / df['open'] * 100

    # Relación cuerpo / rango (evitar división por cero)
    df['rel_cuerpo'] = abs(df['close'] - df['open']) / (df['high'] - df['low'])
    df['rel_cuerpo'].replace([np.inf, -np.inf], 0, inplace=True)
    df['rel_cuerpo'].fillna(0, inplace=True)

    # Filtrar por condiciones
    condicion = (abs(df['mov_pct']) >= MOVIMIENTO_MIN) & (df['rel_cuerpo'] >= RELACION_CUERPO_MIN)
    velas_filtradas = df[condicion].copy()

    if velas_filtradas.empty:
        logger.warning("No se encontraron velas que cumplan las condiciones.")
        return velas_filtradas, None

    # Estadísticas de volumen
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

    logger.info(f"Análisis completado: {stats['velas_que_cumplen']} velas cumplen condiciones.")
    return velas_filtradas, stats


def enviar_telegram(mensaje):
    """Envía un mensaje a Telegram usando el bot configurado."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        'chat_id': CHAT_ID,
        'text': mensaje,
        'parse_mode': 'HTML'
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Mensaje de Telegram enviado correctamente.")
    except Exception as e:
        logger.error(f"Error al enviar mensaje por Telegram: {e}")


def main():
    logger.info("=== INICIANDO ANÁLISIS DE VOLUMEN ===")
    logger.info(f"Configuración: {SYMBOL} {INTERVAL}, velas a obtener: {TOTAL_VELAS}")

    # Recolectar datos
    df_velas = recolectar_velas(SYMBOL, INTERVAL, TOTAL_VELAS, limit=1000)
    if df_velas is None or df_velas.empty:
        logger.error("No se obtuvieron datos. Abortando.")
        enviar_telegram("❌ Error: No se pudieron obtener datos de Binance.")
        return

    # Analizar
    velas_filtradas, stats = analizar_velas(df_velas)

    # Construir mensaje para Telegram
    fecha_inicio = df_velas['open_time'].min().strftime('%Y-%m-%d %H:%M')
    fecha_fin = df_velas['open_time'].max().strftime('%Y-%m-%d %H:%M')

    if stats is None:
        msg = f"📊 <b>Análisis de volumen para {SYMBOL} ({INTERVAL})</b>\n\n"
        msg += f"Período analizado: {fecha_inicio} a {fecha_fin}\n"
        msg += f"Total velas: {len(df_velas)}\n"
        msg += "No se encontraron velas que cumplan las condiciones de movimiento decidido."
        enviar_telegram(msg)
        return

    # Estadísticas disponibles
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

    # Interpretación con tu observación de 400 BTC
    if stats['percentil_75'] >= 400:
        msg += "🔍 <b>Observación:</b> El valor de 400 BTC se encuentra por debajo del percentil 75, "
        msg += "lo que indica que aproximadamente el 75% de los movimientos decididos ocurren con volumen ≤ 400 BTC. "
        msg += "Si buscas mayor probabilidad, considera usar el percentil 75 como umbral."
    else:
        msg += "🔍 <b>Observación:</b> El valor de 400 BTC está por encima del percentil 75, "
        msg += "lo que significa que más del 75% de los movimientos decididos requieren menos volumen. "
        msg += "Podrías reducir el umbral para capturar más operaciones."

    enviar_telegram(msg)
    logger.info("=== ANÁLISIS FINALIZADO ===")


if __name__ == "__main__":
    # Ejecutar una vez (Railway lanzará el script y terminará)
    # Si quieres que se ejecute cada cierto tiempo, puedes descomentar el bucle siguiente:
    # while True:
    #     main()
    #     time.sleep(86400)  # 24 horas
    main()
