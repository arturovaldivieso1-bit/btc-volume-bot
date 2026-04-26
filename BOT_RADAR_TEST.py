# -*- coding: utf-8 -*-
import os

# ╔══════════════════════════════════════════════════════════════╗
# ║       CONFIGURACIÓN – MODIFICA SOLO ESTE BLOQUE            ║
# ╚══════════════════════════════════════════════════════════════╝
TOKEN       = os.getenv("TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
SYMBOL      = "BTCUSDT"

INTERVAL_1H = "1h"
INTERVAL_4H = "4h"
INTERVAL_5M = "5m"

# --- Niveles (soportes / resistencias) ---
LOOKBACK_PIVOTS       = 672      # velas 1h analizadas (4 semanas ≈ 672h)
FETCH_1H_LIMIT        = 720      # velas 1h que se piden a Binance (≥ LOOKBACK_PIVOTS + 50)
TOP_NIVELES           = 5        # cuántos máximos/mínimos se guardan como referencia
AGRUPACION_NIVELES    = 200      # redondeo para unificar niveles cercanos (200 = 200 USD)
PROXIMIDAD_NIVEL      = 0.001    # % que define "tocar" un nivel (0.001 = 0.1%)

# --- Alertas de precio ---
IMPULSO_5M_PCT        = 0.65     # % mínimo de mecha/cuerpo en vela 5m para avisar
MOVIMIENTO_BRUSCO_PCT = 1.0      # % mínimo de variación en 1h para avisar (antes 1.5)
DIAS_ESTRECHO_MIN     = 3        # días consecutivos de rango pequeño (solo informativo)

# --- Archivo de memoria ---
MEMORIA_NIVELES_FILE  = "memoria_niveles.json"
# ╔══════════════════════════════════════════════════════════════╗
# ║       FIN DE CONFIGURACIÓN – NO TOCAR MÁS ABAJO            ║
# ╚══════════════════════════════════════════════════════════════╝

import requests
import pandas as pd
import numpy as np
import time
import json
import threading
from datetime import datetime, timedelta, UTC
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import atexit

# =========================
# ESTADO GLOBAL
# =========================
ultima_deriva_time     = None
ultimo_precio_deriva   = None
ultimo_precio_resumen  = None      # ya casi no se usa, se mantiene por si algún día reactivas el resumen
last_heartbeat         = None
last_resumen           = None
memoria_niveles        = {}
ultimo_alerta_nivel    = {}
ultima_alerta_global   = None
ultima_ruptura_alerta  = {}
ultima_alerta_brusco   = {}
executor               = ThreadPoolExecutor(max_workers=5)

def _cerrar():
    executor.shutdown(wait=False)
atexit.register(_cerrar)

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
    if n is None: return "N/D"
    return f"{int(n):,}"

def obtener_candles(interval, limit=100):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": interval, "limit": limit}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "_","_","_","_","_","_"
        ])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        df["time_dt"] = pd.to_datetime(df["time"], unit='ms')
        return df
    except Exception as e:
        print(f"Error velas {interval}: {e}")
        return pd.DataFrame()

def precio_actual():
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}", timeout=10)
        return float(r.json()["price"])
    except:
        return None

def obtener_oi(limit=300):
    url = "https://fapi.binance.com/futures/data/openInterestHist"
    params = {"symbol": SYMBOL, "period": "5m", "limit": limit}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        df = pd.DataFrame(data)
        df["sumOpenInterestValue"] = pd.to_numeric(df["sumOpenInterestValue"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit='ms')
        return df
    except:
        return pd.DataFrame()

# =========================
# MEMORIA DE NIVELES
# =========================
def cargar_memoria():
    if os.path.exists(MEMORIA_NIVELES_FILE):
        try:
            with open(MEMORIA_NIVELES_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def guardar_memoria(mem):
    try:
        with open(MEMORIA_NIVELES_FILE, "w") as f:
            json.dump(mem, f)
    except Exception as e:
        print(f"Error guardando memoria: {e}")

def actualizar_memoria_niveles(niveles, precio, hora_utc_naive):
    nuevo = False
    for nivel, tipo in niveles:
        key = str(nivel)
        if abs(precio - nivel) / precio <= PROXIMIDAD_NIVEL:
            if key not in memoria_niveles:
                memoria_niveles[key] = {"tipo": tipo, "toques": []}
            toques = memoria_niveles[key]["toques"]
            if toques:
                ultimo = datetime.fromisoformat(toques[-1])
                if (hora_utc_naive - ultimo) < timedelta(hours=1):
                    continue
            memoria_niveles[key]["toques"].append(hora_utc_naive.isoformat())
            memoria_niveles[key]["toques"] = memoria_niveles[key]["toques"][-20:]
            nuevo = True
    return nuevo

def obtener_contexto_nivel(nivel, precio, df_1h=None):
    key = str(nivel)
    if key not in memoria_niveles:
        return "sin datos históricos"
    info = memoria_niveles[key]
    toques = info.get("toques", [])
    if not toques:
        return "sin toques registrados"

    ahora = datetime.now(UTC).replace(tzinfo=None)
    ultimo_ts = datetime.fromisoformat(toques[-1])
    dias = (ahora - ultimo_ts).days
    n = len(toques)

    if dias == 0:
        tiempo = f"tocado hoy ({ultimo_ts.strftime('%H:%M')} UTC)"
    elif dias == 1:
        tiempo = f"última visita hace 1 día ({ultimo_ts.strftime('%d %b')})"
    else:
        tiempo = f"última visita hace {dias} días ({ultimo_ts.strftime('%d %b')})"

    rebote = ""
    if df_1h is not None and n >= 2:
        rebotes = 0
        for ts_str in toques[:-1]:
            ts = datetime.fromisoformat(ts_str)
            post = df_1h[df_1h["time_dt"] > ts].head(4)
            if not post.empty:
                if info["tipo"] == "soporte":
                    if (post["high"].max() - nivel) / nivel > 0.005:
                        rebotes += 1
                else:
                    if (nivel - post["low"].min()) / nivel > 0.005:
                        rebotes += 1
        tasa = int(rebotes / (n - 1) * 100)
        rebote = f", rebote {tasa}%" if info["tipo"] == "soporte" else f", rechazo {tasa}%"

    return f"{tiempo} | {n} toque{'s' if n > 1 else ''}{rebote}"

# =========================
# NIVELES ESTRUCTURALES
# =========================
def niveles_principales(df_1h, lookback=LOOKBACK_PIVOTS, top=TOP_NIVELES, agrupacion=AGRUPACION_NIVELES):
    if df_1h.empty or len(df_1h) < lookback:
        return [], []
    highs = df_1h["high"].values[-lookback:]
    lows  = df_1h["low"].values[-lookback:]
    top_highs = sorted(highs)[-top:] if len(highs) >= top else sorted(highs)
    top_lows  = sorted(lows)[:top]   if len(lows) >= top   else sorted(lows)
    rh = sorted(set([round(h / agrupacion) * agrupacion for h in top_highs]))
    rl = sorted(set([round(l / agrupacion) * agrupacion for l in top_lows]))
    return rh, rl

def nivel_mas_cercano(precio, niveles, es_soporte=True):
    if es_soporte:
        debajo = [n for n in niveles if n < precio]
        return min(debajo, key=lambda x: precio - x) if debajo else None
    else:
        arriba = [n for n in niveles if n > precio]
        return min(arriba, key=lambda x: x - precio) if arriba else None

# =========================
# SESGO Y RANGO
# =========================
def calcular_sesgo(df_1h, precio):
    if df_1h.empty or len(df_1h) < 50:
        return "LATERAL"
    ema50  = df_1h["close"].ewm(span=50).mean().iloc[-1]
    ema200 = df_1h["close"].ewm(span=200).mean().iloc[-1]
    if precio > ema50 and ema50 > ema200:
        return "ALCISTA"
    elif precio < ema50 and ema50 < ema200:
        return "BAJISTA"
    return "LATERAL"

# =========================
# ALERTA DE IMPULSO (VELA CERRADA)
# =========================
def alerta_impulso_vela(df_5m, precio, pivotes_sop, pivotes_res, sesgo):
    if df_5m.empty or len(df_5m) < 2:
        return

    ahora = datetime.now(UTC).replace(tzinfo=None)
    ultima_vela = df_5m.iloc[-1]
    cierre_teorico = ultima_vela["time_dt"] + timedelta(minutes=5)

    # Si la última vela todavía se está formando, analizamos la anterior (ya cerrada)
    if ahora < cierre_teorico:
        if len(df_5m) < 3:
            return
        vela = df_5m.iloc[-2]
    else:
        vela = ultima_vela

    open_, high, low, close, vol = (
        vela["open"], vela["high"], vela["low"], vela["close"], vela["volume"]
    )
    high_change = (high - open_) / open_ * 100
    low_change  = (open_ - low)  / open_ * 100
    pct = max(high_change, low_change)
    if pct < IMPULSO_5M_PCT:
        return

    direccion = "ALCISTA" if high_change >= low_change else "BAJISTA"
    emoji = "🟢" if direccion == "ALCISTA" else "🔴"
    cierre_ok = (direccion == "ALCISTA" and close > open_) or (direccion == "BAJISTA" and close < open_)
    conf = "✅ cierre confirmado" if cierre_ok else "⚡ solo mecha"

    prox = nivel_mas_cercano(close, pivotes_res, es_soporte=False) if direccion == "ALCISTA" else nivel_mas_cercano(close, pivotes_sop, es_soporte=True)
    nivel_txt = ""
    if prox:
        dist = abs(prox - close) / close * 100
        tipo = "resistencia" if direccion == "ALCISTA" else "soporte"
        nivel_txt = f"\n→ Próximo {tipo}: {fmt(prox)} ({dist:.2f}%)"

    enviar(f"⚡ Movimiento {direccion} {pct:.2f}% {emoji}\n"
           f"Precio: {fmt(close)} | {datetime.now(UTC).strftime('%H:%M')} UTC\n"
           f"Máx: {fmt(high)} / Mín: {fmt(low)}\n"
           f"Volumen: {vol:.0f} BTC ({conf})\n"
           f"Sesgo: {sesgo}{nivel_txt}")

# =========================
# RESTO DE ALERTAS
# =========================
def alerta_nivel(precio, nivel, tipo_nivel, df_1h):
    global ultima_alerta_global
    ahora = datetime.now(UTC).replace(tzinfo=None)
    key = (nivel, tipo_nivel)
    if ultima_alerta_global and (ahora - ultima_alerta_global) < timedelta(hours=1):
        return
    if key in ultimo_alerta_nivel and (ahora - ultimo_alerta_nivel[key]) < timedelta(hours=4):
        return
    ultimo_alerta_nivel[key] = ahora
    ultima_alerta_global = ahora

    contexto = obtener_contexto_nivel(nivel, precio, df_1h)
    emoji = "🛡️" if tipo_nivel == "soporte" else "🚀"
    dist = abs(precio - nivel) / precio * 100
    enviar(f"{emoji} Precio en {tipo_nivel}: {fmt(nivel)}\n"
           f"Distancia: {dist:.2f}%\n"
           f"Historial: {contexto}\n"
           f"{datetime.now(UTC).strftime('%H:%M')} UTC")

def alerta_movimiento_brusco(precio, df_1h):
    global ultima_alerta_brusco
    if df_1h.empty or len(df_1h) < 2:
        return
    ahora = datetime.now(UTC).replace(tzinfo=None)
    precio_anterior = df_1h["close"].iloc[-2]
    var = abs(precio - precio_anterior) / precio_anterior * 100
    if var < MOVIMIENTO_BRUSCO_PCT:
        return
    direccion = "ALZA" if precio > precio_anterior else "BAJA"
    if direccion in ultima_alerta_brusco and (ahora - ultima_alerta_brusco[direccion]) < timedelta(minutes=30):
        return
    ultima_alerta_brusco[direccion] = ahora
    emoji = "🔥" if direccion == "ALZA" else "❄️"
    enviar(f"{emoji} Movimiento brusco: {direccion} {var:.2f}% en 1h\n"
           f"De {fmt(precio_anterior)} → {fmt(precio)}\n"
           f"{datetime.now(UTC).strftime('%H:%M')} UTC")

def alerta_ruptura_rango(df_1h, precio, pivotes_h, pivotes_l):
    if df_1h.empty or len(df_1h) < 168:
        return
    ahora = datetime.now(UTC).replace(tzinfo=None)
    min_7d = df_1h["low"].tail(168).min()
    max_7d = df_1h["high"].tail(168).max()
    key_up, key_down = "rup_up", "rup_down"
    cd = timedelta(hours=4)
    if precio > max_7d and (key_up not in ultima_ruptura_alerta or ahora - ultima_ruptura_alerta[key_up] > cd):
        ultima_ruptura_alerta[key_up] = ahora
        sig = nivel_mas_cercano(precio, pivotes_h, es_soporte=False)
        msg = (f"🚨 RUPTURA ALCISTA: supera máximo de 7 días ({fmt(max_7d)})\n"
               f"Precio: {fmt(precio)}\n"
               f"Rango roto: {fmt(min_7d)} – {fmt(max_7d)}")
        if sig: msg += f"\nPróxima resistencia: {fmt(sig)}"
        enviar(msg)
    elif precio < min_7d and (key_down not in ultima_ruptura_alerta or ahora - ultima_ruptura_alerta[key_down] > cd):
        ultima_ruptura_alerta[key_down] = ahora
        sig = nivel_mas_cercano(precio, pivotes_l, es_soporte=True)
        msg = (f"🚨 RUPTURA BAJISTA: pierde mínimo de 7 días ({fmt(min_7d)})\n"
               f"Precio: {fmt(precio)}\n"
               f"Rango roto: {fmt(min_7d)} – {fmt(max_7d)}")
        if sig: msg += f"\nPróximo soporte: {fmt(sig)}"
        enviar(msg)

def deriva_silenciosa(precio, ahora):
    global ultima_deriva_time, ultimo_precio_deriva
    ahora_naive = ahora.replace(tzinfo=None)
    if ultima_deriva_time is None:
        ultima_deriva_time = ahora_naive
        ultimo_precio_deriva = precio
        return
    if (ahora_naive - ultima_deriva_time) >= timedelta(hours=1):
        if ultimo_precio_deriva:
            var = abs(precio - ultimo_precio_deriva) / ultimo_precio_deriva * 100
            if var >= 0.65:
                dir_ = "ALZA" if precio > ultimo_precio_deriva else "BAJA"
                emoji = "🟢" if dir_ == "ALZA" else "🔴"
                enviar(f"🐢 Deriva silenciosa: {dir_} {var:.2f}% en 1h\n"
                       f"De {fmt(ultimo_precio_deriva)} → {fmt(precio)}\n"
                       f"{ahora.strftime('%H:%M')} UTC")
        ultima_deriva_time = ahora_naive
        ultimo_precio_deriva = precio

# =========================
# BUCLE PRINCIPAL
# =========================
def main():
    global last_resumen, last_heartbeat, memoria_niveles, ultimo_precio_resumen

    memoria_niveles = cargar_memoria()
    precio = precio_actual()
    enviar(f"🤖 BOT V14‑FINAL INICIADO\nPrecio: {fmt(precio)}")
    last_heartbeat = datetime.now(UTC).replace(tzinfo=None)

    # Precarga con FETCH_1H_LIMIT
    df_1h_init = obtener_candles(INTERVAL_1H, FETCH_1H_LIMIT)
    if not df_1h_init.empty:
        print(f"Precarga de {FETCH_1H_LIMIT} velas 1h OK")

    while True:
        try:
            ahora_utc = datetime.now(UTC)
            ahora_naive = ahora_utc.replace(tzinfo=None)

            df_1h = obtener_candles(INTERVAL_1H, FETCH_1H_LIMIT)
            df_4h = obtener_candles(INTERVAL_4H, 100)
            df_5m = obtener_candles(INTERVAL_5M, 100)
            df_oi = obtener_oi(300)
            precio = precio_actual()

            if precio is None or df_1h.empty:
                time.sleep(30)
                continue

            pivotes_h, pivotes_l = niveles_principales(df_1h)
            soporte     = nivel_mas_cercano(precio, pivotes_l, es_soporte=True)
            resistencia = nivel_mas_cercano(precio, pivotes_h, es_soporte=False)

            niveles_cercanos = []
            for n in pivotes_l:
                niveles_cercanos.append((n, "soporte"))
            for n in pivotes_h:
                niveles_cercanos.append((n, "resistencia"))
            nuevo_toque = actualizar_memoria_niveles(niveles_cercanos, precio, ahora_naive)
            if nuevo_toque:
                guardar_memoria(memoria_niveles)

            sesgo = calcular_sesgo(df_1h, precio)

            alerta_impulso_vela(df_5m, precio, pivotes_l, pivotes_h, sesgo)
            if soporte and abs(precio - soporte) / precio <= PROXIMIDAD_NIVEL:
                alerta_nivel(precio, soporte, "soporte", df_1h)
            if resistencia and abs(precio - resistencia) / precio <= PROXIMIDAD_NIVEL:
                alerta_nivel(precio, resistencia, "resistencia", df_1h)

            alerta_movimiento_brusco(precio, df_1h)
            alerta_ruptura_rango(df_1h, precio, pivotes_h, pivotes_l)
            deriva_silenciosa(precio, ahora_utc)

            # --- Heartbeat solo a las 9:00 y 16:00 Chile (UTC-4) ---
            hora_chile = (ahora_utc - timedelta(hours=4)).strftime('%H:%M')
            if hora_chile in ("09:00", "16:00") and (last_heartbeat is None or (ahora_naive - last_heartbeat) > timedelta(hours=1)):
                enviar(f"⏱️ Bot V14 activo — {fmt(precio)} USD — {hora_chile} Chile")
                last_heartbeat = ahora_naive

            time.sleep(45)

        except Exception as e:
            print(f"Error: {e}")
            enviar(f"⚠️ Error: {str(e)[:80]}")
            time.sleep(60)

if __name__ == "__main__":
    main()
