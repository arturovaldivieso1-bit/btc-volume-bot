import os
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTCUSDT"
INTERVAL = "5m"

HEARTBEAT_HOURS = 4
NO_EVENT_HOURS = 6

IMPULSE_RANGE = 1.3
IMPULSE_VOLUME = 1.1

APPROACH_DISTANCE = 0.004
CRITICAL_DISTANCE = 0.0015
SWEEP_MIN = 0.001

KLINES_LIMIT = 200
REQUEST_TIMEOUT = 10
SLEEP_SECONDS = 60
ERROR_SLEEP_SECONDS = 120

last_impulse = None
last_sweep = None
last_breakout = None

last_event_time = datetime.utcnow()
last_heartbeat = datetime.utcnow()

alerted_liquidity = set()


session = requests.Session()


def send(msg: str) -> None:
    if not TOKEN or not CHAT_ID:
        print(f"[WARN] Missing TOKEN/CHAT_ID. Message not sent: {msg}")
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        session.post(
            url,
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=REQUEST_TIMEOUT,
        ).raise_for_status()
    except requests.RequestException as exc:
        print(f"[ERROR] Telegram send failed: {exc}")


def fmt(n: float) -> str:
    return f"{n:,.2f}" if isinstance(n, float) else f"{int(n):,}"


def get_price() -> float:
    url = "https://api.binance.com/api/v3/ticker/price"
    resp = session.get(url, params={"symbol": SYMBOL}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return float(data["price"])


def get_klines(limit: int = KLINES_LIMIT) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    resp = session.get(
        url,
        params={"symbol": SYMBOL, "interval": INTERVAL, "limit": limit},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list) or len(data) < 30:
        raise ValueError("Respuesta inválida de Binance (klines insuficientes).")

    df = pd.DataFrame(
        data,
        columns=[
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "ct",
            "q",
            "n",
            "tbb",
            "tbq",
            "ignore",
        ],
    )

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
    return df


def find_liquidity(df: pd.DataFrame):
    highs = []
    lows = []

    for i in range(3, len(df) - 3):
        h = df.iloc[i]["high"]
        if (
            h > df.iloc[i - 1]["high"]
            and h > df.iloc[i - 2]["high"]
            and h > df.iloc[i + 1]["high"]
            and h > df.iloc[i + 2]["high"]
        ):
            touches = int(((df["high"] - h).abs() / h < 0.0008).sum())
            highs.append({"price": h, "touches": touches})

        l = df.iloc[i]["low"]
        if (
            l < df.iloc[i - 1]["low"]
            and l < df.iloc[i - 2]["low"]
            and l < df.iloc[i + 1]["low"]
            and l < df.iloc[i + 2]["low"]
        ):
            touches = int(((df["low"] - l).abs() / l < 0.0008).sum())
            lows.append({"price": l, "touches": touches})

    highs = sorted(highs, key=lambda x: -x["touches"])
    lows = sorted(lows, key=lambda x: -x["touches"])

    return highs[:5], lows[:5]


def radar_impulse(df: pd.DataFrame) -> None:
    global last_impulse, last_event_time

    r = df.iloc[-1]
    range_val = (r["high"] - r["low"]) / r["close"]
    vol_mean = df["volume"].rolling(20).mean().iloc[-1]

    if pd.isna(vol_mean) or vol_mean <= 0:
        return

    vol = r["volume"] / vol_mean

    if range_val > IMPULSE_RANGE / 100 and vol > IMPULSE_VOLUME:
        if last_impulse is None or (datetime.utcnow() - last_impulse).total_seconds() > 900:
            price = get_price()
            send(
                f"""⚡ RADAR 0 — IMPULSO

Hora UTC: {datetime.utcnow().strftime('%H:%M')}

Precio: {fmt(price)}
Movimiento anómalo detectado"""
            )
            last_impulse = datetime.utcnow()
            last_event_time = datetime.utcnow()


def radar_approach(price: float, levels) -> None:
    global last_event_time

    for lvl in levels:
        dist = abs(price - lvl["price"]) / lvl["price"]
        if dist < APPROACH_DISTANCE:
            key = ("approach", round(lvl["price"]))
            if key not in alerted_liquidity:
                send(
                    f"""📡 RADAR 1 — APROXIMACIÓN

Hora UTC: {datetime.utcnow().strftime('%H:%M')}

Precio: {fmt(price)}
Liquidez: {fmt(lvl['price'])}

Distancia: {fmt(abs(price-lvl['price']))}"""
                )
                alerted_liquidity.add(key)
                last_event_time = datetime.utcnow()


def radar_critical(price: float, levels) -> None:
    global last_event_time

    for lvl in levels:
        dist = abs(price - lvl["price"]) / lvl["price"]
        if dist < CRITICAL_DISTANCE:
            key = ("critical", round(lvl["price"]))
            if key not in alerted_liquidity:
                send(
                    f"""⚠️ RADAR 2 — ZONA CRÍTICA

Hora UTC: {datetime.utcnow().strftime('%H:%M')}

Precio: {fmt(price)}
Liquidez: {fmt(lvl['price'])}

Barrido probable"""
                )
                alerted_liquidity.add(key)
                last_event_time = datetime.utcnow()


def radar_sweep(df: pd.DataFrame, levels) -> None:
    global last_sweep, last_event_time

    candle = df.iloc[-1]

    for lvl in levels:
        if candle["high"] > lvl["price"] * (1 + SWEEP_MIN) and candle["close"] < lvl["price"]:
            key = ("sweep", round(lvl["price"]))
            if key not in alerted_liquidity:
                send(
                    f"""🚨 RADAR 3 — SWEEP

Hora UTC: {datetime.utcnow().strftime('%H:%M')}

Nivel barrido: {fmt(lvl['price'])}
High sweep: {fmt(candle['high'])}

Precio actual: {fmt(candle['close'])}

Dirección probable: 🔻"""
                )
                alerted_liquidity.add(key)
                last_sweep = datetime.utcnow()
                last_event_time = datetime.utcnow()


def radar_breakout(df: pd.DataFrame, levels) -> None:
    global last_breakout, last_event_time

    candle = df.iloc[-1]

    for lvl in levels:
        if candle["close"] > lvl["price"] * (1 + SWEEP_MIN):
            key = ("break", round(lvl["price"]))
            if key not in alerted_liquidity:
                send(
                    f"""📡 RADAR 4 — BREAKOUT

Hora UTC: {datetime.utcnow().strftime('%H:%M')}

Nivel roto: {fmt(lvl['price'])}

Precio actual: {fmt(candle['close'])}

Continuación probable: 🔺"""
                )
                alerted_liquidity.add(key)
                last_breakout = datetime.utcnow()
                last_event_time = datetime.utcnow()


def heartbeat() -> None:
    global last_heartbeat

    if (datetime.utcnow() - last_heartbeat) > timedelta(hours=HEARTBEAT_HOURS):
        price = get_price()
        send(
            f"""💓 BOT ACTIVO

Hora UTC: {datetime.utcnow().strftime('%H:%M')}

Precio BTC: {fmt(price)}"""
        )
        last_heartbeat = datetime.utcnow()


def no_events() -> None:
    global last_event_time

    if (datetime.utcnow() - last_event_time) > timedelta(hours=NO_EVENT_HOURS):
        price = get_price()
        send(
            f"""🟡 SIN EVENTOS

Hora UTC: {datetime.utcnow().strftime('%H:%M')}

Precio BTC: {fmt(price)}"""
        )
        last_event_time = datetime.utcnow()


def main() -> None:
    send("🤖 BOT BTC INICIADO")

    while True:
        try:
            df = get_klines()
            price = get_price()
            highs, lows = find_liquidity(df)
            levels = highs + lows

            radar_impulse(df)
            radar_approach(price, levels)
            radar_critical(price, levels)
            radar_sweep(df, highs)
            radar_breakout(df, highs)
            heartbeat()
            no_events()

            # Evita crecimiento infinito del set en ejecuciones largas.
            if len(alerted_liquidity) > 5000:
                alerted_liquidity.clear()

            time.sleep(SLEEP_SECONDS)

        except requests.RequestException as exc:
            send(f"⚠️ ERROR RED BOT\n{str(exc)}")
            time.sleep(ERROR_SLEEP_SECONDS)

        except Exception as exc:
            send(f"⚠️ ERROR BOT\n{str(exc)}")
            time.sleep(ERROR_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
