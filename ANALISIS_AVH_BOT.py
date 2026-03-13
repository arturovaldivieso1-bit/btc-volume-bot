import os
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import time
import logging

# ================= CONFIGURACIÓN =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not TOKEN or not CHAT_ID:
    raise ValueError("Faltan TOKEN o CHAT_ID en variables de entorno")

# Parámetros del análisis
SYMBOL = "BTCUSDT"
INTERVAL = "5m"
TOTAL_VELAS = 10000          # Unos 35 días de datos (puedes subir a 20000)
MOVIMIENTO_MIN = 0.7          # % mínimo de movimiento neto
RELACION_CUERPO_MIN = 0.7     # Relación mínima cuerpo/rango

# ================= FUNCIONES =================

def obtener_velas(symbol, interval, limit=1000, end_time=None):
    """Obtiene un lote de velas de Binance"""
    base_url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    if end_time:
        params["endTime"] = end_time

    try:
        response = requests.get(base_url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not data:
            return None

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

    except Exception as e:
        logger.error(f"Error obteniendo velas: {e}")
        return None


def recolectar_velas(symbol, interval, total_velas, limit=1000):
    """Recolecta exactamente total_velas velas hacia atrás"""
    inicio = time.time()
    lotes = []
    end_time = None
    acumulado = 0
    intentos_fallidos = 0
    max_intentos = 3

    logger.info(f"Recolectando {total_velas} velas {interval} de {symbol}")

    while acumulado < total_velas and intentos_fallidos < max_intentos:
        logger.info(f"Solicitando lote con end_time={end_time}")
        df = obtener_velas(symbol, interval, limit, end_time)

        if df is None or df.empty:
            intentos_fallidos += 1
            logger.warning(f"Intento fallido {intentos_fallidos}/{max_intentos}")
            time.sleep(1)
            continue

        lotes.append(df)
        acumulado += len(df)
        logger.info(f"Lote: {len(df)} velas. Total: {acumulado}/{total_velas}")

        primer_open = df.iloc[0]['open_time']
        end_time = int(primer_open.timestamp() * 1000) - 1
        intentos_fallidos = 0

        if len(df) < limit:
            logger.info("Fin de datos históricos")
            break

        time.sleep(0.1)

    if not lotes:
        return None

    df_total = pd.concat(lotes, ignore_index=True)
    df_total.drop_duplicates(subset=['open_time'], keep='first', inplace=True)
    df_total.sort_values('open_time', ascending=True, inplace=True)

    if len(df_total) > total_velas:
        df_total = df_total.tail(total_velas)

    logger.info(f"Recolección completada en {time.time()-inicio:.2f}s. Velas: {len(df_total)}")
    return df_total


def calcular_stats(df_filtrado, nombre_grupo="General"):
    """Calcula estadísticas de volumen para un DataFrame filtrado"""
    if df_filtrado is None or df_filtrado.empty:
        logger.warning(f"No hay datos para {nombre_grupo}")
        return None

    return {
        'grupo': nombre_grupo,
        'conteo': len(df_filtrado),
        'media': df_filtrado['volume'].mean(),
        'mediana': df_filtrado['volume'].median(),
        'min': df_filtrado['volume'].min(),
        'max': df_filtrado['volume'].max(),
        'p25': df_filtrado['volume'].quantile(0.25),
        'p50': df_filtrado['volume'].quantile(0.50),
        'p70': df_filtrado['volume'].quantile(0.70),
        'p75': df_filtrado['volume'].quantile(0.75),
        'p80': df_filtrado['volume'].quantile(0.80),
        'p85': df_filtrado['volume'].quantile(0.85),
        'p90': df_filtrado['volume'].quantile(0.90),
    }


def analizar_velas(df):
    """Filtra velas y calcula stats por dirección"""
    if df is None or df.empty:
        return None, None, None, None

    df = df[df['open'] != 0].copy()

    # Métricas
    df['mov_pct'] = (df['close'] - df['open']) / df['open'] * 100
    df['rango_pct'] = (df['high'] - df['low']) / df['open'] * 100
    df['rel_cuerpo'] = abs(df['close'] - df['open']) / (df['high'] - df['low'])
    df['rel_cuerpo'].replace([np.inf, -np.inf], 0, inplace=True)
    df['rel_cuerpo'].fillna(0, inplace=True)

    # Filtro principal
    condicion = (abs(df['mov_pct']) >= MOVIMIENTO_MIN) & (df['rel_cuerpo'] >= RELACION_CUERPO_MIN)
    velas_filtradas = df[condicion].copy()

    if velas_filtradas.empty:
        logger.warning("No hay velas que cumplan condiciones")
        return None, None, None, None

    # Separar por dirección
    alcistas = velas_filtradas[velas_filtradas['mov_pct'] > 0].copy()
    bajistas = velas_filtradas[velas_filtradas['mov_pct'] < 0].copy()

    # Calcular stats
    stats_gral = calcular_stats(velas_filtradas, "Totales")
    stats_alc = calcular_stats(alcistas, "Alcistas") if not alcistas.empty else None
    stats_baj = calcular_stats(bajistas, "Bajistas") if not bajistas.empty else None

    return stats_gral, stats_alc, stats_baj, velas_filtradas


def formato_stats(stats):
    """Convierte stats a string legible"""
    if stats is None:
        return "   (sin datos)\n"
    return (f"   • Conteo: {stats['conteo']}\n"
            f"   • Mediana: {stats['mediana']:.2f} BTC\n"
            f"   • Media: {stats['media']:.2f} BTC\n"
            f"   • Min: {stats['min']:.2f} | Max: {stats['max']:.2f}\n"
            f"   • P25: {stats['p25']:.2f} | P75: {stats['p75']:.2f}\n"
            f"   • P70: {stats['p70']:.2f} | P80: {stats['p80']:.2f} | P90: {stats['p90']:.2f}\n")


def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={'chat_id': CHAT_ID, 'text': mensaje, 'parse_mode': 'HTML'}, timeout=10)
        logger.info("Mensaje enviado")
    except Exception as e:
        logger.error(f"Error al enviar: {e}")


def main():
    logger.info("=== INICIO ANÁLISIS DE VOLUMEN ===")
    logger.info(f"Buscando velas {SYMBOL} {INTERVAL} con mov >= {MOVIMIENTO_MIN}% y cuerpo/rango >= {RELACION_CUERPO_MIN}")

    # Recolectar datos
    df_velas = recolectar_velas(SYMBOL, INTERVAL, TOTAL_VELAS, limit=1000)
    if df_velas is None:
        enviar_telegram("❌ No se pudieron obtener datos de Binance.")
        return

    # Analizar
    stats_gral, stats_alc, stats_baj, velas_filtradas = analizar_velas(df_velas)

    # Construir mensaje
    fecha_ini = df_velas['open_time'].min().strftime('%Y-%m-%d %H:%M')
    fecha_fin = df_velas['open_time'].max().strftime('%Y-%m-%d %H:%M')

    msg = f"📊 <b>Análisis de volumen para {SYMBOL} ({INTERVAL})</b>\n"
    msg += f"Período: {fecha_ini} a {fecha_fin}\n"
    msg += f"Velas totales analizadas: {len(df_velas)}\n\n"

    if stats_gral is None:
        msg += "❌ No se encontraron velas que cumplan las condiciones."
        enviar_telegram(msg)
        return

    msg += f"✅ Velas que cumplen condición: {stats_gral['conteo']}\n\n"

    # Totales
    msg += "<b>📈 Estadísticas generales (ambas direcciones):</b>\n"
    msg += formato_stats(stats_gral)

    # Alcistas
    msg += "\n<b>🟢 Alcistas (mov > 0):</b>\n"
    msg += formato_stats(stats_alc) if stats_alc else "   (no hay suficientes datos)\n"

    # Bajistas
    msg += "\n<b>🔴 Bajistas (mov < 0):</b>\n"
    msg += formato_stats(stats_baj) if stats_baj else "   (no hay suficientes datos)\n"

    # Interpretación y recomendación sobre 400 BTC
    msg += "\n🔍 <b>Análisis de tu hipótesis (400 BTC):</b>\n"

    # Usamos el percentil 75 general como referencia
    p75 = stats_gral['p75']
    if p75 >= 400:
        msg += f"• El percentil 75 es {p75:.2f} BTC (≥400). Esto indica que el 75% de los movimientos ocurren con volumen ≤ {p75:.2f}.\n"
        msg += f"• Tu umbral de 400 BTC está por DEBAJO del P75, por lo que capturarías aproximadamente el {stats_gral['p70']:.1f}% de los eventos (usando P70 como referencia).\n"
    else:
        msg += f"• El percentil 75 es {p75:.2f} BTC (<400). Esto significa que más del 75% de los movimientos requieren MENOS de 400 BTC.\n"
        msg += f"• Con 400 BTC solo capturarías los eventos más voluminosos (por encima del P{((stats_gral['p90']<400)*90 + (stats_gral['p80']<400)*80 + ... )} ).\n"

    msg += "\n📌 <b>Recomendación de umbrales según tu objetivo:</b>\n"
    msg += f"• Para alta probabilidad (P75): {stats_gral['p75']:.2f} BTC\n"
    msg += f"• Para equilibrio (P50/mediana): {stats_gral['p50']:.2f} BTC\n"
    msg += f"• Para máxima frecuencia (P25): {stats_gral['p25']:.2f} BTC\n"

    if stats_alc and stats_baj:
        msg += "\n📊 <b>Asimetría alcista/bajista:</b>\n"
        dif = abs(stats_alc['p75'] - stats_baj['p75']) / max(stats_alc['p75'], stats_baj['p75']) * 100
        if dif > 20:
            msg += f"• Hay diferencia significativa: Alcistas P75={stats_alc['p75']:.2f}, Bajistas P75={stats_baj['p75']:.2f}. Considera umbrales separados.\n"
        else:
            msg += f"• Alcistas y bajistas tienen umbrales similares (diferencia <20%).\n"

    enviar_telegram(msg)
    logger.info("=== ANÁLISIS COMPLETADO ===")


if __name__ == "__main__":
    # Ejecutar una vez. Para ejecución periódica, descomentar el bucle.
    # while True:
    main()
    # time.sleep(86400)  # 24 horas
