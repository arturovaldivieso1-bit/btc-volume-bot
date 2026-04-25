# -*- coding: utf-8 -*-
# BOT V14‑ESENCIA PLUS – Radar de contexto con memoria histórica
#   [FIX 1] Bug resumen_horario: msg se cortaba si oi_24h_var era None
#   [FIX 2] alerta_movimiento_brusco: índice correcto + cooldown 1h por dirección
#   [FIX 3] alerta_ruptura_rango: usa pivotes reales para siguiente nivel
#   [FIX 4] guardar_memoria: solo guarda cada 10 min o en nuevo toque
#   [FIX 5] dias_en_rango_actual: umbral dinámico según volatilidad reciente
#   [NUEVO] obtener_contexto_nivel: calcula tasa de rebote real con timestamps

import requests
import pandas as pd
import numpy as np
import time
import os
import json
import threading
from datetime import datetime, timedelta, UTC
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import atexit

# =========================
# CONFIGURACIÓN
# =========================
TOKEN       = os.getenv("TOKEN")
CHAT_ID     = os.getenv("CHAT_ID")
SYMBOL      = "BTCUSDT"
INTERVAL_1H = "1h"
INTERVAL_4H = "4h"
INTERVAL_5M = "5m"

LOOKBACK_PIVOTS       = 80      # velas 1h para buscar máximos/mínimos
PROXIMIDAD_NIVEL      = 0.001   # 0.1% para considerar "toque"
IMPULSO_5M_PCT        = 0.8     # % mínimo para alerta de vela 5m
MOVIMIENTO_BRUSCO_PCT = 1.5     # % mínimo en 1h
DIAS_ESTRECHO_MIN     = 3       # días de rango contenido para avisar

MEMORIA_NIVELES_FILE  = "memoria_niveles.json"

# Ciclo principal cada 45s → 1h ≈ 80 ciclos
CICLOS_POR_HORA = 80

# =========================
# ESTADO GLOBAL
# =========================
ultimos_precios        = deque(maxlen=CICLOS_POR_HORA * 24)  # 24h de precios
ultimos_movimientos_1h = deque(maxlen=168)
ultima_deriva_time     = None
ultimo_precio_deriva   = None
last_heartbeat         = None
last_resumen           = None
memoria_niveles        = {}
ultimo_alerta_nivel    = {}
ultima_ruptura_alerta  = {}
ultimo_guardado_mem    = None
ultima_alerta_brusco   = {}   # cooldown por dirección ("ALZA"/"BAJA")
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
    if n is None:
        return "N/D"
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
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}",
            timeout=10
        )
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

def guardar_memoria(mem, forzar=False):
    """[FIX 4] Solo guarda cada 10 min o si se fuerza (nuevo toque detectado)."""
    global ultimo_guardado_mem
    ahora = datetime.now(UTC)
    if not forzar and ultimo_guardado_mem and (ahora - ultimo_guardado_mem) < timedelta(minutes=10):
        return
    try:
        with open(MEMORIA_NIVELES_FILE, "w") as f:
            json.dump(mem, f)
        ultimo_guardado_mem = ahora
    except Exception as e:
        print(f"Error guardando memoria: {e}")

def actualizar_memoria_niveles(pivotes_altos, pivotes_bajos, precio, hora_actual):
    """Registra toques de precio en niveles estructurales. Retorna True si hubo nuevo toque."""
    nuevo_toque = False
    for tipo, lista in [("resistencia", pivotes_altos), ("soporte", pivotes_bajos)]:
        for nivel in lista:
            key = str(round(nivel))
            if abs(precio - nivel) / precio <= PROXIMIDAD_NIVEL:
                if key not in memoria_niveles:
                    memoria_niveles[key] = {"tipo": tipo, "toques": []}
                toques = memoria_niveles[key]["toques"]
                # Evitar registrar toques consecutivos dentro de la misma hora
                if toques:
                    ultimo_ts = datetime.fromisoformat(toques[-1])
                    if (hora_actual - ultimo_ts) < timedelta(hours=1):
                        continue
                memoria_niveles[key]["toques"].append(hora_actual.isoformat())
                memoria_niveles[key]["toques"] = memoria_niveles[key]["toques"][-20:]
                nuevo_toque = True
    return nuevo_toque

def obtener_contexto_nivel(nivel, precio, df_1h=None):
    """
    [NUEVO] Devuelve texto con:
    - última visita y cuántos días hace
    - número de toques
    - tasa de rebote estimada (si hay df_1h disponible)
    """
    key = str(round(nivel))
    if key not in memoria_niveles:
        return "sin datos históricos"
    info = memoria_niveles[key]
    toques = info.get("toques", [])
    if not toques:
        return "sin toques registrados"

    ahora = datetime.now(UTC)
    ultimo_ts = datetime.fromisoformat(toques[-1])
    dias = (ahora - ultimo_ts).days
    n_toques = len(toques)

    if dias == 0:
        tiempo_txt = f"tocado hoy ({ultimo_ts.strftime('%H:%M')} UTC)"
    elif dias == 1:
        tiempo_txt = f"última visita hace 1 día ({ultimo_ts.strftime('%d %b')})"
    else:
        tiempo_txt = f"última visita hace {dias} días ({ultimo_ts.strftime('%d %b')})"

    # Tasa de rebote estimada con df_1h
    rebote_txt = ""
    if df_1h is not None and n_toques >= 2 and info["tipo"] == "soporte":
        rebotes = 0
        for ts_str in toques[:-1]:  # excluye el más reciente
            ts = datetime.fromisoformat(ts_str)
            # Busca velas 1h posteriores al toque
            post = df_1h[df_1h["time_dt"] > ts].head(4)
            if not post.empty:
                max_post = post["high"].max()
                if (max_post - nivel) / nivel > 0.005:  # subió >0.5% desde el nivel
                    rebotes += 1
        tasa = int(rebotes / (n_toques - 1) * 100)
        rebote_txt = f", rebote {tasa}% de las veces"
    elif df_1h is not None and n_toques >= 2 and info["tipo"] == "resistencia":
        rebotes = 0
        for ts_str in toques[:-1]:
            ts = datetime.fromisoformat(ts_str)
            post = df_1h[df_1h["time_dt"] > ts].head(4)
            if not post.empty:
                min_post = post["low"].min()
                if (nivel - min_post) / nivel > 0.005:  # bajó >0.5% desde el nivel
                    rebotes += 1
        tasa = int(rebotes / (n_toques - 1) * 100)
        rebote_txt = f", rechazo {tasa}% de las veces"

    return f"{tiempo_txt} | {n_toques} toque{'s' if n_toques > 1 else ''}{rebote_txt}"

# =========================
# ANÁLISIS ESTRUCTURAL
# =========================
def pivotes_estructurales(df_1h, lookback=LOOKBACK_PIVOTS):
    if df_1h.empty or len(df_1h) < lookback:
        return [], []
    highs = df_1h["high"].values[-lookback:]
    lows  = df_1h["low"].values[-lookback:]
    idx_h, idx_l = [], []
    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            idx_h.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            idx_l.append(lows[i])
    return sorted(idx_h), sorted(idx_l)

def nivel_mas_cercano(precio, niveles, es_soporte=True):
    if es_soporte:
        debajo = [n for n in niveles if n < precio]
        return min(debajo, key=lambda x: precio - x) if debajo else None
    else:
        arriba = [n for n in niveles if n > precio]
        return min(arriba, key=lambda x: x - precio) if arriba else None

# =========================
# SESGO Y RANGOS
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

def dias_en_rango_actual(df_1h, precio):
    """
    [FIX 5] Calcula días consecutivos en rango estrecho.
    Umbral dinámico: rango diario < 50% de la volatilidad media de los últimos 10 días.
    """
    if df_1h.empty or len(df_1h) < 48:
        return 0, 0, 0

    # Volatilidad media de los últimos 10 días (en %)
    rangos_diarios = []
    for i in range(10):
        seg = df_1h.iloc[-(24*(i+1)):-(24*i) if i > 0 else len(df_1h)]
        if len(seg) >= 12:
            r = (seg["high"].max() - seg["low"].min()) / precio
            rangos_diarios.append(r)
    if not rangos_diarios:
        return 0, 0, 0
    umbral = np.mean(rangos_diarios) * 0.5  # estrecho = menos del 50% de la media

    dias = 0
    for i in range(1, min(15, len(df_1h) // 24) + 1):
        seg = df_1h.iloc[-24*i:-24*(i-1) if i > 1 else len(df_1h)]
        if len(seg) < 12:
            continue
        rango_dia = (seg["high"].max() - seg["low"].min()) / precio
        if rango_dia < umbral:
            dias += 1
        else:
            break

    min_rango = df_1h["low"].tail(24 * max(dias, 1)).min()
    max_rango = df_1h["high"].tail(24 * max(dias, 1)).max()
    return dias, min_rango, max_rango

# =========================
# ALERTAS INMEDIATAS
# =========================
def alerta_impulso_vela(df_5m, precio, pivotes_sop, pivotes_res, sesgo):
    if df_5m.empty or len(df_5m) < 2:
        return
    vela = df_5m.iloc[-1]
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
    cierre_confirmado = (
        (direccion == "ALCISTA" and close > open_) or
        (direccion == "BAJISTA" and close < open_)
    )
    conf = "✅ cierre confirmado" if cierre_confirmado else "⚡ solo mecha"

    if direccion == "ALCISTA":
        prox = nivel_mas_cercano(close, pivotes_res, es_soporte=False)
    else:
        prox = nivel_mas_cercano(close, pivotes_sop, es_soporte=True)

    nivel_txt = ""
    if prox:
        dist = abs(prox - close) / close * 100
        tipo_nivel = "resistencia" if direccion == "ALCISTA" else "soporte"
        nivel_txt = f"\n→ Próximo {tipo_nivel}: {fmt(prox)} ({dist:.2f}%)"

    msg = (f"⚡ Movimiento {direccion} {pct:.2f}% {emoji}\n"
           f"Precio: {fmt(close)} | {datetime.now(UTC).strftime('%H:%M')} UTC\n"
           f"Máx: {fmt(high)} / Mín: {fmt(low)}\n"
           f"Volumen: {vol:.0f} BTC ({conf})\n"
           f"Sesgo: {sesgo}{nivel_txt}")
    enviar(msg)

def alerta_nivel(precio, nivel, tipo_nivel, df_1h=None):
    ahora = datetime.now(UTC)
    key = (round(nivel), tipo_nivel)
    if key in ultimo_alerta_nivel and (ahora - ultimo_alerta_nivel[key]) < timedelta(hours=2):
        return
    ultimo_alerta_nivel[key] = ahora
    contexto = obtener_contexto_nivel(nivel, precio, df_1h)
    emoji = "🛡️" if tipo_nivel == "soporte" else "🚀"
    dist = abs(precio - nivel) / precio * 100
    msg = (f"{emoji} Precio en {tipo_nivel}: {fmt(nivel)}\n"
           f"Distancia: {dist:.2f}%\n"
           f"Historial: {contexto}\n"
           f"{ahora.strftime('%H:%M')} UTC")
    enviar(msg)

def alerta_movimiento_brusco(precio_actual_val):
    """[FIX 2] Usa índice correcto para precio hace ~1h. Cooldown de 1h por dirección."""
    global ultima_alerta_brusco
    ahora = datetime.now(UTC)
    if len(ultimos_precios) < CICLOS_POR_HORA:
        return
    precio_hace_1h = ultimos_precios[-CICLOS_POR_HORA]
    var = abs(precio_actual_val - precio_hace_1h) / precio_hace_1h * 100
    if var >= MOVIMIENTO_BRUSCO_PCT:
        direccion = "ALZA" if precio_actual_val > precio_hace_1h else "BAJA"

        # Cooldown de 1h por dirección
        if direccion in ultima_alerta_brusco:
            if (ahora - ultima_alerta_brusco[direccion]) < timedelta(hours=1):
                return
        ultima_alerta_brusco[direccion] = ahora

        emoji = "🔥" if direccion == "ALZA" else "❄️"

        # Contexto: ¿es el mayor movimiento reciente?
        movs = [abs(v[0]) for v in ultimos_movimientos_1h if v[1] == direccion]
        contexto = ""
        if movs:
            dias_sin_igual = sum(1 for m in movs if m < var)
            if dias_sin_igual >= len(movs) * 0.8:
                contexto = f" — mayor movimiento en ~{len(movs)} horas"

        msg = (f"{emoji} Movimiento brusco: {direccion} {var:.2f}% en ~1h{contexto}\n"
               f"De {fmt(precio_hace_1h)} → {fmt(precio_actual_val)}\n"
               f"{ahora.strftime('%H:%M')} UTC")
        enviar(msg)

def alerta_ruptura_rango(df_1h, precio, pivotes_h, pivotes_l):
    """[FIX 3] Usa pivotes reales para el siguiente nivel, no el nivel roto."""
    if df_1h.empty or len(df_1h) < 168:
        return
    ahora = datetime.now(UTC)
    min_7d = df_1h["low"].tail(168).min()
    max_7d = df_1h["high"].tail(168).max()

    key_up   = "ruptura_up"
    key_down = "ruptura_down"
    cooldown = timedelta(hours=4)

    if precio > max_7d:
        if key_up in ultima_ruptura_alerta and (ahora - ultima_ruptura_alerta[key_up]) < cooldown:
            return
        ultima_ruptura_alerta[key_up] = ahora
        dias_rango, _, _ = dias_en_rango_actual(df_1h, precio)
        sig_res = nivel_mas_cercano(precio, pivotes_h, es_soporte=False)
        sig_txt = f"\nPróxima resistencia: {fmt(sig_res)}" if sig_res else ""
        msg = (f"🚨 RUPTURA ALCISTA: supera máximo de 7 días ({fmt(max_7d)})\n"
               f"Precio actual: {fmt(precio)}\n"
               f"Rango roto: {fmt(min_7d)} – {fmt(max_7d)}"
               f"{sig_txt}\n"
               f"{ahora.strftime('%H:%M')} UTC")
        enviar(msg)

    elif precio < min_7d:
        if key_down in ultima_ruptura_alerta and (ahora - ultima_ruptura_alerta[key_down]) < cooldown:
            return
        ultima_ruptura_alerta[key_down] = ahora
        dias_rango, _, _ = dias_en_rango_actual(df_1h, precio)
        sig_sop = nivel_mas_cercano(precio, pivotes_l, es_soporte=True)
        sig_txt = f"\nPróximo soporte: {fmt(sig_sop)}" if sig_sop else ""
        msg = (f"🚨 RUPTURA BAJISTA: pierde mínimo de 7 días ({fmt(min_7d)})\n"
               f"Precio actual: {fmt(precio)}\n"
               f"Rango roto: {fmt(min_7d)} – {fmt(max_7d)}"
               f"{sig_txt}\n"
               f"{ahora.strftime('%H:%M')} UTC")
        enviar(msg)

def deriva_silenciosa(precio, ahora):
    global ultima_deriva_time, ultimo_precio_deriva
    if ultima_deriva_time is None:
        ultima_deriva_time   = ahora
        ultimo_precio_deriva = precio
        return
    if (ahora - ultima_deriva_time) >= timedelta(hours=1):
        if ultimo_precio_deriva:
            var = abs(precio - ultimo_precio_deriva) / ultimo_precio_deriva * 100
            if var >= 0.65:
                dir_ = "ALZA" if precio > ultimo_precio_deriva else "BAJA"
                emoji = "🟢" if dir_ == "ALZA" else "🔴"
                msg = (f"🐢 Deriva silenciosa: {dir_} {var:.2f}% en 1h\n"
                       f"De {fmt(ultimo_precio_deriva)} → {fmt(precio)}\n"
                       f"{ahora.strftime('%H:%M')} UTC")
                enviar(msg)
        ultima_deriva_time   = ahora
        ultimo_precio_deriva = precio

# =========================
# RESUMEN HORARIO ENRIQUECIDO
# =========================
def resumen_horario(precio, soporte, resistencia, df_1h, df_oi, pivotes_h, pivotes_l):
    """[FIX 1] El mensaje se construye completamente antes de enviarse."""
    ahora = datetime.now(UTC)

    var1h  = ((precio - df_1h["close"].iloc[-2])   / df_1h["close"].iloc[-2]   * 100) if len(df_1h) >= 2   else 0
    var24h = ((precio - df_1h["close"].iloc[-25])   / df_1h["close"].iloc[-25]  * 100) if len(df_1h) >= 25  else 0
    var7d  = ((precio - df_1h["close"].iloc[-169])  / df_1h["close"].iloc[-169] * 100) if len(df_1h) >= 169 else 0

    rango_24h_min = df_1h["low"].tail(24).min()
    rango_24h_max = df_1h["high"].tail(24).max()
    rango_24h     = rango_24h_max - rango_24h_min

    sesgo = calcular_sesgo(df_1h, precio)

    soporte_txt = (
        f"{fmt(soporte)} ({abs(soporte-precio)/precio*100:.2f}%) — "
        f"{obtener_contexto_nivel(soporte, precio, df_1h)}"
        if soporte else "sin soporte claro"
    )
    resistencia_txt = (
        f"{fmt(resistencia)} ({abs(resistencia-precio)/precio*100:.2f}%) — "
        f"{obtener_contexto_nivel(resistencia, precio, df_1h)}"
        if resistencia else "sin resistencia clara"
    )

    dias_rango, min_rango, max_rango = dias_en_rango_actual(df_1h, precio)

    # Construir mensaje base
    msg = (f"📍 CONTEXTO ACTUAL — {ahora.strftime('%H:%M')} UTC\n"
           f"Precio: {fmt(precio)}\n"
           f"\n"
           f"📊 Variación: {var1h:+.2f}% (1h) | {var24h:+.2f}% (24h) | {var7d:+.2f}% (7d)\n"
           f"🛡️ Soporte: {soporte_txt}\n"
           f"🚀 Resistencia: {resistencia_txt}\n"
           f"📐 Rango 24h: {fmt(rango_24h_min)} – {fmt(rango_24h_max)} ({rango_24h:.0f} USD)\n"
           f"📈 Sesgo: {sesgo}")

    # [FIX 1] OI se agrega solo si existe, sin afectar el resto del mensaje
    if not df_oi.empty and len(df_oi) >= 288:
        oi_24h_var = (
            (df_oi["sumOpenInterestValue"].iloc[-1] - df_oi["sumOpenInterestValue"].iloc[-288])
            / df_oi["sumOpenInterestValue"].iloc[-288] * 100
        )
        oi_dir = "📈" if oi_24h_var > 0 else "📉"
        msg += f"\n{oi_dir} OI Futuros (24h): {oi_24h_var:+.2f}%"

    if dias_rango >= DIAS_ESTRECHO_MIN:
        msg += (f"\n⏳ Rango estrecho: {dias_rango} días"
                f" ({fmt(min_rango)} – {fmt(max_rango)})"
                f" — posible explosión próxima")

    enviar(msg)

# =========================
# BUCLE PRINCIPAL
# =========================
def main():
    global ultima_deriva_time, ultimo_precio_deriva
    global last_heartbeat, last_resumen, memoria_niveles

    memoria_niveles = cargar_memoria()

    precio = precio_actual()
    enviar(f"🤖 BOT V14‑ESENCIA PLUS INICIADO\nPrecio: {fmt(precio)}")
    last_heartbeat = datetime.now(UTC)

    # Precarga de precios e historial de movimientos
    df_1h_init = obtener_candles(INTERVAL_1H, 200)
    if not df_1h_init.empty:
        for c in df_1h_init["close"]:
            ultimos_precios.append(c)
        for i in range(1, len(df_1h_init)):
            var = (
                (df_1h_init["close"].iloc[i] - df_1h_init["close"].iloc[i-1])
                / df_1h_init["close"].iloc[i-1] * 100
            )
            dir_ = "ALCISTA" if var > 0 else "BAJISTA"
            ultimos_movimientos_1h.append((var, dir_, df_1h_init["time_dt"].iloc[i]))

    threading.Thread(target=lambda: None, daemon=True).start()

    while True:
        try:
            ahora  = datetime.now(UTC)
            df_1h  = obtener_candles(INTERVAL_1H, 200)
            df_4h  = obtener_candles(INTERVAL_4H, 100)
            df_5m  = obtener_candles(INTERVAL_5M, 100)
            df_oi  = obtener_oi(300)
            precio = precio_actual()

            if precio is None or df_1h.empty:
                time.sleep(30)
                continue

            # 1. Niveles estructurales
            pivotes_h, pivotes_l = pivotes_estructurales(df_1h)
            soporte     = nivel_mas_cercano(precio, pivotes_l, es_soporte=True)
            resistencia = nivel_mas_cercano(precio, pivotes_h, es_soporte=False)

            # 2. Memoria de niveles — guardar solo si hubo nuevo toque
            nuevo_toque = actualizar_memoria_niveles(pivotes_h, pivotes_l, precio, ahora)
            guardar_memoria(memoria_niveles, forzar=nuevo_toque)

            # 3. Registrar precio y movimiento
            if ultimos_precios:
                var1m = (precio - ultimos_precios[-1]) / ultimos_precios[-1] * 100
                dir_  = "ALCISTA" if var1m > 0 else "BAJISTA"
                ultimos_movimientos_1h.append((var1m, dir_, ahora))
            ultimos_precios.append(precio)

            # 4. Calcular sesgo una vez
            sesgo = calcular_sesgo(df_1h, precio)

            # 5. Alertas inmediatas
            alerta_impulso_vela(df_5m, precio, pivotes_l, pivotes_h, sesgo)

            if soporte and abs(precio - soporte) / precio <= PROXIMIDAD_NIVEL:
                alerta_nivel(precio, soporte, "soporte", df_1h)
            if resistencia and abs(precio - resistencia) / precio <= PROXIMIDAD_NIVEL:
                alerta_nivel(precio, resistencia, "resistencia", df_1h)

            alerta_movimiento_brusco(precio)
            alerta_ruptura_rango(df_1h, precio, pivotes_h, pivotes_l)
            deriva_silenciosa(precio, ahora)

            # 6. Resumen horario
            if last_resumen is None or (ahora - last_resumen) > timedelta(hours=1):
                resumen_horario(precio, soporte, resistencia, df_1h, df_oi, pivotes_h, pivotes_l)
                last_resumen = ahora

            # 7. Heartbeat cada 4h
            if last_heartbeat and (ahora - last_heartbeat) > timedelta(hours=4):
                enviar(f"⏱️ Bot V14 Plus activo — {fmt(precio)} USD — {ahora.strftime('%H:%M')} UTC")
                last_heartbeat = ahora

            time.sleep(45)

        except Exception as e:
            print(f"Error en bucle: {e}")
            enviar(f"⚠️ Error bot: {str(e)[:120]}")
            time.sleep(60)

if __name__ == "__main__":
    main()
