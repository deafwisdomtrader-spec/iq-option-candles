import os
import time
from flask import Flask, jsonify, request
from iqoptionapi.stable_api import IQ_Option

app = Flask(__name__)

TIMEFRAME = 60
CANDLE_COUNT = 100

PARES = [
    "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "EURJPY-OTC",
    "AUDUSD-OTC", "USDCAD-OTC", "GBPJPY-OTC", "EURGBP-OTC",
    "USDCHF-OTC", "AUDJPY-OTC", "NZDUSD-OTC", "EURCAD-OTC",
    "GBPAUD-OTC", "CADJPY-OTC", "EURAUD-OTC", "XAUUSD-OTC",
]

_iq = None
_ultima_conexao = 0


def normalizar_candle(candle):
    return {
        "from": int(candle.get("from", 0)),
        "open": float(candle.get("open", 0)),
        "high": float(candle.get("max", 0)),
        "low": float(candle.get("min", 0)),
        "close": float(candle.get("close", 0)),
        "volume": float(candle.get("volume", 0)),
    }


def conectar():
    global _iq, _ultima_conexao

    email = os.getenv("IQ_EMAIL")
    password = os.getenv("IQ_PASSWORD")

    if not email or not password:
        raise RuntimeError("IQ_EMAIL e IQ_PASSWORD não configurados no Render.")

    if _iq is not None and getattr(_iq, "check_connect", False):
        return _iq

    _iq = IQ_Option(email, password)
    conectado, motivo = _iq.connect()

    if not conectado:
        _iq = None
        raise RuntimeError(f"Não foi possível conectar à IQ Option: {motivo}")

    _ultima_conexao = int(time.time())
    return _iq


def buscar_candles(iq, par):
    timestamp = iq.get_server_timestamp()

    candles = iq.get_candles(
        par,
        TIMEFRAME,
        CANDLE_COUNT,
        timestamp
    )

    if not candles:
        return []

    resultado = [normalizar_candle(c) for c in candles]
    resultado.sort(key=lambda x: x["from"])
    return resultado


@app.get("/")
def inicio():
    return jsonify({
        "ok": True,
        "servico": "Academy Trading - IQ Option Candles",
        "somente_dados": True,
        "operacao": False
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "servico": "iq-option-candles",
        "timestamp": int(time.time())
    })


@app.get("/candles")
def candles():
    try:
        iq = conectar()

        pares_param = request.args.get("pares", "").strip()

        if pares_param:
            pares = [
                p.strip().upper()
                for p in pares_param.split(",")
                if p.strip()
            ]
        else:
            pares = PARES

        # Limite para evitar requisições exageradas no serviço gratuito.
        pares = pares[:20]

        resultados = []

        for par in pares:
            try:
                dados = buscar_candles(iq, par)

                resultados.append({
                    "par": par,
                    "timeframe": "M1",
                    "candles": dados,
                    "quantidade": len(dados)
                })

            except Exception as erro:
                resultados.append({
                    "par": par,
                    "timeframe": "M1",
                    "candles": [],
                    "quantidade": 0,
                    "erro": str(erro)
                })

        return jsonify({
            "ok": True,
            "fonte": "IQ Option",
            "somente_dados": True,
            "operacao": False,
            "timeframe": "M1",
            "timestamp": int(time.time()),
            "resultados": resultados
        })

    except Exception as erro:
        return jsonify({
            "ok": False,
            "fonte": "IQ Option",
            "somente_dados": True,
            "operacao": False,
            "erro": str(erro)
        }), 503


if __name__ == "__main__":
    porta = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=porta)
