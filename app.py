import asyncio
import os
import time

from flask import Flask, jsonify, request
from iqoptionapi.aio import AsyncIQOption

app = Flask(__name__)

TIMEFRAME = 60
CANDLE_COUNT = 100

# Ativos OTC conhecidos pelo cliente atualizado.
# Podemos acrescentar outros depois, conforme a IQ Option disponibilizar.
PARES = [
    "EURUSD-OTC",
    "GBPUSD-OTC",
    "USDJPY-OTC",
    "EURJPY-OTC",
    "NZDUSD-OTC",
    "EURGBP-OTC",
    "USDCHF-OTC",
    "GBPJPY-OTC",
    "USDSGD-OTC",
    "USDHKD-OTC",
    "USDINR-OTC",
]


def normalizar_candle(candle):
    """
    Converte o formato recebido da IQ Option
    para o formato usado pela Academy.
    """

    return {
        "from": int(candle.get("from", 0)),
        "to": int(candle.get("to", 0)),
        "open": float(candle.get("open", 0)),
        "high": float(
            candle.get(
                "max",
                candle.get("high", 0)
            )
        ),
        "low": float(
            candle.get(
                "min",
                candle.get("low", 0)
            )
        ),
        "close": float(candle.get("close", 0)),
        "volume": float(candle.get("volume", 0)),
    }


def obter_credenciais():
    email = os.getenv("IQ_EMAIL")
    password = os.getenv("IQ_PASSWORD")

    if not email:
        raise RuntimeError(
            "IQ_EMAIL não configurado no Render."
        )

    if not password:
        raise RuntimeError(
            "IQ_PASSWORD não configurado no Render."
        )

    return email, password


async def buscar_par(email, password, par):
    """
    Conecta somente para leitura, busca candles M1
    e encerra a conexão.

    Nenhuma função de compra/venda é utilizada.
    """

    cliente = AsyncIQOption(
        email,
        password
    )

    try:
        await cliente.connect()

        candles = await cliente.get_candles(
            active=par,
            size=TIMEFRAME,
            count=CANDLE_COUNT,
            endtime=int(time.time()),
            timeout=15.0,
        )

        resultado = [
            normalizar_candle(candle)
            for candle in candles
        ]

        resultado.sort(
            key=lambda candle: candle["from"]
        )

        return resultado

    finally:
        await cliente.close()


async def buscar_todos(email, password, pares):
    """
    Busca os pares sequencialmente para não sobrecarregar
    o serviço gratuito do Render.
    """

    resultados = []

    for par in pares:
        try:
            dados = await buscar_par(
                email,
                password,
                par
            )

            resultados.append({
                "par": par,
                "timeframe": "M1",
                "candles": dados,
                "quantidade": len(dados),
                "ok": True,
            })

        except Exception as erro:
            resultados.append({
                "par": par,
                "timeframe": "M1",
                "candles": [],
                "quantidade": 0,
                "ok": False,
                "erro": str(erro),
            })

    return resultados


@app.get("/")
def inicio():
    return jsonify({
        "ok": True,
        "servico": "Academy Trading - IQ Option Candles",
        "somente_dados": True,
        "operacao": False,
        "timeframe": "M1",
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "servico": "iq-option-candles",
        "somente_dados": True,
        "operacao": False,
        "timestamp": int(time.time()),
    })


@app.get("/candles")
def candles():
    try:
        email, password = obter_credenciais()

        pares_param = request.args.get(
            "pares",
            ""
        ).strip()

        if pares_param:
            pares = [
                p.strip().upper()
                for p in pares_param.split(",")
                if p.strip()
            ]
        else:
            pares = PARES

        # Limita para proteger o Render Free.
        pares = pares[:15]

        resultados = asyncio.run(
            buscar_todos(
                email,
                password,
                pares
            )
        )

        quantidade_total = sum(
            item["quantidade"]
            for item in resultados
        )

        return jsonify({
            "ok": True,
            "fonte": "IQ Option",
            "servico": "Academy Trading",
            "somente_dados": True,
            "operacao": False,
            "timeframe": "M1",
            "timestamp": int(time.time()),
            "pares_solicitados": len(pares),
            "candles_total": quantidade_total,
            "resultados": resultados,
        })

    except Exception as erro:
        return jsonify({
            "ok": False,
            "fonte": "IQ Option",
            "servico": "Academy Trading",
            "somente_dados": True,
            "operacao": False,
            "erro": str(erro),
        }), 503


@app.get("/candles/<par>")
def candles_par(par):
    """
    Consulta somente um par.
    Exemplo:
    /candles/EURUSD-OTC
    """

    try:
        email, password = obter_credenciais()

        par = par.strip().upper()

        dados = asyncio.run(
            buscar_par(
                email,
                password,
                par
            )
        )

        return jsonify({
            "ok": True,
            "fonte": "IQ Option",
            "servico": "Academy Trading",
            "somente_dados": True,
            "operacao": False,
            "par": par,
            "timeframe": "M1",
            "quantidade": len(dados),
            "candles": dados,
            "timestamp": int(time.time()),
        })

    except Exception as erro:
        return jsonify({
            "ok": False,
            "fonte": "IQ Option",
            "servico": "Academy Trading",
            "somente_dados": True,
            "operacao": False,
            "par": par,
            "erro": str(erro),
        }), 503


if __name__ == "__main__":
    porta = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=porta
    )
