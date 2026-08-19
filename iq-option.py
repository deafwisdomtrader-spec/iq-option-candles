import os
import sys
import json
import time

from iqoptionapi.stable_api import IQ_Option


# ============================================================
# CONFIGURAÇÃO
# ============================================================

EMAIL = os.getenv("IQ_EMAIL")
PASSWORD = os.getenv("IQ_PASSWORD")

TIMEFRAME = 60
CANDLE_COUNT = 100

PARES = [
    "EURUSD-OTC",
    "GBPUSD-OTC",
    "USDJPY-OTC",
    "EURJPY-OTC",
    "AUDUSD-OTC",
    "USDCAD-OTC",
    "GBPJPY-OTC",
    "EURGBP-OTC",
    "USDCHF-OTC",
    "AUDJPY-OTC",
    "NZDUSD-OTC",
    "EURCAD-OTC",
    "GBPAUD-OTC",
    "CADJPY-OTC",
    "EURAUD-OTC",
    "XAUUSD-OTC",
]


# ============================================================
# NORMALIZA CANDLE
# ============================================================

def normalizar_candle(candle):
    return {
        "from": int(candle.get("from", 0)),
        "open": float(candle.get("open", 0)),
        "high": float(candle.get("max", 0)),
        "low": float(candle.get("min", 0)),
        "close": float(candle.get("close", 0)),
        "volume": float(candle.get("volume", 0)),
    }


# ============================================================
# BUSCA CANDLES
# ============================================================

def buscar_candles(iq, par):

    try:

        timestamp = iq.get_server_timestamp()

        candles = iq.get_candles(
            par,
            TIMEFRAME,
            CANDLE_COUNT,
            timestamp
        )

        if not candles:
            return []

        candles = [
            normalizar_candle(c)
            for c in candles
        ]

        candles.sort(
            key=lambda x: x["from"]
        )

        return candles

    except Exception:
        return []


# ============================================================
# RESPOSTA
# ============================================================

def main():

    if not EMAIL or not PASSWORD:

        print(json.dumps({
            "ok": False,
            "erro": "Credenciais do motor não configuradas."
        }, ensure_ascii=False))

        sys.exit(1)


    iq = IQ_Option(
        EMAIL,
        PASSWORD
    )


    conectado, motivo = iq.connect()


    if not conectado:

        print(json.dumps({
            "ok": False,
            "erro": "Não foi possível conectar à IQ Option.",
            "detalhe": str(motivo)
        }, ensure_ascii=False))

        sys.exit(1)


    resultados = []


    for par in PARES:

        candles = buscar_candles(
            iq,
            par
        )


        resultados.append({
            "par": par,
            "timeframe": "M1",
            "candles": candles,
            "quantidade": len(candles)
        })


    resposta = {
        "ok": True,
        "fonte": "IQ Option",
        "somente_dados": True,
        "operacao": False,
        "timeframe": "M1",
        "timestamp": int(time.time()),
        "resultados": resultados
    }


    print(
        json.dumps(
            resposta,
            ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()