import os
import time
import threading
import concurrent.futures
from datetime import datetime, timezone, timedelta

# Brasil não tem mais horário de verão desde 2019,
# então um offset fixo de UTC-3 é sempre correto
# (evita depender de tzdata instalado no servidor do Render).
FUSO_BR = timezone(timedelta(hours=-3))

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
]

# Forex "normal" (mercado aberto, sem ser OTC).
# Só retorna dado quando o mercado real está aberto
# (dias úteis, fora do horário OTC-only).
PARES_FOREX = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "EURJPY",
    "AUDUSD",
    "USDCAD",
    "GBPJPY",
    "EURGBP",
    "USDCHF",
    "AUDJPY",
    "NZDUSD",
    "EURCAD",
    "GBPAUD",
    "CADJPY",
    "EURAUD",
]

ESTRATEGIA = (
    "MHI + RSI + EMA21/50 + "
    "Rompimento + Pullback + Tendência"
)

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
# CONECTAR
# ============================================================

def conectar():

    global _iq
    global _ultima_conexao

    email, password = obter_credenciais()

    with _lock:

        # Tenta reutilizar a conexão existente
        if _iq is not None:

            try:

                if _iq.check_connect():

                    return _iq

            except Exception:

                _iq = None

        # Nova conexão
        cliente = IQ_Option(
            email,
            password
        )

        inicio = time.time()

        conectado, motivo = cliente.connect()

        duracao = round(
            time.time() - inicio,
            2
        )

        if not conectado:

            _iq = None

            raise RuntimeError(
                "Não foi possível conectar à IQ Option "
                f"após {duracao}s: {motivo}"
            )

        _iq = cliente

        _ultima_conexao = int(
            time.time()
        )

        return _iq


# ============================================================
# NORMALIZAR CANDLE
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

    try:

        timestamp = (
            iq.get_server_timestamp()
        )

    except Exception:

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

    resultado = []

    for candle in candles:

        try:

            resultado.append(
                normalizar_candle(
                    candle
                )
            )

        except Exception:

            continue

    resultado.sort(
        key=lambda item: item["from"]
    )

    return resultado


# ============================================================
# BUSCAR CANDLES COM TIMEOUT REAL (por thread)
# ============================================================
#
# Alguns pares podem fazer a biblioteca iqoptionapi travar
# internamente (ex: tentando reconectar sem sucesso). Isso pode
# derrubar o worker inteiro do Render, mesmo com try/except,
# porque o timeout do gunicorn mata o processo via sinal
# (SystemExit), que não é capturado por "except Exception".
#
# Rodando cada busca numa thread separada com timeout real,
# se um par travar, a gente simplesmente ABANDONA aquela thread
# (ela continua rodando sozinha em segundo plano, mas o pedido
# HTTP não fica preso esperando) e marca esse par como falho.

_executor_candles = concurrent.futures.ThreadPoolExecutor(
    max_workers=3
)

def buscar_candles_com_timeout(
    iq,
    par,
    quantidade=CANDLE_COUNT,
    timeout_segundos=15
):

    future = _executor_candles.submit(
        buscar_candles,
        iq,
        par,
        quantidade
    )

    try:

        return future.result(
            timeout=timeout_segundos
        )

    except concurrent.futures.TimeoutError:

        raise TimeoutError(
            f"Busca de candles para {par} "
            f"demorou mais de {timeout_segundos}s "
            "e foi abandonada."
        )


# ============================================================
# EMA
# ============================================================

def calcular_ema(
    valores,
    periodo
):

    if len(valores) < periodo:

        return None

    ema = sum(
        valores[:periodo]
    ) / periodo

    multiplicador = (
        2 / (periodo + 1)
    )

    for valor in valores[periodo:]:

        ema = (
            (valor - ema)
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
    valores,
    periodo=14
):

    if len(valores) < periodo + 1:

        return None

    ganhos = []
    perdas = []

    for i in range(
        1,
        len(valores)
    ):

        diferenca = (
            valores[i]
            - valores[i - 1]
        )

        if diferenca > 0:

            ganhos.append(
                diferenca
            )

            perdas.append(0)

        elif diferenca < 0:

            ganhos.append(0)

            perdas.append(
                abs(diferenca)
            )

        else:

            ganhos.append(0)
            perdas.append(0)

    ganho_medio = (
        sum(ganhos[:periodo])
        / periodo
    )

    perda_media = (
        sum(perdas[:periodo])
        / periodo
    )

    for i in range(
        periodo,
        len(ganhos)
    ):

        ganho_medio = (
            (
                ganho_medio
                * (periodo - 1)
            )
            + ganhos[i]
        ) / periodo

        perda_media = (
            (
                perda_media
                * (periodo - 1)
            )
            + perdas[i]
        ) / periodo

    if perda_media == 0:

        if ganho_medio == 0:

            return 50.0

        return 100.0

    rs = (
        ganho_medio
        / perda_media
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

    candle = candles[-2]

    pivo = (
        candle["high"]
        + candle["low"]
        + candle["close"]
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

def analisar_rompimento(
    candles,
    preco
):

    if len(candles) < 6:

        return {
            "direcao": "NEUTRO",
            "rompimento": False,
            "resistencia": None,
            "suporte": None,
        }

    anteriores = candles[-6:-1]

    resistencia = max(
        candle["high"]
        for candle in anteriores
    )

    suporte = min(
        candle["low"]
        for candle in anteriores
    )

    if preco > resistencia:

        return {
            "direcao": "ALTA",
            "rompimento": True,
            "resistencia": round(
                resistencia,
                6
            ),
            "suporte": round(
                suporte,
                6
            ),
        }

    if preco < suporte:

        return {
            "direcao": "BAIXA",
            "rompimento": True,
            "resistencia": round(
                resistencia,
                6
            ),
            "suporte": round(
                suporte,
                6
            ),
        }

    return {
        "direcao": "NEUTRO",
        "rompimento": False,
        "resistencia": round(
            resistencia,
            6
        ),
        "suporte": round(
            suporte,
            6
        ),
    }


# ============================================================
# PULLBACK
# ============================================================

def analisar_pullback(
    candles,
    ema21,
    tendencia
):

    if (
        ema21 is None
        or len(candles) < 3
    ):

        return {
            "direcao": "NEUTRO",
            "pullback": False,
        }

    anterior = candles[-2]
    atual = candles[-1]

    tolerancia = abs(
        ema21
    ) * 0.0005

    if tendencia == "ALTA":

        tocou_media = (
            anterior["low"]
            <= ema21 + tolerancia
        )

        voltou_acima = (
            atual["close"]
            > ema21
        )

        if (
            tocou_media
            and voltou_acima
        ):

            return {
                "direcao": "ALTA",
                "pullback": True,
            }

    if tendencia == "BAIXA":

        tocou_media = (
            anterior["high"]
            >= ema21 - tolerancia
        )

        voltou_abaixo = (
            atual["close"]
            < ema21
        )

        if (
            tocou_media
            and voltou_abaixo
        ):

            return {
                "direcao": "BAIXA",
                "pullback": True,
            }

    return {
        "direcao": "NEUTRO",
        "pullback": False,
    }


# ============================================================
# MHI
# ============================================================

def analisar_mhi(candles):

    if len(candles) < 5:

        return {
            "direcao": "NEUTRO",
            "altas": 0,
            "baixas": 0,
        }

    ultimas = candles[-5:]

    altas = 0
    baixas = 0

    for candle in ultimas:

        if candle["close"] > candle["open"]:

            altas += 1

        elif candle["close"] < candle["open"]:

            baixas += 1

    # MHI contrarian
    if baixas > altas:

        direcao = "CALL"

    elif altas > baixas:

        direcao = "PUT"

    else:

        direcao = "NEUTRO"

    return {
        "direcao": direcao,
        "altas": altas,
        "baixas": baixas,
    }


# ============================================================
# ANÁLISE COMPLETA
# ============================================================

def analisar_sinal(candles):

    if len(candles) < 50:

        return {
            "sinal": "AGUARDANDO",
            "status": "POUCOS DADOS",
            "confianca": 0,
        }

    fechamentos = [
        candle["close"]
        for candle in candles
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

    tendencia = analisar_tendencia(
        preco,
        ema21,
        ema50
    )

    rompimento = analisar_rompimento(
        candles,
        preco
    )

    pullback = analisar_pullback(
        candles,
        ema21,
        tendencia
    )

    mhi = analisar_mhi(
        candles
    )

    pivo = calcular_pivo(
        candles
    )

    pontos_call = 0
    pontos_put = 0

    # --------------------------------------------------------
    # TENDÊNCIA
    # --------------------------------------------------------

    if tendencia == "ALTA":

        pontos_call += 2

    elif tendencia == "BAIXA":

        pontos_put += 2

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if rsi is not None:

        if (
            tendencia == "ALTA"
            and 50 <= rsi <= 70
        ):

            pontos_call += 1

        elif (
            tendencia == "BAIXA"
            and 30 <= rsi <= 50
        ):

            pontos_put += 1

    # --------------------------------------------------------
    # ROMPIMENTO
    # --------------------------------------------------------

    if (
        rompimento["rompimento"]
        and rompimento["direcao"] == "ALTA"
    ):

        pontos_call += 2

    elif (
        rompimento["rompimento"]
        and rompimento["direcao"] == "BAIXA"
    ):

        pontos_put += 2

    # --------------------------------------------------------
    # PULLBACK
    # --------------------------------------------------------

    if pullback["pullback"]:

        if (
            pullback["direcao"]
            == "ALTA"
        ):

            pontos_call += 2

        elif (
            pullback["direcao"]
            == "BAIXA"
        ):

            pontos_put += 2

    # --------------------------------------------------------
    # MHI
    # --------------------------------------------------------

    if mhi["direcao"] == "CALL":

        pontos_call += 1

    elif mhi["direcao"] == "PUT":

        pontos_put += 1

    # --------------------------------------------------------
    # SINAL
    # --------------------------------------------------------

    sinal = "AGUARDANDO"

    status = "SEM CONFIRMAÇÃO"

    confianca = 0

    # Pontuação mínima para confirmar CALL/PUT.
    # Antes era 5 (mais raro, mais seletivo).
    # Agora 4 (sinais mais frequentes, um pouco menos filtrados).
    PONTUACAO_MINIMA = 4

    if (
        pontos_call >= PONTUACAO_MINIMA
        and pontos_call > pontos_put
    ):

        sinal = "CALL"

        status = (
            "CONFIRMAÇÃO DE ALTA"
        )

        confianca = min(
            95,
            60 + pontos_call * 5
        )

    elif (
        pontos_put >= PONTUACAO_MINIMA
        and pontos_put > pontos_call
    ):

        sinal = "PUT"

        status = (
            "CONFIRMAÇÃO DE BAIXA"
        )

        confianca = min(
            95,
            60 + pontos_put * 5
        )

    # --------------------------------------------------------
    # MM
    # --------------------------------------------------------

    if (
        ema21 is not None
        and ema50 is not None
    ):

        if ema21 > ema50:

            mm = "EMA21 > EMA50"

        elif ema21 < ema50:

            mm = "EMA21 < EMA50"

        else:

            mm = "NEUTRA"

    else:

        mm = "--"

    # --------------------------------------------------------
    # PIVÔ / EVENTO
    # --------------------------------------------------------

    if rompimento["rompimento"]:

        pivo_status = (
            "ROMPIMENTO "
            + rompimento["direcao"]
        )

    elif pullback["pullback"]:

        pivo_status = (
            "PULLBACK "
            + pullback["direcao"]
        )

    else:

        pivo_status = pivo

    # --------------------------------------------------------
    # HORA
    # --------------------------------------------------------

    if candles:

        hora = datetime.fromtimestamp(
            candles[-1]["from"],
            tz=FUSO_BR,
        ).strftime(
            "%H:%M"
        )

        # timestamp UNIX (UTC) de fechamento do candle atual,
        # usado pelo front-end para montar o timer regressivo
        expira_em = candles[-1]["to"]

        # Horário sugerido de ENTRADA.
        # +120 = dois candles à frente do início do analisado.
        # Isso dá 1 minuto INTEIRO de preparação para a pessoa
        # abrir o par na corretora antes de operar. O painel
        # mostra "PREPARE" nesse minuto e "ENTRAR AGORA" depois.
        entrada = datetime.fromtimestamp(
            candles[-1]["from"] + 120,
            tz=FUSO_BR,
        ).strftime(
            "%H:%M"
        )

    else:

        hora = "--:--"

        expira_em = None

        entrada = "--:--"

    # --------------------------------------------------------
    # RESULTADO
    # --------------------------------------------------------

    return {

        "sinal": sinal,

        "status": status,

        "confianca": confianca,

        "hora": hora,

        "expira_em": expira_em,

        "entrada": entrada,

        "preco": round(
            preco,
            6
        ),

        "rsi": rsi,

        "mm": mm,

        "pivo": pivo_status,

        "ema21": ema21,

        "ema50": ema50,

        "tendencia": tendencia,

        "rompimento": (
            rompimento["direcao"]
            if rompimento["rompimento"]
            else "NÃO"
        ),

        "pullback": (
            "SIM"
            if pullback["pullback"]
            else "NÃO"
        ),

        "mhi": mhi,

        "pontos_call":
            pontos_call,

        "pontos_put":
            pontos_put,

        "validade":
            "1 minuto",

        "estrategia":
            ESTRATEGIA,

        "aviso":
            "Análise educacional. "
            "Não garante resultado.",

    }


# ============================================================
# ABRIR POSIÇÃO (CALL / PUT)
# ============================================================

def abrir_posicao(iq, par, direcao, valor, duracao=1):
    """
    Abre uma posição de CALL ou PUT.

    Tenta primeiro a opção binária/turbo clássica (iq.buy).
    Muitos pares -OTC não aceitam mais binária, então cai
    automaticamente para a opção digital (iq.buy_digital_spot),
    que é o que a maioria dos pares OTC usa hoje em dia.
    """

    direcao = direcao.strip().lower()

    if direcao not in ("call", "put"):
        raise ValueError(
            "Direção inválida. Use 'call' ou 'put'."
        )

    # ---- Tentativa 1: binária / turbo ----
    try:

        status, order_id = iq.buy(
            valor,
            par,
            direcao,
            duracao,
        )

        if status:

            return {
                "tipo": "binary",
                "id": order_id,
            }

    except Exception:

        pass

    # ---- Tentativa 2: opção digital ----
    status, order_id = iq.buy_digital_spot(
        par,
        valor,
        direcao,
        duracao,
    )

    if not status:

        raise RuntimeError(
            "A corretora recusou a ordem "
            f"(binária e digital falharam) para {par}. "
            f"Retorno: {order_id}"
        )

    return {
        "tipo": "digital",
        "id": order_id,
    }


def verificar_resultado(iq, tipo, order_id, timeout=180):
    """
    Fica escutando o resultado da ordem até ela fechar
    ou até estourar o timeout (em segundos).
    """

    inicio = time.time()

    while time.time() - inicio < timeout:

        try:

            if tipo == "digital":

                finalizada, lucro = iq.check_win_digital_v2(
                    order_id
                )

            else:

                lucro = iq.check_win_v4(order_id)

                finalizada = lucro is not None

            if finalizada:

                return {
                    "finalizado": True,
                    "lucro": lucro,
                }

        except Exception:

            pass

        time.sleep(1)

    return {
        "finalizado": False,
        "lucro": None,
        "aviso": "Tempo de espera esgotado, "
        "consulte o resultado depois pelo painel da IQ Option.",
    }


# ============================================================
# HOME
# ============================================================

@app.get("/")
def inicio():

    return jsonify({

        "ok": True,

        "servico":
            "Academy Trading - IQ Option Candles",

        "somente_dados":
            True,

        "operacao":
            False,

        "timeframe":
            "M1",

        "validade_teste":
            "1 minuto",

        "estrategia":
            ESTRATEGIA,

        "conexao":
            "persistente",

    })


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    global _iq

    conectado = False

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
# UM PAR
# ============================================================

@app.get("/candles/<par>")
def candles_par(par):

    inicio = time.time()

    par = par.strip().upper()

    try:

        iq = conectar()

        candles = buscar_candles_com_timeout(
            iq,
            par,
            CANDLE_COUNT
        )

        if not candles:

            raise RuntimeError(
                "A IQ Option não retornou candles."
            )

        analise = analisar_sinal(
            candles
        )

        tempo = round(
            time.time() - inicio,
            2
        )

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

            "quantidade":
                len(candles),

            "tempo_resposta":
                tempo,

            "timestamp":
                int(time.time()),

            "sinal":
                analise["sinal"],

            "status":
                analise["status"],

            "confianca":
                analise["confianca"],

            "hora":
                analise["hora"],

            "expira_em":
                analise["expira_em"],

            "entrada":
                analise["entrada"],

            "preco":
                analise["preco"],

            "rsi":
                analise["rsi"],

            "mm":
                analise["mm"],

            "pivo":
                analise["pivo"],

            "ema21":
                analise["ema21"],

            "ema50":
                analise["ema50"],

            "tendencia":
                analise["tendencia"],

            "rompimento":
                analise["rompimento"],

            "pullback":
                analise["pullback"],

            "mhi":
                analise["mhi"],

            "pontos_call":
                analise["pontos_call"],

            "pontos_put":
                analise["pontos_put"],

            "validade":
                "1 minuto",

            "estrategia":
                ESTRATEGIA,

            "candles":
                candles,

            "resultados":
                [analise],

        })

    except Exception as erro:

        # Se a conexão caiu,
        # força nova conexão na próxima chamada.

        global _iq

        _iq = None

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
                "conexão/candles/análise",

        }), 503


# ============================================================
# OPERAR (CALL / PUT) — EXECUÇÃO REAL DE ORDEM
# ============================================================

@app.get("/operar/<par>")
def operar(par):

    par = par.strip().upper()

    direcao = request.args.get(
        "direcao",
        ""
    ).strip().lower()

    if direcao not in ("call", "put"):

        return jsonify({

            "ok": False,

            "erro":
                "Parâmetro 'direcao' obrigatório: "
                "use ?direcao=call ou ?direcao=put",

        }), 400

    try:

        valor = float(
            request.args.get("valor", "1")
        )

    except ValueError:

        valor = 1.0

    try:

        duracao = int(
            request.args.get("duracao", "1")
        )

    except ValueError:

        duracao = 1

    try:

        iq = conectar()

        posicao = abrir_posicao(
            iq,
            par,
            direcao,
            valor,
            duracao,
        )

        resultado = verificar_resultado(
            iq,
            posicao["tipo"],
            posicao["id"],
        )

        return jsonify({

            "ok": True,

            "fonte": "IQ Option",

            "operacao": True,

            "par": par,

            "direcao": direcao,

            "valor": valor,

            "duracao_min": duracao,

            "tipo_operacao": posicao["tipo"],

            "id_ordem": posicao["id"],

            "timestamp": int(time.time()),

            **resultado,

        })

    except Exception as erro:

        global _iq

        _iq = None

        return jsonify({

            "ok": False,

            "fonte": "IQ Option",

            "operacao": True,

            "par": par,

            "erro": str(erro),

            "tipo_erro": type(erro).__name__,

            "etapa": "abrindo ordem",

        }), 503


# ============================================================
# MÚLTIPLOS PARES
# ============================================================

@app.get("/candles")
def candles():

    try:

        iq = conectar()

        pares_param = request.args.get(
            "pares",
            ""
        ).strip()

        # Qual mercado usar quando NÃO vier ?pares= explícito.
        # "otc" (padrão) = pares sintéticos, roda sempre.
        # "forex" = mercado real, só funciona em horário de
        # mercado aberto (dias úteis).
        mercado = request.args.get(
            "mercado",
            "otc"
        ).strip().lower()

        lista_base = (
            PARES_FOREX
            if mercado == "forex"
            else PARES
        )

        if pares_param:

            pares = [
                p.strip().upper()
                for p in
                pares_param.split(",")
                if p.strip()
            ]

        else:

            # Sem par específico: alterna automaticamente
            # qual grupo de pares mostrar, pra nunca buscar
            # mais que TAMANHO_GRUPO de uma vez só (mais rápido
            # e seguro), cobrindo todos os pares ao longo de
            # algumas atualizações.

            TAMANHO_GRUPO = 5

            # Muda de grupo a cada 2 minutos, batendo com o
            # intervalo de atualização do painel. Assim os cards
            # não ficam trocando de par no meio do contador.
            indice_rotativo = int(
                time.time() // 120
            ) % len(lista_base)

            pares = [
                lista_base[(indice_rotativo + i) % len(lista_base)]
                for i in range(
                    min(TAMANHO_GRUPO, len(lista_base))
                )
            ]

        # Segurança extra: nunca busca mais que 5 pares
        # numa chamada só, mesmo se pedirem explicitamente
        # mais que isso via ?pares=.

        pares = pares[:5]

        resultados = []

        for par in pares:

            try:

                dados = buscar_candles_com_timeout(
                    iq,
                    par,
                    CANDLE_COUNT,
                    timeout_segundos=15
                )

                analise = analisar_sinal(
                    dados
                )

                resultados.append({

                    "par":
                        par,

                    "timeframe":
                        "M1",

                    "candles":
                        dados,

                    "quantidade":
                        len(dados),

                    "sinal":
                        analise["sinal"],

                    "status":
                        analise["status"],

                    "confianca":
                        analise["confianca"],

                    "hora":
                        analise["hora"],

                    "expira_em":
                        analise["expira_em"],

                    "entrada":
                        analise["entrada"],

                    "rsi":
                        analise["rsi"],

                    "mm":
                        analise["mm"],

                    "pivo":
                        analise["pivo"],

                    "preco":
                        analise["preco"],

                    "ema21":
                        analise["ema21"],

                    "ema50":
                        analise["ema50"],

                    "tendencia":
                        analise["tendencia"],

                    "rompimento":
                        analise["rompimento"],

                    "pullback":
                        analise["pullback"],

                    "pontos_call":
                        analise["pontos_call"],

                    "pontos_put":
                        analise["pontos_put"],

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
                        "ERRO",

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
                ESTRATEGIA,

            "resultados":
                resultados,

            "timestamp":
                int(time.time()),

        })

    except Exception as erro:

        global _iq

        _iq = None

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
