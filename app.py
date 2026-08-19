import asyncio
import os
import time
from statistics import mean

from flask import Flask, jsonify, request
from iqoptionapi.aio import AsyncIQOption

app = Flask(__name__)

TIMEFRAME = 60
CANDLE_COUNT = 100

# Quantidade mínima de confirmações para liberar sinal.
MIN_CONFIRMACOES = 5

# Indicadores
EMA_RAPIDA = 21
EMA_LENTA = 50
RSI_PERIODO = 14
MHI_PERIODO = 5

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


# ============================================================
# CANDLE
# ============================================================

def normalizar_candle(candle):
    return {
        "from": int(candle.get("from", 0)),
        "to": int(candle.get("to", 0)),
        "open": float(candle.get("open", 0)),
        "high": float(
            candle.get("max", candle.get("high", 0))
        ),
        "low": float(
            candle.get("min", candle.get("low", 0))
        ),
        "close": float(candle.get("close", 0)),
        "volume": float(candle.get("volume", 0)),
    }


# ============================================================
# CREDENCIAIS
# ============================================================

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


# ============================================================
# EMA
# ============================================================

def calcular_ema(valores, periodo):
    if len(valores) < periodo:
        return None

    multiplicador = 2 / (periodo + 1)

    ema = mean(valores[:periodo])

    for preco in valores[periodo:]:
        ema = (
            (preco - ema) * multiplicador
        ) + ema

    return ema


# ============================================================
# RSI
# ============================================================

def calcular_rsi(valores, periodo=14):
    if len(valores) < periodo + 1:
        return None

    ganhos = []
    perdas = []

    for i in range(1, len(valores)):
        diferenca = valores[i] - valores[i - 1]

        if diferenca > 0:
            ganhos.append(diferenca)
            perdas.append(0)

        else:
            ganhos.append(0)
            perdas.append(abs(diferenca))

    ganhos = ganhos[-periodo:]
    perdas = perdas[-periodo:]

    media_ganho = sum(ganhos) / periodo
    media_perda = sum(perdas) / periodo

    if media_perda == 0:
        return 100.0

    rs = media_ganho / media_perda

    return 100 - (100 / (1 + rs))


# ============================================================
# MHI
# ============================================================

def analisar_mhi(candles):
    ultimas = candles[-MHI_PERIODO:]

    if len(ultimas) < MHI_PERIODO:
        return {
            "sinal": "AGUARDANDO",
            "confirmado": False,
            "altas": 0,
            "baixas": 0,
        }

    altas = 0
    baixas = 0

    for candle in ultimas:
        if candle["close"] > candle["open"]:
            altas += 1

        elif candle["close"] < candle["open"]:
            baixas += 1

    if altas > baixas:
        direcao = "PUT"

    elif baixas > altas:
        direcao = "CALL"

    else:
        direcao = "AGUARDANDO"

    return {
        "sinal": direcao,
        "confirmado": direcao != "AGUARDANDO",
        "altas": altas,
        "baixas": baixas,
    }


# ============================================================
# TENDÊNCIA
# ============================================================

def analisar_tendencia(candles):
    closes = [
        candle["close"]
        for candle in candles
    ]

    if len(closes) < EMA_LENTA:
        return {
            "direcao": "NEUTRA",
            "ema21": None,
            "ema50": None,
        }

    ema21 = calcular_ema(
        closes,
        EMA_RAPIDA
    )

    ema50 = calcular_ema(
        closes,
        EMA_LENTA
    )

    if ema21 is None or ema50 is None:
        return {
            "direcao": "NEUTRA",
            "ema21": ema21,
            "ema50": ema50,
        }

    if ema21 > ema50:
        direcao = "ALTA"

    elif ema21 < ema50:
        direcao = "BAIXA"

    else:
        direcao = "NEUTRA"

    return {
        "direcao": direcao,
        "ema21": round(ema21, 6),
        "ema50": round(ema50, 6),
    }


# ============================================================
# ROMPIMENTO
# ============================================================

def analisar_rompimento(candles):
    if len(candles) < 21:
        return {
            "direcao": "NEUTRO",
            "rompimento": False,
        }

    atual = candles[-1]

    anteriores = candles[-21:-1]

    resistencia = max(
        candle["high"]
        for candle in anteriores
    )

    suporte = min(
        candle["low"]
        for candle in anteriores
    )

    if atual["close"] > resistencia:
        return {
            "direcao": "ALTA",
            "rompimento": True,
            "resistencia": resistencia,
            "suporte": suporte,
        }

    if atual["close"] < suporte:
        return {
            "direcao": "BAIXA",
            "rompimento": True,
            "resistencia": resistencia,
            "suporte": suporte,
        }

    return {
        "direcao": "NEUTRO",
        "rompimento": False,
        "resistencia": resistencia,
        "suporte": suporte,
    }


# ============================================================
# PULLBACK
# ============================================================

def analisar_pullback(candles):
    if len(candles) < 25:
        return {
            "direcao": "NEUTRO",
            "pullback": False,
        }

    closes = [
        candle["close"]
        for candle in candles
    ]

    ema21 = calcular_ema(
        closes,
        EMA_RAPIDA
    )

    if ema21 is None:
        return {
            "direcao": "NEUTRO",
            "pullback": False,
        }

    atual = candles[-1]

    distancia = abs(
        atual["close"] - ema21
    )

    tolerancia = abs(
        ema21
    ) * 0.0015

    perto_da_ema = distancia <= tolerancia

    if not perto_da_ema:
        return {
            "direcao": "NEUTRO",
            "pullback": False,
            "ema21": round(ema21, 6),
        }

    # Retoma para cima da EMA
    if atual["close"] > ema21:
        return {
            "direcao": "ALTA",
            "pullback": True,
            "ema21": round(ema21, 6),
        }

    # Retoma para baixo da EMA
    if atual["close"] < ema21:
        return {
            "direcao": "BAIXA",
            "pullback": True,
            "ema21": round(ema21, 6),
        }

    return {
        "direcao": "NEUTRO",
        "pullback": False,
        "ema21": round(ema21, 6),
    }


# ============================================================
# LINHA DE TENDÊNCIA
# ============================================================

def analisar_linha_tendencia(candles):
    if len(candles) < 10:
        return {
            "direcao": "NEUTRA"
        }

    fechamentos = [
        candle["close"]
        for candle in candles[-10:]
    ]

    primeira = fechamentos[0]
    ultima = fechamentos[-1]

    if ultima > primeira:
        direcao = "ALTA"

    elif ultima < primeira:
        direcao = "BAIXA"

    else:
        direcao = "NEUTRA"

    return {
        "direcao": direcao
    }


# ============================================================
# ANÁLISE COMPLETA
# ============================================================

def analisar_sinal(candles):
    if len(candles) < EMA_LENTA + 5:
        return {
            "sinal": "AGUARDANDO",
            "confirmado": False,
            "motivo": "Aguardando histórico suficiente.",
        }

    fechadas = [
        candle
        for candle in candles
        if candle["to"] <= int(time.time())
    ]

    if len(fechadas) < EMA_LENTA:
        return {
            "sinal": "AGUARDANDO",
            "confirmado": False,
            "motivo": "Aguardando velas fechadas.",
        }

    closes = [
        candle["close"]
        for candle in fechadas
    ]

    rsi = calcular_rsi(
        closes,
        RSI_PERIODO
    )

    tendencia = analisar_tendencia(
        fechadas
    )

    mhi = analisar_mhi(
        fechadas
    )

    rompimento = analisar_rompimento(
        fechadas
    )

    pullback = analisar_pullback(
        fechadas
    )

    linha = analisar_linha_tendencia(
        fechadas
    )

    pontos_call = 0
    pontos_put = 0

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    rsi_call = False
    rsi_put = False

    if rsi is not None:

        if rsi > 50:
            pontos_call += 1
            rsi_call = True

        elif rsi < 50:
            pontos_put += 1
            rsi_put = True

    # --------------------------------------------------------
    # EMA 21 / 50
    # --------------------------------------------------------

    ema_call = tendencia["direcao"] == "BAIXA"
    ema_put = tendencia["direcao"] == "ALTA"

    if ema_call:
        pontos_call += 1

    if ema_put:
        pontos_put += 1

    # --------------------------------------------------------
    # MHI
    # --------------------------------------------------------

    if mhi["sinal"] == "CALL":
        pontos_call += 1

    elif mhi["sinal"] == "PUT":
        pontos_put += 1

    # --------------------------------------------------------
    # ROMPIMENTO
    # --------------------------------------------------------

    rompimento_call = (
        rompimento["direcao"] == "BAIXA"
    )

    rompimento_put = (
        rompimento["direcao"] == "ALTA"
    )

    if rompimento_call:
        pontos_call += 1

    if rompimento_put:
        pontos_put += 1

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    pullback_call = (
        pullback["direcao"] == "BAIXA"
    )

    pullback_put = (
        pullback["direcao"] == "ALTA"
    )

    if pullback_call:
        pontos_call += 1

    if pullback_put:
        pontos_put += 1

    # --------------------------------------------------------
    # LINHA DE TENDÊNCIA
    # --------------------------------------------------------

    linha_call = (
        linha["direcao"] == "BAIXA"
    )

    linha_put = (
        linha["direcao"] == "ALTA"
    )

    if linha_call:
        pontos_call += 1

    if linha_put:
        pontos_put += 1

    # --------------------------------------------------------
    # DECISÃO
    # --------------------------------------------------------

    if (
        pontos_call >= MIN_CONFIRMACOES
        and pontos_call > pontos_put
    ):
        sinal = "CALL"

    elif (
        pontos_put >= MIN_CONFIRMACOES
        and pontos_put > pontos_call
    ):
        sinal = "PUT"

    else:
        sinal = "AGUARDANDO"

    agora = int(time.time())

    proxima_vela = (
        (agora // TIMEFRAME) + 1
    ) * TIMEFRAME

    segundos = max(
        0,
        proxima_vela - agora
    )

    return {
        "sinal": sinal,
        "confirmado": sinal != "AGUARDANDO",
        "estrategia": "MHI + RSI + EMA + Rompimento + Pullback + Tendência",
        "periodo": "M1",
        "validade": "1 minuto",
        "validade_segundos": segundos,

        "pontuacao": {
            "CALL": pontos_call,
            "PUT": pontos_put,
            "minimo": MIN_CONFIRMACOES,
        },

        "indicadores": {
            "rsi": round(rsi, 2) if rsi is not None else None,
            "ema21": tendencia["ema21"],
            "ema50": tendencia["ema50"],
            "tendencia": tendencia["direcao"],
            "mhi": mhi,
            "rompimento": rompimento,
            "pullback": pullback,
            "linha_tendencia": linha,
        },

        "timestamp": agora,
        "proxima_vela": proxima_vela,

        "aviso": (
            "Sinal educacional. "
            "Não executa operações e não garante resultado."
        ),
    }


# ============================================================
# CONEXÃO IQ OPTION
# ============================================================

async def buscar_par(email, password, par):

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

    resultados = []

    for par in pares:

        try:

            dados = await buscar_par(
                email,
                password,
                par
            )

            sinal = analisar_sinal(
                dados
            )

            resultados.append({
                "par": par,
                "timeframe": "M1",
                "candles": dados,
                "quantidade": len(dados),
                "ok": True,
                "sinal": sinal,
            })

        except Exception as erro:

            resultados.append({
                "par": par,
                "timeframe": "M1",
                "candles": [],
                "quantidade": 0,
                "ok": False,
                "erro": str(erro),
                "sinal": {
                    "sinal": "AGUARDANDO",
                    "confirmado": False,
                },
            })

    return resultados


# ============================================================
# ROTAS
# ============================================================

@app.get("/")
def inicio():

    return jsonify({
        "ok": True,
        "servico": "Academy Trading - IQ Option Candles",
        "somente_dados": True,
        "operacao": False,
        "timeframe": "M1",
        "estrategia": (
            "MHI + RSI + EMA21/50 + "
            "Rompimento + Pullback + Tendência"
        ),
        "validade_teste": "1 minuto",
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
            "estrategia": (
                "MHI + RSI + EMA21/50 + "
                "Rompimento + Pullback + Tendência"
            ),
            "validade_teste": "1 minuto",
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

        sinal = analisar_sinal(
            dados
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
            "sinal": sinal,
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


# ============================================================
# EXECUÇÃO LOCAL
# ============================================================

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
