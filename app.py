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

# OTC (funciona 24h, inclusive fim de semana).
# Espelha a lista do Forex normal, com o sufixo -OTC.
# Se algum par não existir na corretora, ele simplesmente
# falha na busca e aparece como erro no card — os outros
# continuam funcionando normalmente.
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

# AÇÕES.
# ATENÇÃO: os nomes abaixo são um PALPITE inicial. Os códigos
# reais da corretora podem ser diferentes (com ou sem -OTC).
# Use a rota /ativos para ver a lista exata do que está aberto
# e ajuste esta lista com os nomes que aparecerem lá.
# AÇÕES (pregão da bolsa, dias úteis).
#
# Confirmados por teste direto em /candles/<nome>:
#   APPLE, FACEBOOK, TESLA  -> responderam ok:true
#
# Os demais são o mesmo padrão de nome (sem sufixo) e ainda
# precisam ser confirmados. Se algum der ERRO na segunda-feira,
# basta apagar a linha dele.
#
# IMPORTANTE: não existe versão -OTC para ações. Fora do
# pregão elas retornam MERCADO FECHADO, o que é o correto.
PARES_ACOES = [
    "APPLE",
    "FACEBOOK",
    "TESLA",
    "AMAZON",
    "GOOGLE",
    "MICROSOFT",
    "NETFLIX",
    "INTEL",
    "ALIBABA",
    "COCA-COLA",
    "MCDON",
    "VISA",
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

def invalidar_conexao():
    """Marca a conexão atual como inutilizável.

    check_connect() só olha se o socket está aberto. A sessão
    da IQ Option pode estar morta mesmo com o socket vivo — é
    aí que aparece o erro "get_candles need reconnect" em loop,
    porque conectar() continua devolvendo o mesmo cliente
    quebrado para sempre.

    Chamando isto após uma falha de busca, a próxima chamada é
    obrigada a abrir uma conexão nova.
    """

    global _iq

    with _lock:
        _iq = None


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

# Threads que travam na biblioteca da IQ Option são
# abandonadas, mas continuam ocupando uma vaga do pool. Com
# apenas 3 vagas, três travamentos deixavam o serviço inteiro
# sem conseguir buscar mais nada.
_executor_candles = concurrent.futures.ThreadPoolExecutor(
    max_workers=12
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
# FIBONACCI (retração dentro da tendência)
# ============================================================
#
# Ideia: numa tendência de ALTA, o preço não sobe em linha
# reta. Ele sobe, recua um pouco e sobe de novo. Fibonacci
# mede QUANTO ele recuou do último movimento.
#
#   0%     = topo do movimento
#   38,2%  \
#   50,0%   > zona boa para entrar a favor da tendência
#   61,8%  /
#   100%   = fundo do movimento
#
# Recuo pequeno demais (menos de 23,6%): ainda não recuou,
# entrar aqui é comprar no topo.
#
# Recuo grande demais (mais de 78,6%): não foi recuo, foi
# reversão. A tendência provavelmente acabou.
#
# A zona entre 38,2% e 61,8% é a clássica "zona de ouro".
# ============================================================

JANELA_FIBO = 30

FIBO_ZONA_INICIO = 0.382
FIBO_ZONA_FIM = 0.618
FIBO_LIMITE_REVERSAO = 0.786


def analisar_fibonacci(candles, tendencia):

    vazio = {
        "na_zona": False,
        "nivel": None,
        "texto": "SEM DADOS",
    }

    if not candles or len(candles) < JANELA_FIBO:
        return vazio

    if tendencia not in ("ALTA", "BAIXA"):

        return {
            "na_zona": False,
            "nivel": None,
            "texto": "SEM TENDÊNCIA",
        }

    janela = candles[-JANELA_FIBO:]

    topo = max(c["high"] for c in janela)
    fundo = min(c["low"] for c in janela)

    amplitude = topo - fundo

    if amplitude <= 0:
        return vazio

    preco = candles[-1]["close"]

    if tendencia == "ALTA":

        # Quanto o preço recuou a partir do topo.
        recuo = (topo - preco) / amplitude

    else:

        # Quanto o preço subiu a partir do fundo.
        recuo = (preco - fundo) / amplitude

    percentual = round(recuo * 100, 1)

    if recuo > FIBO_LIMITE_REVERSAO:

        return {
            "na_zona": False,
            "nivel": percentual,
            "texto": "RECUO FUNDO " + str(percentual) + "%",
        }

    if FIBO_ZONA_INICIO <= recuo <= FIBO_ZONA_FIM:

        return {
            "na_zona": True,
            "nivel": percentual,
            "texto": "ZONA OURO " + str(percentual) + "%",
        }

    return {
        "na_zona": False,
        "nivel": percentual,
        "texto": "FORA DA ZONA " + str(percentual) + "%",
    }


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

    # Os candles já chegam aqui sem a vela em formação
    # (descartada em analisar_sinal). Ainda assim, filtramos
    # de novo por segurança, caso a função seja chamada
    # isoladamente em outro ponto do código.
    fechados = descartar_vela_aberta(candles)

    if len(fechados) < 5:

        return {
            "direcao": "NEUTRO",
            "altas": 0,
            "baixas": 0,
        }

    ultimas = fechados[-5:]

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

def descartar_vela_aberta(candles):
    """Remove do fim da lista qualquer candle que ainda não
    fechou.

    A API da IQ Option devolve a vela EM FORMAÇÃO como último
    item. Se ela for usada, os indicadores leem um close que
    ainda vai mudar — na prática um valor quase igual ao open.
    Isso distorce EMA, RSI, MHI e Fibonacci de uma só vez.

    Regra: um candle só é considerado fechado quando
    from + TIMEFRAME <= agora.
    """

    if not candles:
        return []

    try:
        agora = int(time.time())
    except Exception:
        return candles

    fechados = [
        c for c in candles
        if c.get("from", 0) + TIMEFRAME <= agora
    ]

    return fechados


def analisar_sinal(candles):

    # SEMPRE descartar a vela aberta antes de qualquer cálculo.
    candles = descartar_vela_aberta(candles)

    if len(candles) < 50:

        # ATENÇÃO: este retorno precisa ter TODAS as chaves que
        # as rotas leem. Se faltar uma, o Flask levanta KeyError
        # e a requisição inteira devolve 500 — derrubando todos
        # os pares do grupo, não só este.
        return {
            "sinal": "AGUARDANDO",
            "status": "POUCOS DADOS",
            "confianca": 0,
            "hora": "--:--",
            "expira_em": None,
            "entrada_em": None,
            "entrada": "--:--",
            "preco": None,
            "rsi": None,
            "mm": "--",
            "pivo": "--",
            "ema21": None,
            "ema50": None,
            "tendencia": "NEUTRA",
            "rompimento": "NÃO",
            "pullback": "NÃO",
            "fibo": "SEM DADOS",
            "fibo_nivel": None,
            "mhi": {
                "direcao": "NEUTRO",
                "altas": 0,
                "baixas": 0,
            },
            "pontos_call": 0,
            "pontos_put": 0,
            "validade": "1 minuto",
        }

    # --------------------------------------------------------
    # DADO VELHO = MERCADO FECHADO
    # --------------------------------------------------------
    #
    # Quando a bolsa fecha, a corretora continua devolvendo os
    # candles do último pregão. Sem esta checagem o robô analisa
    # um candle de horas atrás como se fosse agora, gera um
    # sinal impossível de operar e ainda suja a estatística.
    #
    # Regra: o último candle fechado precisa ser recente. Em M1,
    # no máximo 3 minutos de atraso.

    ATRASO_MAXIMO = 180

    atraso = int(time.time()) - candles[-1]["to"]

    if atraso > ATRASO_MAXIMO:

        minutos = atraso // 60

        return {
            "sinal": "AGUARDANDO",
            "status": "MERCADO FECHADO",
            "confianca": 0,
            "hora": datetime.fromtimestamp(
                candles[-1]["from"],
                tz=FUSO_BR,
            ).strftime("%H:%M"),
            "expira_em": None,
            "entrada_em": None,
            "entrada": "--:--",
            "preco": round(candles[-1]["close"], 6),
            "rsi": None,
            "mm": "--",
            "pivo": "--",
            "ema21": None,
            "ema50": None,
            "tendencia": "NEUTRA",
            "rompimento": "NÃO",
            "pullback": "NÃO",
            "fibo": "SEM DADOS",
            "fibo_nivel": None,
            "mhi": {
                "direcao": "NEUTRO",
                "altas": 0,
                "baixas": 0,
            },
            "pontos_call": 0,
            "pontos_put": 0,
            "confirmacoes_call": [],
            "confirmacoes_put": [],
            "qualidade_sinal": "AGUARDANDO",
            "validade": "1 minuto",
            "estrategia": ESTRATEGIA,
            "aviso": (
                "Ultimo candle tem "
                + str(minutos)
                + " min de atraso. Mercado provavelmente fechado."
            ),
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

    fibo = analisar_fibonacci(
        candles,
        tendencia
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
    # FIBONACCI
    # --------------------------------------------------------
    #
    # Vale 2 pontos, e só pontua A FAVOR da tendência. Ele não
    # cria sinal sozinho: reforça a entrada quando o preço
    # recuou até a zona de ouro e está pronto para retomar.

    if fibo["na_zona"]:

        if tendencia == "ALTA":

            pontos_call += 2

        elif tendencia == "BAIXA":

            pontos_put += 2

    # --------------------------------------------------------
    # SINAL
    # --------------------------------------------------------

    sinal = "AGUARDANDO"

    status = "SEM CONFIRMAÇÃO"

    confianca = 0

    # Pontuação mínima para confirmar CALL/PUT (máximo é 8).
    # Histórico: era 4 (muito sinal fraco), subiu para 6 (quase
    # nenhum sinal aparecia), agora 5 como meio-termo.
    PONTUACAO_MINIMA = 5

    # O lado vencedor também precisa ganhar por esta margem.
    #
    # Histórico: 3 deixava passar os sinais "fracos" (diferença
    # de 3 a 4 pontos). Agora 5, para só aparecerem os sinais
    # classificados como MÉDIO (5-6) e FORTE (7+) no painel.
    #
    # Efeito: menos sinal na tela, todos com concordância
    # folgada entre os indicadores.
    DIFERENCA_MINIMA = 5

    if (
        pontos_call >= PONTUACAO_MINIMA
        and pontos_call - pontos_put >= DIFERENCA_MINIMA
    ):

        sinal = "CALL"

        status = (
            "CONFIRMAÇÃO DE ALTA"
        )

        # NÃO é probabilidade de acerto. É apenas a soma
        # técnica dos indicadores que concordaram.
        confianca = pontos_call

    elif (
        pontos_put >= PONTUACAO_MINIMA
        and pontos_put - pontos_call >= DIFERENCA_MINIMA
    ):

        sinal = "PUT"

        status = (
            "CONFIRMAÇÃO DE BAIXA"
        )

        # NÃO é probabilidade de acerto. É apenas a soma
        # técnica dos indicadores que concordaram.
        confianca = pontos_put

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

        # LINHA DO TEMPO OFICIAL (candle M1)
        #
        #   candles[-1] já é um candle FECHADO, porque a vela
        #   em formação foi descartada no início da função.
        #
        #   analisado : from        -> from + 60
        #   entrada   : from + 60   -> from + 120
        #
        #   O resultado WIN/LOSS é conferido usando EXATAMENTE
        #   o candle de entrada (from + 60), comparando o open
        #   com o close dele. Nunca o candle seguinte.

        # Fechamento do candle analisado. É também o instante
        # em que o candle de entrada ABRE.
        expira_em = candles[-1]["to"]

        # Início do candle de ENTRADA, em UNIX. É esta a chave
        # que o front-end usa para o contador e para conferir
        # o resultado.
        entrada_em = candles[-1]["from"] + TIMEFRAME

        entrada = datetime.fromtimestamp(
            entrada_em,
            tz=FUSO_BR,
        ).strftime(
            "%H:%M"
        )

    else:

        hora = "--:--"

        expira_em = None

        entrada_em = None

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

        "entrada_em": entrada_em,

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

        "fibo": fibo["texto"],

        "fibo_nivel": fibo["nivel"],

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
# LISTAR ATIVOS DISPONÍVEIS
# ============================================================
#
# Devolve os nomes EXATOS dos ativos que a corretora está
# oferecendo agora, já separados por tipo. Serve para descobrir
# como cada ação se chama de verdade (APPLE? APPLE-OTC?) sem
# precisar adivinhar par por par.
#
# Uso:  /ativos            -> tudo que está aberto
#       /ativos?filtro=app -> só os que contêm "app" no nome
# ============================================================

@app.get("/ativos")
def listar_ativos():

    filtro = (
        request.args.get("filtro", "")
        .strip()
        .upper()
    )

    try:

        iq = conectar()

        # get_all_open_time() pode ficar 30s parada dentro da
        # biblioteca esperando a lista de opções digitais. Isso
        # estoura o limite do gunicorn e MATA o worker, o que
        # derruba também o painel de forex que estava saudável.
        #
        # Rodando numa thread com timeout curto, se travar a
        # gente abandona a thread e responde normalmente.
        futuro = _executor_candles.submit(
            iq.get_all_open_time
        )

        todos = futuro.result(timeout=12)

    except concurrent.futures.TimeoutError:

        return jsonify({
            "ok": False,
            "etapa": "listagem",
            "erro": (
                "A corretora demorou mais de 12s para "
                "devolver a lista de ativos. Tente de novo."
            ),
        }), 200

    except Exception as erro:

        invalidar_conexao()

        return jsonify({
            "ok": False,
            "etapa": "conexao",
            "erro": str(erro)[:200],
        }), 200

    # A partir daqui, tudo dentro de try: a estrutura devolvida
    # pela biblioteca varia entre versões, e um formato
    # inesperado não pode virar Internal Server Error.

    try:

        abertos = {}
        total = 0

        if isinstance(todos, dict):

            for tipo, ativos in todos.items():

                if not isinstance(ativos, dict):
                    continue

                nomes = []

                for nome, info in ativos.items():

                    aberto = False

                    if isinstance(info, dict):
                        aberto = bool(info.get("open"))
                    else:
                        aberto = bool(info)

                    if not aberto:
                        continue

                    texto = str(nome)

                    if filtro and filtro not in texto.upper():
                        continue

                    nomes.append(texto)

                if nomes:
                    abertos[str(tipo)] = sorted(nomes)
                    total += len(nomes)

        return jsonify({
            "ok": True,
            "dica": (
                "Use estes nomes exatos nas listas "
                "PARES, PARES_FOREX, PARES_ACOES ou "
                "PARES_ACOES do app.py."
            ),
            "filtro": filtro or None,
            "total_abertos": total,
            "abertos": abertos,
        })

    except Exception as erro:

        return jsonify({
            "ok": False,
            "etapa": "leitura da lista",
            "erro": str(erro)[:200],
            "tipo_recebido": type(todos).__name__,
        }), 200


# ============================================================
# CONFERIR RESULTADO DE UM SINAL
# ============================================================
#
# Recebe o par e o timestamp de ABERTURA do candle de entrada.
# Procura esse candle no histórico e compara abertura x
# fechamento para dizer se um CALL ou um PUT teria acertado.
#
# Isso confere o SINAL, não a operação real da pessoa. Serve
# para medir a estratégia sem depender de anotação manual.
#
# Uso: /resultado/EURUSD?inicio=1755792060&sinal=PUT
# ============================================================

@app.get("/resultado/<par>")
def resultado_sinal(par):

    par = par.strip().upper()

    try:
        inicio_candle = int(
            request.args.get("inicio", 0)
        )
    except (TypeError, ValueError):
        inicio_candle = 0

    sinal = (
        request.args.get("sinal", "")
        .strip()
        .upper()
    )

    if inicio_candle <= 0 or sinal not in ("CALL", "PUT"):

        return jsonify({
            "erro": "parametros invalidos",
            "esperado": "?inicio=UNIX&sinal=CALL|PUT",
        }), 400

    # Espera o candle de entrada fechar. As durações maiores
    # (M2 e M3) voltam como null até que fechem também.
    if time.time() < inicio_candle + TIMEFRAME:

        return jsonify({
            "par": par,
            "status": "AGUARDANDO",
            "mensagem": "candle ainda nao fechou",
        })

    try:

        iq = conectar()

        # Timeout curto: esta rota é chamada em segundo plano
        # e não pode competir com a busca dos cards, que é o
        # que o usuário está esperando na tela.
        candles = buscar_candles_com_timeout(
            iq,
            par,
            CANDLE_COUNT,
            timeout_segundos=8
        )

    except Exception as erro:

        invalidar_conexao()

        # Devolve 200, não 502.
        #
        # O 502 fazia o PHP responder "Falha ao consultar o
        # servidor", sem status nenhum, e o painel repetia a
        # mesma consulta indefinidamente. Com 200 + status
        # INDISPONIVEL o front entende que houve falha
        # temporária e passa para o próximo pendente.
        return jsonify({
            "par": par,
            "status": "INDISPONIVEL",
            "mensagem": str(erro)[:120],
        }), 200

    por_inicio = {
        c.get("from"): c
        for c in candles
    }

    alvo = por_inicio.get(inicio_candle)

    if alvo is None:

        return jsonify({
            "par": par,
            "status": "NAO_ENCONTRADO",
            "mensagem": "candle fora do historico disponivel",
        })

    # A ABERTURA é sempre a do candle de entrada. O que muda
    # entre M1, M2 e M3 é qual FECHAMENTO usamos:
    #
    #   M1 -> fecha no próprio candle de entrada
    #   M2 -> fecha um candle depois
    #   M3 -> fecha dois candles depois
    #
    # Assim medimos as três durações da MESMA entrada, sem
    # precisar de sinais diferentes.

    abertura = alvo["open"]

    def avaliar(velas):

        ultimo = por_inicio.get(
            inicio_candle + (velas - 1) * TIMEFRAME
        )

        if ultimo is None:
            return None

        # Ainda não fechou.
        if time.time() < ultimo["from"] + TIMEFRAME:
            return None

        fechamento = ultimo["close"]

        if fechamento > abertura:
            direcao = "ALTA"
        elif fechamento < abertura:
            direcao = "BAIXA"
        else:
            direcao = "DOJI"

        if direcao == "DOJI":
            return "EMPATE"

        if sinal == "CALL" and direcao == "ALTA":
            return "WIN"

        if sinal == "PUT" and direcao == "BAIXA":
            return "WIN"

        return "LOSS"

    m1 = avaliar(1)
    m2 = avaliar(2)
    m3 = avaliar(3)

    return jsonify({
        "par": par,
        "sinal": sinal,
        "status": m1 or "AGUARDANDO",
        "m1": m1,
        "m2": m2,
        "m3": m3,
        "abertura": abertura,
        "inicio": inicio_candle,
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
                analise.get("sinal"),

            "status":
                analise.get("status"),

            "confianca":
                analise.get("confianca"),

            "hora":
                analise.get("hora"),

            "expira_em":
                analise.get("expira_em"),

            "entrada":
                analise.get("entrada"),

            "entrada_em":
                analise.get("entrada_em"),

            "preco":
                analise.get("preco"),

            "rsi":
                analise.get("rsi"),

            "mm":
                analise.get("mm"),

            "pivo":
                analise.get("pivo"),

            "ema21":
                analise.get("ema21"),

            "ema50":
                analise.get("ema50"),

            "tendencia":
                analise.get("tendencia"),

            "rompimento":
                analise.get("rompimento"),

            "pullback":
                analise.get("pullback"),

            "mhi":
                analise.get("mhi"),

            "fibo":
                analise.get("fibo"),

            "fibo_nivel":
                analise.get("fibo_nivel"),

            "pontos_call":
                analise.get("pontos_call"),

            "pontos_put":
                analise.get("pontos_put"),

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

        if mercado == "forex":
            lista_base = PARES_FOREX
        elif mercado == "acoes":
            lista_base = PARES_ACOES
        else:
            lista_base = PARES

        # Mercado sem nenhum ativo configurado. Precisa ser
        # verificado ANTES da rotação, porque ela faz
        # "% len(lista_base)" e dividir por zero derruba a rota.
        if not lista_base and not pares_param:

            return jsonify({
                "ok": True,
                "fonte": "IQ Option",
                "servico": "Academy Trading",
                "somente_dados": True,
                "operacao": False,
                "timeframe": "M1",
                "mercado": mercado,
                "aviso": (
                    "Nenhum ativo configurado para este "
                    "mercado. Use /ativos para descobrir os "
                    "nomes corretos."
                ),
                "resultados": [],
                "timestamp": int(time.time()),
            })

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

            # ROTAÇÃO DOS PARES: 120 segundos (2 minutos).
            # Isso é SEPARADO da busca de dados, que roda a
            # cada 120s no front-end. Com os dois em 120s,
            # cada grupo de pares recebe uma busca antes de
            # dar lugar ao próximo.
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

        # Lista de pares vazia (ex.: mercado de ações ainda não
        # configurado). Devolve resposta vazia em vez de tentar
        # buscar e derrubar a conexão.
        if not pares:

            return jsonify({
                "ok": True,
                "fonte": "IQ Option",
                "servico": "Academy Trading",
                "somente_dados": True,
                "operacao": False,
                "timeframe": "M1",
                "mercado": mercado,
                "aviso": (
                    "Nenhum ativo configurado para este "
                    "mercado. Use /ativos para descobrir os "
                    "nomes corretos."
                ),
                "resultados": [],
                "timestamp": int(time.time()),
            })

        resultados = []

        # ORÇAMENTO DE TEMPO
        #
        # O gunicorn mata o worker por volta dos 30 segundos.
        # Com 5 pares a 15s cada, a soma podia chegar a 75s e
        # a requisição inteira voltava como 502, derrubando
        # todos os pares — inclusive os que já tinham
        # respondido bem.
        #
        # Agora cada par tem 7s, e existe um teto total de 22s
        # para a chamada inteira. Quando o teto estoura, os
        # pares restantes voltam com status PULADO em vez de
        # arriscar o 502.

        ORCAMENTO_TOTAL = 22
        TIMEOUT_POR_PAR = 7

        inicio_lote = time.time()

        for par in pares:

            gasto = time.time() - inicio_lote
            restante = ORCAMENTO_TOTAL - gasto

            if restante < 3:

                resultados.append({
                    "par": par,
                    "timeframe": "M1",
                    "candles": [],
                    "quantidade": 0,
                    "sinal": "AGUARDANDO",
                    "status": "PULADO",
                    "erro": (
                        "Tempo da requisicao esgotado. "
                        "Este par entra na proxima atualizacao."
                    ),
                })

                continue

            try:

                dados = buscar_candles_com_timeout(
                    iq,
                    par,
                    CANDLE_COUNT,
                    timeout_segundos=min(
                        TIMEOUT_POR_PAR,
                        int(restante)
                    )
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
                        analise.get("sinal"),

                    "status":
                        analise.get("status"),

                    "confianca":
                        analise.get("confianca"),

                    "hora":
                        analise.get("hora"),

                    "expira_em":
                        analise.get("expira_em"),

                    "entrada":
                        analise.get("entrada"),

                    "entrada_em":
                        analise.get("entrada_em"),

                    "rsi":
                        analise.get("rsi"),

                    "mm":
                        analise.get("mm"),

                    "pivo":
                        analise.get("pivo"),

                    "preco":
                        analise.get("preco"),

                    "ema21":
                        analise.get("ema21"),

                    "ema50":
                        analise.get("ema50"),

                    "tendencia":
                        analise.get("tendencia"),

                    "rompimento":
                        analise.get("rompimento"),

                    "pullback":
                        analise.get("pullback"),

                    "fibo":
                        analise.get("fibo"),

                    "fibo_nivel":
                        analise.get("fibo_nivel"),

                    "pontos_call":
                        analise.get("pontos_call"),

                    "pontos_put":
                        analise.get("pontos_put"),

                })

            except Exception as erro:

                texto_erro = str(erro)

                # Sinais de sessão morta. Sem invalidar aqui, o
                # conectar() continua devolvendo o mesmo cliente
                # quebrado e o erro se repete para sempre.
                sintomas = (
                    "need reconnect",
                    "is_ssl",
                    "EOF occurred",
                    "not connected",
                    "Connection",
                )

                if any(
                    marca.lower() in texto_erro.lower()
                    for marca in sintomas
                ):

                    invalidar_conexao()

                    try:
                        iq = conectar()
                    except Exception:
                        iq = None

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
                        texto_erro,

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
