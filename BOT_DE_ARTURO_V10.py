# ... (todo el código anterior igual, solo modifico las funciones de requests y añado mensaje inicial)

def obtener_candles(interval, limit=200):
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "limit": limit
    }
    # Añadimos timeout de 10 segundos
    data = requests.get(url, params=params, timeout=10).json()
    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "_","_","_","_","_","_"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df

# ... (resto de funciones igual)

# --- Mensaje de inicio inmediato por Telegram ---
enviar("🤖 BOT BTC INICIADO (V10 con RADAR 0,1,2,3,4 operativos)")

while True:
    try:
        evaluar()
    except Exception as e:
        print("Error en el ciclo principal:", e)
        enviar(f"⚠️ ERROR EN BOT: {str(e)}")  # Opcional: notificar error por Telegram
    time.sleep(60)
