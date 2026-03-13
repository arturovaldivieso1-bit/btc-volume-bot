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
TOTAL_VELAS = 20000          # Número de velas a analizar (~70 días para 5m)
RELACION_CUERPO_MIN = 0.7    # Relación mínima cuerpo / rango total
PERCENTIL_VOLUMEN = 75       # Percentil para recomendar umbral (ej. 75)
UMBRALES_MOVIMIENTO = [0.5, 0.7, 1.0]  # Lista de % a analizar

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

    logger.info(f"Recolectando {total_velas} velas {interval} de {symbol}")

    while acumulado < total_velas and intentos_fallidos < max_intentos_fallidos:
        logger.info(f"Solicitando lote con end_time={end_time if end_time else 'ahora'}")
        df = obtener_velas(symbol, interval, limit, end_time)

        if df is None or df.empty:
            intentos_fallidos += 1
            logger.warning(f"Intento fallido {intentos_fallidos}/{max_intentos_fallidos}")
            time.sleep(1)
            continue

        lotes.append(df)
        acumulado += len(df)
        logger.info(f"Lote: {len(df)} velas. Total: {acumulado}/{total_velas}")

        # Preparamos el próximo end_time (anterior a la primera vela de este lote)
        primer_open = df.iloc[0]['open_time']
        end_time = int(primer_open.timestamp() * 1000) - 1
        intentos_fallidos = 0

        if len(df) < limit:
            logger.info("Se alcanzó el final de los datos históricos disponibles.")
            break

        time.sleep(0.1)

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
        logger.info(f"Solo se pudieron obtener {len(df_total)} velas.")

    fin_total = time.time()
    logger.info(f"Recolección completada en {fin_total - inicio_total:.2f}s. Velas: {len(df_total)}")
    return df_total


def analizar_velas_por_umbral(df, umbral_mov, relacion_min):
    """
    Para un umbral de movimiento dado, filtra las velas y calcula estadísticas
    globales, alcistas y bajistas.
    Retorna un dict con estadísticas o None si no hay suficientes velas.
    """
    # Evitar división por cero en open
    df = df[df['open'] != 0].copy()

    # Calcular movimiento neto y relación cuerpo/rango
    df['mov_pct'] = (df['close'] - df['open']) / df['open'] * 100
    df['rango'] = df['high'] - df['low']
    # Evitar división por cero en rango
    df['rel_cuerpo'] = abs(df['close'] - df['open']) / df['rango'].replace(0, np.nan)
    df['rel_cuerpo'] = df['rel_cuerpo'].replace([np.inf, -np.inf], np.nan).fillna(0)

    # Filtrar por movimiento y relación cuerpo
    condicion = (abs(df['mov_pct']) >= umbral_mov) & (df['rel_cuerpo'] >= relacion_min)
    velas_filtradas = df[condicion].copy()

    if velas_filtradas.empty:
        return None

    # Separar por dirección
    alcistas = velas_filtradas[velas_filtradas['mov_pct'] > 0]
    bajistas = velas_filtradas[velas_filtradas['mov_pct'] < 0]

    def calc_stats(subdf, nombre):
        if subdf.empty:
            return None
        return {
            'nombre': nombre,
            'cantidad': len(subdf),
            'media_vol': subdf['volume'].mean(),
            'mediana_vol': subdf['volume'].median(),
            'std_vol': subdf['volume'].std(),
            'min_vol': subdf['volume'].min(),
            'max_vol': subdf['volume'].max(),
            'percentil_25': subdf['volume'].quantile(0.25),
            'percentil_50': subdf['volume'].quantile(0.50),
            'percentil_75': subdf['volume'].quantile(0.75),
            'percentil_90': subdf['volume'].quantile(0.90),
        }

    stats = {
        'umbral': umbral_mov,
        'global': calc_stats(velas_filtradas, 'GLOBAL'),
        'alcista': calc_stats(alcistas, 'ALCISTA'),
        'bajista': calc_stats(bajistas, 'BAJISTA'),
    }
    return stats


def generar_reporte_completo(df_velas, relacion_min, percentil_vol, umbrales):
    """
    Itera sobre los umbrales de movimiento, obtiene estadísticas y construye
    el mensaje de Telegram.
    """
    lineas = []
    lineas.append(f"📊 <b>Análisis de volumen para {SYMBOL} ({INTERVAL})</b>")
    lineas.append(f"Período: {df_velas['open_time'].min().strftime('%Y-%m-%d %H:%M')} a {df_velas['open_time'].max().strftime('%Y-%m-%d %H:%M')}")
    lineas.append(f"Velas totales analizadas: {len(df_velas)}")
    lineas.append(f"Relación cuerpo/rango mínima: {relacion_min}")
    lineas.append("")

    recomendaciones = []

    for umbral in umbrales:
        stats = analizar_velas_por_umbral(df_velas, umbral, relacion_min)
        if stats is None:
            lineas.append(f"❌ <b>Umbral {umbral}%</b>: No se encontraron velas que cumplan las condiciones.")
            lineas.append("")
            continue

        lineas.append(f"<b>📈 Umbral {umbral}%</b>")
        g = stats['global']
        lineas.append(f"   Global: {g['cantidad']} velas | Mediana: {g['mediana_vol']:.1f} BTC | P75: {g['percentil_75']:.1f} | P90: {g['percentil_90']:.1f}")

        a = stats['alcista']
        if a:
            lineas.append(f"   ▲ Alcista: {a['cantidad']} velas | Mediana: {a['mediana_vol']:.1f} | P75: {a['percentil_75']:.1f}")
        else:
            lineas.append("   ▲ Alcista: sin datos")

        b = stats['bajista']
        if b:
            lineas.append(f"   ▼ Bajista: {b['cantidad']} velas | Mediana: {b['mediana_vol']:.1f} | P75: {b['percentil_75']:.1f}")
        else:
            lineas.append("   ▼ Bajista: sin datos")
        lineas.append("")

        # Guardar para recomendación (usando el percentil elegido)
        if a and a['cantidad'] >= 3:  # mínimo 3 eventos para recomendación
            p_val = a.get(f'percentil_{percentil_vol}', a['percentil_75'])
            recomendaciones.append(f"▲ Alcista ({umbral}%): volumen > {p_val:.1f} BTC ({percentil_vol}º percentil, {a['cantidad']} eventos)")
        if b and b['cantidad'] >= 3:
            p_val = b.get(f'percentil_{percentil_vol}', b['percentil_75'])
            recomendaciones.append(f"▼ Bajista ({umbral}%): volumen > {p_val:.1f} BTC ({percentil_vol}º percentil, {b['cantidad']} eventos)")

    # Añadir recomendaciones finales
    lineas.append("<b>🔍 Recomendaciones de volumen óptimo</b> (basadas en percentil {})".format(percentil_vol))
    if recomendaciones:
        lineas.extend(recomendaciones)
    else:
        lineas.append("No hay suficientes datos para recomendar (mínimo 3 eventos por dirección y umbral).")

    return "\n".join(lineas)


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
    logger.info("=== INICIO ANÁLISIS DE VOLUMEN ===")
    logger.info(f"Buscando velas {SYMBOL} {INTERVAL} con umbrales {UMBRALES_MOVIMIENTO}% y cuerpo/rango >= {RELACION_CUERPO_MIN}")

    # Recolectar datos
    df_velas = recolectar_velas(SYMBOL, INTERVAL, TOTAL_VELAS, limit=1000)
    if df_velas is None or df_velas.empty:
        logger.error("No se obtuvieron datos. Abortando.")
        enviar_telegram("❌ Error: No se pudieron obtener datos de Binance.")
        return

    # Generar reporte
    mensaje = generar_reporte_completo(df_velas, RELACION_CUERPO_MIN, PERCENTIL_VOLUMEN, UMBRALES_MOVIMIENTO)

    # Enviar
    enviar_telegram(mensaje)
    logger.info("=== ANÁLISIS COMPLETADO ===")


if __name__ == "__main__":
    # Ejecutar una vez
    main()
