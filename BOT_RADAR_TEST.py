# -*- coding: utf-8 -*-
# BOT V14‑FINAL (agrupación 200 USD)
#   - Movimiento brusco: referencia estable (cierre vela 1h anterior) + cooldown 4h
#   - Niveles: solo 5 máximos y 5 mínimos, agrupados en bloques de 200 USD
#   - Alertas de nivel con cooldown por nivel (4h) y global (1h)
#   - Datetime homogéneo: todo naive UTC (sin zona horaria)
#   - Sin spam, solo contexto útil

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
TOP_NIVELES           = 5       # cuántos máximos/mínimos guardamos
AGRUPACION_NIVELES    = 200     # redondeo a 200 USD para agrupar niveles
PROXIMIDAD_NIVEL      = 0.001   # 0.1% para considerar "toque"

IMPULSO_5M_PCT        = 0.8     # % mínimo para alerta de vela 5m
MOVIMIENTO_BRUSCO_PCT = 1.5     # % mínimo en 1h
DIAS_ESTRECHO_MIN     = 3       # días de rango contenido para avisar

MEMORIA_NIVELES_FILE  = "memoria_niveles.json"

# =========================
# ESTADO GLOBAL
# =========================
ultima_deriva_time     = None
ultimo_precio_deriva   = None
last_heartbeat         = None
last_resumen           = None
memoria_niveles        = {}
ultimo_alerta_nivel    = {}        # cooldown por (nivel, tipo)
ultima_alerta_global   = None     # cooldown global para cualquier nivel
ultima_ruptura_alerta  = {}
ultima_alerta_brusco   = {}        # cooldown por dirección ("ALZA"/"BAJA")
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
        # Fecha naive en UTC (sin zona horaria)
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
# MEMORIA DE NIVELES (con agrupación)
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
    """Registra toques en los niveles agrupados. Retorna True si hubo nuevo toque."""
    nuevo = False
    for nivel, tipo in niveles:
        key = str(nivel)  # ya viene redondeado a AGRUPACION_NIVELES
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
    """Devuelve texto con última visita, número de toques y tasa de rebote."""
    key = str(nivel)
    if key not in memoria_niveles:
        return "sin datos históricos"
    info = memoria_niveles[key]
    toques = info.get("toques", [])
    if not toques:
        return "sin toques registrados"

    ahora = datetime.now(UTC).replace(tzinfo=None)  # naive UTC
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
        for ts_str in toques[:-1]:  # excluye el último
            ts = datetime.fromisoformat(ts_str)
            post = df_1h[df_1h["time_dt"] > ts].head(4)
            if not post.empty:
                if info["tipo"] == "soporte":
                    if (post["high"].max() - nivel) / nivel > 0.005:
                        rebotes += 1
                else:  # resistencia
                    if (nivel - post["low"].min()) / nivel > 0.005:
                        rebotes += 1
        tasa = int(rebotes / (n - 1) * 100)
        rebote = f", rebote {tasa}%" if info["tipo"] == "soporte" else f", rechazo {tasa}%"

    return f"{tiempo} | {n} toque{'s' if n > 1 else ''}{rebote}"

# =========================
# NIVELES ESTRUCTURALES
# =========================
def niveles_principales(df_1h, lookback=LOOKBACK_PIVOTS, top=TOP_NIVELES, agrupacion=AGRUPACION_NIVELES):
    """Devuelve los top máximos y mínimos redondeados a la agrupación."""
    if df_1h.empty or len(df_1h) < lookback:
        return [], []
    highs = df_1h["high"].values[-lookback:]
    lows  = df_1h["low"].values[-lookback:]
    # Tomamos los 5 más altos y 5 más bajos
    top_highs = sorted(highs)[-top:] if len(highs) >= top else sorted(highs)
    top_lows  = sorted(lows)[:top]   if len(lows) >= top   else sorted(lows)
    # Redondear a múltiplos de agrupacion
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

def dias_en_rango_actual(df_1h, precio):
    if df_1h.empty or len(df_1h) < 48:
        return 0, 0, 0
    rangos_diarios = []
    for i in range(10):
        seg = df_1h.iloc[-(24*(i+1)):-(24*i) if i > 0 else len(df_1h)]
        if len(seg) >= 12:
            r = (seg["high"].max() - seg["low"].min()) / precio
            rangos_diarios.append(r)
    if not rangos_diarios:
        return 0, 0, 0
    umbral = np.mean(rangos_diarios) * 0.5

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
# ALERTAS
# =========================
def alerta_impulso_vela(df_5m, precio, pivotes_sop, pivotes_res, sesgo):
    if df_5m.empty or len(df_5m) < 2:
        return
    vela = df_5m.iloc[-1]
    open_, high, low, close, vol = vela["open"], vela["high"], vela["low"], vela["close"], vela["volume"]
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

def alerta_nivel(precio, nivel, tipo_nivel, df_1h):
    global ultima_alerta_global
    ahora = datetime.now(UTC).replace(tzinfo=None)
    key = (nivel, tipo_nivel)

    # Cooldown global (1h)
    if ultima_alerta_global and (ahora - ultima_alerta_global) < timedelta(hours=1):
        return
    # Cooldown por nivel (4h)
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
    if direccion in ultima_alerta_brusco and (ahora - ultima_alerta_brusco[direccion]) < timedelta(hours=4):
        return
    ultima_alerta_brusco[direccion] = ahora

    emoji = "🔥" if direccion == "ALZA" else "❄️"
    # Mayor movimiento reciente (contexto)
    variaciones = []
    for i in range(2, min(25, len(df_1h))):
        var_i = (df_1h["close"].iloc[-i] - df_1h["close"].iloc[-i-1]) / df_1h["close"].iloc[-i-1] * 100
        if (var_i > 0 and direccion == "ALZA") or (var_i < 0 and direccion == "BAJA"):
            variaciones.append(abs(var_i))
    extra = ""
    if variaciones and var > max(variaciones):
        extra = f" — mayor movimiento en {len(variaciones)} horas"
    enviar(f"{emoji} Movimiento brusco: {direccion} {var:.2f}% en 1h{extra}\n"
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
        if sig:
            msg += f"\nPróxima resistencia: {fmt(sig)}"
        enviar(msg)

    elif precio < min_7d and (key_down not in ultima_ruptura_alerta or ahora - ultima_ruptura_alerta[key_down] > cd):
        ultima_ruptura_alerta[key_down] = ahora
        sig = nivel_mas_cercano(precio, pivotes_l, es_soporte=True)
        msg = (f"🚨 RUPTURA BAJISTA: pierde mínimo de 7 días ({fmt(min_7d)})\n"
               f"Precio: {fmt(precio)}\n"
               f"Rango roto: {fmt(min_7d)} – {fmt(max_7d)}")
        if sig:
            msg += f"\nPróximo soporte: {fmt(sig)}"
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
# RESUMEN HORARIO
# =========================
def resumen_horario(precio, soporte, resistencia, df_1h, df_oi, pivotes_h, pivotes_l):
    ahora = datetime.now(UTC).replace(tzinfo=None)
    var1h = (precio - df_1h["close"].iloc[-2]) / df_1h["close"].iloc[-2] * 100 if len(df_1h) >= 2 else 0
    var24h = (precio - df_1h["close"].iloc[-25]) / df_1h["close"].iloc[-25] * 100 if len(df_1h) >= 25 else 0
    var7d = (precio - df_1h["close"].iloc[-169]) / df_1h["close"].iloc[-169] * 100 if len(df_1h) >= 169 else 0

    rango_min = df_1h["low"].tail(24).min()
    rango_max = df_1h["high"].tail(24).max()
    rango = rango_max - rango_min

    sesgo = calcular_sesgo(df_1h, precio)

    soporte_txt = f"{fmt(soporte)} ({abs(soporte-precio)/precio*100:.2f}%) — {obtener_contexto_nivel(soporte, precio, df_1h)}" if soporte else "sin soporte claro"
    resistencia_txt = f"{fmt(resistencia)} ({abs(resistencia-precio)/precio*100:.2f}%) — {obtener_contexto_nivel(resistencia, precio, df_1h)}" if resistencia else "sin resistencia clara"

    dias_rango, min_r, max_r = dias_en_rango_actual(df_1h, precio)

    msg = (f"📍 CONTEXTO ACTUAL — {datetime.now(UTC).strftime('%H:%M')} UTC\n"
           f"Precio: {fmt(precio)}\n"
           f"📊 Variación: {var1h:+.2f}% (1h) | {var24h:+.2f}% (24h) | {var7d:+.2f}% (7d)\n"
           f"🛡️ Soporte: {soporte_txt}\n"
           f"🚀 Resistencia: {resistencia_txt}\n"
           f"📐 Rango 24h: {fmt(rango_min)} – {fmt(rango_max)} ({rango:.0f} USD)\n"
           f"📈 Sesgo: {sesgo}")

    if not df_oi.empty and len(df_oi) >= 288:
        oi_var = (df_oi["sumOpenInterestValue"].iloc[-1] - df_oi["sumOpenInterestValue"].iloc[-288]) / df_oi["sumOpenInterestValue"].iloc[-288] * 100
        msg += f"\n{'📈' if oi_var > 0 else '📉'} OI Futuros (24h): {oi_var:+.2f}%"

    if dias_rango >= DIAS_ESTRECHO_MIN:
        msg += f"\n⏳ Rango estrecho: {dias_rango} días ({fmt(min_r)} – {fmt(max_r)}) — posible explosión próxima"

    enviar(msg)

# =========================
# BUCLE PRINCIPAL
# =========================
def main():
    global last_resumen, last_heartbeat, memoria_niveles

    memoria_niveles = cargar_memoria()
    precio = precio_actual()
    enviar(f"🤖 BOT V14‑FINAL INICIADO\nPrecio: {fmt(precio)}")
    last_heartbeat = datetime.now(UTC).replace(tzinfo=None)

    while True:
        try:
            ahora_utc = datetime.now(UTC)
            ahora_naive = ahora_utc.replace(tzinfo=None)

            df_1h = obtener_candles(INTERVAL_1H, 200)
            df_4h = obtener_candles(INTERVAL_4H, 100)
            df_5m = obtener_candles(INTERVAL_5M, 100)
            df_oi = obtener_oi(300)
            precio = precio_actual()

            if precio is None or df_1h.empty:
                time.sleep(30)
                continue

            # Niveles principales agrupados
            pivotes_h, pivotes_l = niveles_principales(df_1h)
            soporte     = nivel_mas_cercano(precio, pivotes_l, es_soporte=True)
            resistencia = nivel_mas_cercano(precio, pivotes_h, es_soporte=False)

            # Memoria de niveles (ahora las keys son valores redondeados)
            # Construimos pares (nivel, tipo) para los que están cerca del precio
            niveles_cercanos = []
            for n in pivotes_l:
                niveles_cercanos.append((n, "soporte"))
            for n in pivotes_h:
                niveles_cercanos.append((n, "resistencia"))
            nuevo_toque = actualizar_memoria_niveles(niveles_cercanos, precio, ahora_naive)
            if nuevo_toque:
                guardar_memoria(memoria_niveles)

            # Sesgo
            sesgo = calcular_sesgo(df_1h, precio)

            # Alertas
            alerta_impulso_vela(df_5m, precio, pivotes_l, pivotes_h, sesgo)
            if soporte and abs(precio - soporte) / precio <= PROXIMIDAD_NIVEL:
                alerta_nivel(precio, soporte, "soporte", df_1h)
            if resistencia and abs(precio - resistencia) / precio <= PROXIMIDAD_NIVEL:
                alerta_nivel(precio, resistencia, "resistencia", df_1h)

            alerta_movimiento_brusco(precio, df_1h)
            alerta_ruptura_rango(df_1h, precio, pivotes_h, pivotes_l)
            deriva_silenciosa(precio, ahora_utc)

            # Resumen horario
            if last_resumen is None or (ahora_naive - last_resumen) > timedelta(hours=1):
                resumen_horario(precio, soporte, resistencia, df_1h, df_oi, pivotes_h, pivotes_l)
                last_resumen = ahora_naive

            # Heartbeat cada 4h
            if last_heartbeat and (ahora_naive - last_heartbeat) > timedelta(hours=4):
                enviar(f"⏱️ Bot V14 activo — {fmt(precio)} USD — {datetime.now(UTC).strftime('%H:%M')} UTC")
                last_heartbeat = ahora_naive

            time.sleep(45)

        except Exception as e:
            print(f"Error: {e}")
            enviar(f"⚠️ Error: {str(e)[:80]}")
            time.sleep(60)

if __name__ == "__main__":
    main()
