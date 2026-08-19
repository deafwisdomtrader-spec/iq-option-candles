import os
import time
import threading
from datetime import datetime

from flask import Flask, jsonify, request
from iqoptionapi.stable_api import IQ_Option

app = Flask(__name__)

# ============================================================
# CONFIGURAÇÃO
# ============================================================

TIMEFRAME = 60
CANDLE_COUNT = 100

PARES = [
    "EURUSD-OTC",
    "GBPUSD-OTC",
    "USDJPY-OTC",
    "EURJPY-OTC",
    "NZDUSD-OTC",
    "EURGBP-OTC",
    "USDCHF-OTC",
    "GBPJPY-OTC",
    "AUDUSD-OTC",
    "USDCAD-OTC",
]

# ============================================================
# CONEXÃO PERSISTENTE
# ============================================================

_iq = None
_lock = threading.Lock()
_ultima_conexao = 0


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
# CONEXÃO IQ OPTION
# ============================================================

def conectar():

    global _iq
    global _ultima_conexao

    email, password = obter_credenciais()

    with _lock:

        # ----------------------------------------------------
        # Reutiliza conexão existente
        # ----------------------------------------------------

        if _iq is not None:

            try:

                if _iq.check_connect():

                    return _iq

            except Exception:

                pass

        # ----------------------------------------------------
        # Nova conexão
        # ----------------------------------------------------

        _iq = None

        cliente = IQ_Option(
            email,
            password
        )

        cliente.set_max_reconnect(3)

        inicio = time.time()

        conectado, motivo = cliente.connect()

        duracao = round(
            time.time() - inicio,
            2
        )

        if not conectado:

            _iq = None

            raise RuntimeError(
                f"Falha ao conectar na IQ Option "
                f"após {duracao}s: {motivo}"
            )

        # ----------------------------------------------------
        # Guarda conexão
        # ----------------------------------------------------

        _iq = cliente

        _ultima_conexao = int(
            time.time()
        )

        return _iq


# ============================================================
# NORMALIZA CANDLE
# ============================================================

def normalizar_candle(candle):

    return {

        "from": int(
            candle.get("from", 0)
        ),

        "to": int(
            candle.get("to", 0)
        ),

        "open": float(
            candle.get("open", 0)
        ),

        "high": float(
            candle.get(
                "max",
                candle.get(
                    "high",
                    0
                )
            )
        ),

        "low": float(
            candle.get(
                "min",
                candle.get(
                    "low",
                    0
                )
            )
        ),

        "close": float(
            candle.get(
                "close",
                0
            )
        ),

        "volume": float(
            candle.get(
                "volume",
                0
            )
        ),
    }


# ============================================================
# BUSCAR CANDLES
# ============================================================

def buscar_candles(
    iq,
    par,
    quantidade=CANDLE_COUNT
):

    timestamp = int(
        time.time()
    )

    candles = iq.get_candles(
        par,
        TIMEFRAME,
        quantidade,
        timestamp
    )

    if not candles:

        return []

    resultado = [
        normalizar_candle(candle)
        for candle in candles
    ]

    resultado.sort(
        key=lambda x: x["from"]
    )

    return resultado


# ============================================================
# EMA
# ============================================================

def calcular_ema(valores, periodo):

    if not valores:
        return None

    if len(valores) < periodo:

        return None

    multiplicador = 2 / (
        periodo + 1
    )

    ema = sum(
        valores[:periodo]
    ) / periodo

    for preco in valores[periodo:]:

        ema = (
            (preco - ema)
            * multiplicador
        ) + ema

    return round(
        ema,
        6
    )


# ============================================================
# RSI
# ============================================================

def calcular_rsi(
    fechamentos,
    periodo=14
):

    if len(fechamentos) <= periodo:

        return None

    ganhos = []
    perdas = []

    for i in range(
        1,
        len(fechamentos)
    ):

        diferenca = (
            fechamentos[i]
            - fechamentos[i - 1]
        )

        if diferenca > 0:

            ganhos.append(
                diferenca
            )

            perdas.append(0)

        else:

            ganhos.append(0)

            perdas.append(
                abs(diferenca)
            )

    ganhos_iniciais = ganhos[
        :periodo
    ]

    perdas_iniciais = perdas[
        :periodo
    ]

    media_ganho = (
        sum(ganhos_iniciais)
        / periodo
    )

    media_perda = (
        sum(perdas_iniciais)
        / periodo
    )

    for i in range(
        periodo,
        len(ganhos)
    ):

        media_ganho = (
            (
                media_ganho
                * (periodo - 1)
            )
            + ganhos[i]
        ) / periodo

        media_perda = (
            (
                media_perda
                * (periodo - 1)
            )
            + perdas[i]
        ) / periodo

    if media_perda == 0:

        return 100.0

    rs = (
        media_ganho
        / media_perda
    )

    rsi = 100 - (
        100 / (1 + rs)
    )

    return round(
        rsi,
        2
    )


# ============================================================
# PIVÔ
# ============================================================

def calcular_pivo(candles):

    if len(candles) < 2:

        return None

    anterior = candles[-2]

    pivo = (
        anterior["high"]
        + anterior["low"]
        + anterior["close"]
    ) / 3

    return round(
        pivo,
        6
    )


# ============================================================
# TENDÊNCIA
# ============================================================

def analisar_tendencia(
    preco,
    ema21,
    ema50
):

    if (
        ema21 is None
        or ema50 is None
    ):

        return "NEUTRA"

    if (
        preco > ema21
        and ema21 > ema50
    ):

        return "ALTA"

    if (
        preco < ema21
        and ema21 < ema50
    ):

        return "BAIXA"

    return "NEUTRA"


# ============================================================
# ROMPIMENTO
# ============================================================

def detectar_rompimento(
    candles,
    preco
):

    if len(candles) < 6:

        return None

    anteriores = candles[-6:-1]

    resistencia = max(
        c["high"]
        for c in anteriores
    )

    suporte = min(
        c["low"]
        for c in anteriores
    )

    if preco > resistencia:

        return "ALTA"

    if preco < suporte:

        return "BAIXA"

    return None


# ============================================================
# PULLBACK
# ============================================================

def detectar_pullback(
    candles,
    ema21,
    tendencia
):

    if len(candles) < 4:

        return False

    if ema21 is None:

        return False

    atual = candles[-1]

    anterior = candles[-2]

    if tendencia == "ALTA":

        tocou_media = (
            anterior["low"]
            <= ema21
        )

        voltou_acima = (
            atual["close"]
            > ema21
        )

        return (
            tocou_media
            and voltou_acima
        )

    if tendencia == "BAIXA":

        tocou_media = (
            anterior["high"]
            >= ema21
        )

        voltou_abaixo = (
            atual["close"]
            < ema21
        )

        return (
            tocou_media
            and voltou_abaixo
        )

    return False


# ============================================================
# SINAL
# ============================================================

def gerar_sinal(candles):

    if len(candles) < 50:

        return {
            "sinal": "AGUARDANDO",
            "status": "POUCOS DADOS",
            "confianca": 0
        }

    fechamentos = [
        c["close"]
        for c in candles
    ]

    preco = fechamentos[-1]

    ema21 = calcular_ema(
        fechamentos,
        21
    )

    ema50 = calcular_ema(
        fechamentos,
        50
    )

    rsi = calcular_rsi(
        fechamentos,
        14
    )

    pivo = calcular_pivo(
        candles
    )

    tendencia = analisar_tendencia(
        preco,
        ema21,
        ema50
    )

    rompimento = detectar_rompimento(
        candles,
        preco
    )

    pullback = detectar_pullback(
        candles,
        ema21,
        tendencia
    )

    # --------------------------------------------------------
    # Pontuação
    # --------------------------------------------------------

    pontos_call = 0
    pontos_put = 0

    # Tendência
    if tendencia == "ALTA":

        pontos_call += 2

    elif tendencia == "BAIXA":

        pontos_put += 2

    # RSI
    if rsi is not None:

        if (
            tendencia == "ALTA"
            and rsi >= 50
            and rsi <= 70
        ):

            pontos_call += 1

        if (
            tendencia == "BAIXA"
            and rsi <= 50
            and rsi >= 30
        ):

            pontos_put += 1

    # Rompimento
    if rompimento == "ALTA":

        pontos_call += 2

    elif rompimento == "BAIXA":

        pontos_put += 2

    # Pullback
    if pullback:

        if tendencia == "ALTA":

            pontos_call += 2

        elif tendencia == "BAIXA":

            pontos_put += 2

    # --------------------------------------------------------
    # Decisão
    # --------------------------------------------------------

    sinal = "AGUARDANDO"

    status = "SEM CONFIRMAÇÃO"

    confianca = 0

    if pontos_call >= 5:

        sinal = "CALL"

        status = (
            "TENDÊNCIA DE ALTA + "
            "CONFIRMAÇÃO"
        )

        confianca = min(
            95,
            60 + pontos_call * 5
        )

    elif pontos_put >= 5:

        sinal = "PUT"

        status = (
            "TENDÊNCIA DE BAIXA + "
            "CONFIRMAÇÃO"
        )

        confianca = min(
            95,
            60 + pontos_put * 5
        )

    return {

        "sinal": sinal,

        "status": status,

        "confianca": confianca,

        "preco": round(
            preco,
            6
        ),

        "rsi": rsi,

        "ema21": (
            round(ema21, 6)
            if ema21 is not None
            else None
        ),

        "ema50": (
            round(ema50, 6)
            if ema50 is not None
            else None
        ),

        "mm": (
            round(ema21, 6)
            if ema21 is not None
            else None
        ),

        "pivo": pivo,

        "tendencia": tendencia,

        "rompimento": (
            rompimento
            if rompimento
            else "NÃO"
        ),

        "pullback": (
            "SIM"
            if pullback
            else "NÃO"
        ),

        "pontos_call": pontos_call,

        "pontos_put": pontos_put,
    }


# ============================================================
# API PRINCIPAL
# ============================================================

@app.get("/")
def inicio():

    return jsonify({

        "ok": True,

        "servico":
            "Academy Trading - IQ Option Candles",

        "somente_dados": True,

        "operacao": False,

        "timeframe": "M1",

        "estrategia":
            "MHI + RSI + EMA21/50 + "
            "Rompimento + Pullback + Tendência",

        "conexao":
            "persistente",
    })


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    conectado = False

    global _iq

    if _iq is not None:

        try:

            conectado = bool(
                _iq.check_connect()
            )

        except Exception:

            conectado = False

    return jsonify({

        "ok": True,

        "servico":
            "iq-option-candles",

        "iq_conectada":
            conectado,

        "timestamp":
            int(time.time()),

    })


# ============================================================
# CANDLES DE UM PAR
# ============================================================

@app.get("/candles/<par>")
def candles_par(par):

    inicio = time.time()

    par = par.strip().upper()

    try:

        iq = conectar()

        candles = buscar_candles(
            iq,
            par,
            CANDLE_COUNT
        )

        analise = gerar_sinal(
            candles
        )

        agora = datetime.now()

        return jsonify({

            "ok": True,

            "fonte":
                "IQ Option",

            "servico":
                "Academy Trading",

            "somente_dados":
                True,

            "operacao":
                False,

            "par":
                par,

            "timeframe":
                "M1",

            "hora":
                agora.strftime(
                    "%H:%M"
                ),

            "tempo_resposta":
                round(
                    time.time()
                    - inicio,
                    2
                ),

            "quantidade":
                len(candles),

            "timestamp":
                int(time.time()),

            **analise,

            "candles":
                candles,

        })

    except Exception as erro:

        return jsonify({

            "ok": False,

            "fonte":
                "IQ Option",

            "servico":
                "Academy Trading",

            "somente_dados":
                True,

            "operacao":
                False,

            "par":
                par,

            "erro":
                str(erro),

            "tipo_erro":
                type(erro).__name__,

            "etapa":
                "buscar candles/analisar",

        }), 503


# ============================================================
# CANDLES MÚLTIPLOS
# ============================================================

@app.get("/candles")
def candles():

    try:

        iq = conectar()

        pares_param = request.args.get(
            "pares",
            ""
        ).strip()

        if pares_param:

            pares = [
                p.strip().upper()
                for p in
                pares_param.split(",")
                if p.strip()
            ]

        else:

            pares = PARES

        # Para o Render Free:
        # não buscar 20 pares de uma vez.

        pares = pares[:5]

        resultados = []

        for par in pares:

            inicio = time.time()

            try:

                dados = buscar_candles(
                    iq,
                    par,
                    CANDLE_COUNT
                )

                analise = gerar_sinal(
                    dados
                )

                resultados.append({

                    "par":
                        par,

                    "timeframe":
                        "M1",

                    "hora":
                        datetime.now().strftime(
                            "%H:%M"
                        ),

                    "candles":
                        dados,

                    "quantidade":
                        len(dados),

                    "tempo_resposta":
                        round(
                            time.time()
                            - inicio,
                            2
                        ),

                    **analise,

                })

            except Exception as erro:

                resultados.append({

                    "par":
                        par,

                    "timeframe":
                        "M1",

                    "candles":
                        [],

                    "quantidade":
                        0,

                    "sinal":
                        "AGUARDANDO",

                    "status":
                        "ERRO AO BUSCAR",

                    "erro":
                        str(erro),

                })

        return jsonify({

            "ok": True,

            "fonte":
                "IQ Option",

            "servico":
                "Academy Trading",

            "somente_dados":
                True,

            "operacao":
                False,

            "timeframe":
                "M1",

            "estrategia":
                "MHI + RSI + EMA21/50 + "
                "Rompimento + Pullback + Tendência",

            "resultados":
                resultados,

            "timestamp":
                int(time.time()),

        })

    except Exception as erro:

        return jsonify({

            "ok": False,

            "fonte":
                "IQ Option",

            "servico":
                "Academy Trading",

            "somente_dados":
                True,

            "operacao":
                False,

            "erro":
                str(erro),

            "tipo_erro":
                type(erro).__name__,

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
