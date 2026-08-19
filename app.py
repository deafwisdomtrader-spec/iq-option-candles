import asyncio
import os
import time
from statistics import mean

from flask import Flask, jsonify, request
from iqoptionapi.aio import AsyncIQOption

app = Flask(__name__)

TIMEFRAME = 60
CANDLE_COUNT = 100

EMA_RAPIDA = 21
EMA_LENTA = 50
RSI_PERIODO = 14
MHI_PERIODO = 5

# Para o primeiro teste:
VALIDADE_MINUTOS = 1

# Quantidade mínima de confirmações.
MIN_CONFIRMACOES = 5

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

    ema = mean(valores[:periodo])
    multiplicador = 2 / (periodo + 1)

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

        elif diferenca < 0:
            ganhos.append(0)
            perdas.append(abs(diferenca))

        else:
            ganhos.append(0)
            perdas.append(0)

    ganhos = ganhos[-periodo:]
    perdas = perdas[-periodo:]

    media_ganho = sum(ganhos) / periodo
    media_perda = sum(perdas) / periodo

    if media_perda == 0:
        if media_ganho == 0:
            return 50.0

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
            "neutras": 0,
        }

    altas = 0
    baixas = 0
    neutras = 0

    for candle in ultimas:
        abertura = candle["open"]
        fechamento = candle["close"]

        if fechamento > abertura:
            altas += 1

        elif fechamento < abertura:
            baixas += 1

        else:
            neutras += 1

    if altas > baixas:
        sinal = "PUT"

    elif baixas > altas:
        sinal = "CALL"

    else:
        sinal = "AGUARDANDO"

    return {
        "sinal": sinal,
        "confirmado": sinal != "AGUARDANDO",
        "altas": altas,
        "baixas": baixas,
        "neutras": neutras,
    }


# ============================================================
# TENDÊNCIA / EMA 21/50
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
            "resistencia": round(resistencia, 6),
            "suporte": round(suporte, 6),
        }

    if atual["close"] < suporte:
        return {
            "direcao": "BAIXA",
            "rompimento": True,
            "resistencia": round(resistencia, 6),
            "suporte": round(suporte, 6),
        }

    return {
        "direcao": "NEUTRO",
        "rompimento": False,
        "resistencia": round(resistencia, 6),
        "suporte": round(suporte, 6),
    }


# ============================================================
# PULLBACK
# ============================================================

def analisar_pullback(candles):
    if len(candles) < EMA_RAPIDA + 2:
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

    tolerancia = abs(ema21) * 0.0015

    perto_da_ema = distancia <= tolerancia

    if not perto_da_ema:
        return {
            "direcao": "NEUTRO",
            "pullback": False,
            "ema21": round(ema21, 6),
        }

    if atual["close"] > ema21:
        return {
            "direcao": "ALTA",
            "pullback": True,
            "ema21": round(ema21, 6),
        }

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

    agora = int(time.time())

    # Somente velas fechadas.
    fechadas = [
        candle
        for candle in candles
        if candle["to"] <= agora
    ]

    fechadas.sort(
        key=lambda candle: candle["from"]
    )

    if len(fechadas) < EMA_LENTA + 5:
        return {
            "sinal": "AGUARDANDO",
            "confirmado": False,
            "motivo": "Histórico insuficiente.",
            "velas_disponiveis": len(fechadas),
            "minimo_necessario": EMA_LENTA + 5,
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

    if rsi is not None:

        if rsi > 50:
            pontos_call += 1

        elif rsi < 50:
            pontos_put += 1

    # --------------------------------------------------------
    # EMA 21 / 50
    # --------------------------------------------------------

    if tendencia["direcao"] == "BAIXA":
        pontos_call += 1

    elif tendencia["direcao"] == "ALTA":
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

    if rompimento["direcao"] == "BAIXA":
        pontos_call += 1

    elif rompimento["direcao"] == "ALTA":
        pontos_put += 1

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    if pullback["direcao"] == "BAIXA":
        pontos_call += 1

    elif pullback["direcao"] == "ALTA":
        pontos_put += 1

    # --------------------------------------------------------
    # LINHA DE TENDÊNCIA
    # --------------------------------------------------------

    if linha["direcao"] == "BAIXA":
        pontos_call += 1

    elif linha["direcao"] == "ALTA":
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

    proxima_vela = (
        (agora // TIMEFRAME) + 1
    ) * TIMEFRAME

    segundos_restantes = max(
        0,
        proxima_vela - agora
    )

    return {
        "sinal": sinal,
        "confirmado": sinal != "AGUARDANDO",
        "estrategia": (
            "MHI + RSI + EMA21/50 + "
            "Rompimento + Pullback + Tendência"
        ),
        "periodo": "M1",
        "validade": "1 minuto",
        "validade_segundos": segundos_restantes,

        "pontuacao": {
            "CALL": pontos_call,
            "PUT": pontos_put,
            "minimo": MIN_CONFIRMACOES,
        },

        "indicadores": {
            "rsi": (
                round(rsi, 2)
                if rsi is not None
                else None
            ),
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
# IQ OPTION
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
                "erro": (
                    str(erro)
                    or repr(erro)
                    or type(erro).__name__
                ),
                "tipo_erro": type(erro).__name__,
                "sinal": {
                    "sinal": "AGUARDANDO",
                    "confirmado": False,
                },
            })

    return resultados


# ============================================================
# HOME
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


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return jsonify({
        "ok": True,
        "servico": "iq-option-candles",
        "somente_dados": True,
        "operacao": False,
        "timestamp": int(time.time()),
    })


# ============================================================
# TODOS OS PARES
# ============================================================

@app.get("/candles")
def candles():

    etapa = "iniciando"

    try:

        etapa = "obtendo credenciais"

        email, password = obter_credenciais()

        etapa = "lendo parâmetros"

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

        etapa = "buscando candles"

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
            "etapa": etapa,
            "tipo_erro": type(erro).__name__,
            "erro": (
                str(erro)
                or repr(erro)
                or type(erro).__name__
            ),
        }), 503


# ============================================================
# UM PAR
# ============================================================

@app.get("/candles/<par>")
def candles_par(par):

    etapa = "iniciando"

    try:

        etapa = "obtendo credenciais"

        email, password = obter_credenciais()

        etapa = "preparando par"

        par = par.strip().upper()

        if not par:
            raise ValueError(
                "Par não informado."
            )

        etapa = "conectando na IQ Option"

        dados = asyncio.run(
            buscar_par(
                email,
                password,
                par
            )
        )

        etapa = "recebendo candles"

        if not dados:
            raise RuntimeError(
                "A IQ Option não retornou candles."
            )

        etapa = "analisando indicadores"

        sinal = analisar_sinal(
            dados
        )

        etapa = "montando resposta"

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
            "etapa": etapa,
            "tipo_erro": type(erro).__name__,
            "erro": (
                str(erro)
                or repr(erro)
                or type(erro).__name__
            ),
        }), 503


# ============================================================
# EXECUÇÃO
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
