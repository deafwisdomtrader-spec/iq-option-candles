import json
import math
import os
import subprocess
import sys
import time

from flask import Flask, jsonify, request

app = Flask(__name__)

TIMEFRAME = 60
CANDLE_COUNT = 100

PARES = [
    "EURUSD-OTC",
]

ESTRATEGIA = (
    "MHI + RSI + EMA21/50 + Rompimento + Pullback + Tendência"
)


# ============================================================
# CORS
# ============================================================

@app.after_request
def adicionar_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Cache-Control"] = "no-store"
    return response


# ============================================================
# RESPOSTAS
# ============================================================

def resposta_erro(mensagem, par=None, etapa=None, tipo=None):
    dados = {
        "ok": False,
        "fonte": "IQ Option",
        "servico": "Academy Trading - IQ Option Candles",
        "somente_dados": True,
        "operacao": False,
        "erro": mensagem,
    }

    if par:
        dados["par"] = par

    if etapa:
        dados["etapa"] = etapa

    if tipo:
        dados["tipo_erro"] = tipo

    return jsonify(dados), 503


# ============================================================
# EXECUTA O IQ-OPTION.PY
# ============================================================

def buscar_candles_python(par):
    """
    Executa iq-option.py em um processo separado.

    Isso é importante no Render porque, se a conexão
    da IQ Option travar, podemos encerrar o processo
    sem derrubar o Flask.
    """

    ambiente = os.environ.copy()

    ambiente["IQ_EMAIL"] = os.getenv("IQ_EMAIL", "")
    ambiente["IQ_PASSWORD"] = os.getenv("IQ_PASSWORD", "")

    comando = [
        sys.executable,
        "iq-option.py",
        par,
        str(TIMEFRAME),
        str(CANDLE_COUNT),
    ]

    try:

        processo = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            timeout=35,
            env=ambiente,
        )

    except subprocess.TimeoutExpired:

        return None, {
            "erro": "Tempo limite ao conectar ou buscar candles na IQ Option.",
            "etapa": "conectando na IQ Option",
            "tipo_erro": "TimeoutError",
        }

    except Exception as erro:

        return None, {
            "erro": str(erro),
            "etapa": "executando iq-option.py",
            "tipo_erro": type(erro).__name__,
        }

    saida = (processo.stdout or "").strip()
    erro_saida = (processo.stderr or "").strip()

    if not saida:

        return None, {
            "erro": erro_saida or "O iq-option.py não retornou dados.",
            "etapa": "resposta da IQ Option",
            "tipo_erro": "EmptyResponse",
        }

    try:

        dados = json.loads(saida)

    except json.JSONDecodeError:

        return None, {
            "erro": "Resposta do iq-option.py não é JSON válido.",
            "etapa": "interpretando resposta",
            "tipo_erro": "JSONDecodeError",
            "saida": saida[-2000:],
        }

    if not dados.get("ok"):

        return None, dados

    return dados.get("candles", []), None


# ============================================================
# INDICADORES
# ============================================================

def fechar(candles):
    return [
        float(c["close"])
        for c in candles
        if c.get("close") is not None
    ]


def calcular_ema(valores, periodo):
    if not valores:
        return None

    if len(valores) < periodo:
        return None

    multiplicador = 2 / (periodo + 1)

    ema = sum(valores[:periodo]) / periodo

    for valor in valores[periodo:]:
        ema = (
            (valor - ema) * multiplicador
        ) + ema

    return ema


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

    ganho_medio = sum(
        ganhos[:periodo]
    ) / periodo

    perda_media = sum(
        perdas[:periodo]
    ) / periodo

    for i in range(periodo, len(ganhos)):

        ganho_medio = (
            (ganho_medio * (periodo - 1))
            + ganhos[i]
        ) / periodo

        perda_media = (
            (perda_media * (periodo - 1))
            + perdas[i]
        ) / periodo

    if perda_media == 0:
        return 100.0

    rs = ganho_medio / perda_media

    return 100 - (100 / (1 + rs))


def calcular_pivo(candle):

    maxima = float(candle["high"])
    minima = float(candle["low"])
    fechamento = float(candle["close"])

    return (
        maxima +
        minima +
        fechamento
    ) / 3


# ============================================================
# ANÁLISE
# ============================================================

def analisar(candles, par):

    if len(candles) < 50:

        return {
            "par": par,
            "hora": "--:--",
            "sinal": "AGUARDANDO",
            "status": "POUCOS DADOS",
            "rsi": None,
            "mm": None,
            "pivo": None,
            "preco": None,
        }

    valores = fechar(candles)

    preco = valores[-1]

    ema21 = calcular_ema(
        valores,
        21
    )

    ema50 = calcular_ema(
        valores,
        50
    )

    rsi = calcular_rsi(
        valores,
        14
    )

    candle_atual = candles[-1]

    candle_anterior = candles[-2]

    pivo = calcular_pivo(
        candle_anterior
    )

    # --------------------------------------------------------
    # TENDÊNCIA
    # --------------------------------------------------------

    tendencia_alta = (
        ema21 is not None
        and ema50 is not None
        and ema21 > ema50
        and preco > ema21
    )

    tendencia_baixa = (
        ema21 is not None
        and ema50 is not None
        and ema21 < ema50
        and preco < ema21
    )

    # --------------------------------------------------------
    # ROMPIMENTO
    # --------------------------------------------------------

    ultimos = candles[-6:-1]

    resistencia = max(
        float(c["high"])
        for c in ultimos
    )

    suporte = min(
        float(c["low"])
        for c in ultimos
    )

    fechamento_atual = float(
        candle_atual["close"]
    )

    rompimento_alta = (
        fechamento_atual > resistencia
    )

    rompimento_baixa = (
        fechamento_atual < suporte
    )

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    margem = (
        abs(ema21) * 0.0003
        if ema21
        else 0
    )

    pullback_alta = (
        ema21 is not None
        and candle_atual["low"]
        <= ema21 + margem
        and fechamento_atual > ema21
    )

    pullback_baixa = (
        ema21 is not None
        and candle_atual["high"]
        >= ema21 - margem
        and fechamento_atual < ema21
    )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    rsi_call = (
        rsi is not None
        and 50 <= rsi <= 70
    )

    rsi_put = (
        rsi is not None
        and 30 <= rsi <= 50
    )

    # --------------------------------------------------------
    # PONTUAÇÃO
    # --------------------------------------------------------

    pontos_call = 0
    pontos_put = 0

    if tendencia_alta:
        pontos_call += 2

    if tendencia_baixa:
        pontos_put += 2

    if rsi_call:
        pontos_call += 1

    if rsi_put:
        pontos_put += 1

    if rompimento_alta:
        pontos_call += 2

    if rompimento_baixa:
        pontos_put += 2

    if pullback_alta:
        pontos_call += 2

    if pullback_baixa:
        pontos_put += 2

    # --------------------------------------------------------
    # SINAL
    # --------------------------------------------------------

    sinal = "AGUARDANDO"
    status = "AGUARDANDO CONFIRMAÇÃO"

    if pontos_call >= 5 and pontos_call > pontos_put:

        sinal = "CALL"
        status = "CONFIRMAÇÃO DE ALTA"

    elif pontos_put >= 5 and pontos_put > pontos_call:

        sinal = "PUT"
        status = "CONFIRMAÇÃO DE BAIXA"

    # --------------------------------------------------------
    # MM
    # --------------------------------------------------------

    if ema21 is not None and ema50 is not None:

        if ema21 > ema50:
            mm = "EMA21 > EMA50"

        elif ema21 < ema50:
            mm = "EMA21 < EMA50"

        else:
            mm = "NEUTRA"

    else:
        mm = "--"

    # --------------------------------------------------------
    # PIVÔ
    # --------------------------------------------------------

    if rompimento_alta:
        texto_pivo = "ROMPIMENTO ALTA"

    elif rompimento_baixa:
        texto_pivo = "ROMPIMENTO BAIXA"

    elif pullback_alta:
        texto_pivo = "PULLBACK ALTA"

    elif pullback_baixa:
        texto_pivo = "PULLBACK BAIXA"

    else:
        texto_pivo = round(
            pivo,
            6
        )

    # --------------------------------------------------------
    # HORA
    # --------------------------------------------------------

    hora = time.strftime(
        "%H:%M",
        time.localtime(
            int(candle_atual["from"])
        )
    )

    return {
        "par": par,
        "hora": hora,
        "sinal": sinal,
        "status": status,
        "rsi": round(rsi, 2) if rsi is not None else None,
        "mm": mm,
        "pivo": texto_pivo,
        "preco": round(preco, 6),
        "ema21": round(ema21, 6) if ema21 else None,
        "ema50": round(ema50, 6) if ema50 else None,
        "pontos_call": pontos_call,
        "pontos_put": pontos_put,
        "estrategia": ESTRATEGIA,
    }


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
        "estrategia": ESTRATEGIA,
        "validade_teste": "1 minuto",
    })


@app.get("/health")
def health():

    return jsonify({
        "ok": True,
        "servico": "iq-option-candles",
        "status": "online",
        "timestamp": int(time.time()),
    })


@app.route("/candles/<par>", methods=["GET", "OPTIONS"])
def candles_par(par):

    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    par = par.strip().upper()

    if not par.endswith("-OTC"):
        return resposta_erro(
            "Somente ativos OTC são permitidos neste teste.",
            par=par,
            etapa="validando ativo",
            tipo="InvalidPair",
        )

    candles, erro = buscar_candles_python(par)

    if erro is not None:

        return resposta_erro(
            erro.get(
                "erro",
                "Erro ao obter candles."
            ),
            par=par,
            etapa=erro.get("etapa"),
            tipo=erro.get("tipo_erro"),
        )

    if not candles:

        return resposta_erro(
            "A IQ Option não retornou candles.",
            par=par,
            etapa="recebendo candles",
            tipo="EmptyCandles",
        )

    analise = analisar(
        candles,
        par
    )

    return jsonify({
        "ok": True,
        "fonte": "IQ Option",
        "servico": "Academy Trading",
        "somente_dados": True,
        "operacao": False,
        "par": par,
        "timeframe": "M1",
        "quantidade": len(candles),
        "candles": candles,
        "analise": analise,
        "resultados": [analise],
        "estrategia": ESTRATEGIA,
        "timestamp": int(time.time()),
    })


@app.get("/candles")
def candles():

    par = request.args.get(
        "par",
        "EURUSD-OTC"
    ).strip().upper()

    return candles_par(par)


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
