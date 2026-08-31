import os
import time
import threading
import concurrent.futures
import json
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Brasil não tem mais horário de verão desde 2019,
# então um offset fixo de UTC-3 é sempre correto
# (evita depender de tzdata instalado no servidor do Render).
FUSO_BR = ZoneInfo("America/Sao_Paulo")

from flask import Flask, jsonify, request
from iqoptionapi.stable_api import IQ_Option

app = Flask(__name__)

# ============================================================
# IMPORTANTE — START COMMAND NO RENDER
# ============================================================
#
# Os monitores do Telegram sobem junto com este arquivo. Se o
# gunicorn rodar com mais de 1 worker, cada worker cria o SEU
# monitor e as mensagens saem duplicadas no grupo (o controle
# de duplicata é em memória, não é compartilhado).
#
#   gunicorn app:app -w 1 --threads 4 --timeout 60
#
# ============================================================

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

# AÇÕES (pregão da bolsa, dias úteis).
#
# Confirmados por teste direto em /candles/<nome>:
#   APPLE, FACEBOOK, TESLA  -> responderam ok:true
#
# Os demais são o mesmo padrão de nome (sem sufixo) e ainda
# precisam ser confirmados. Se algum der ERRO, basta apagar
# a linha dele.
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
# APRENDIZADO DOS RESULTADOS - SOMENTE PARA SINAIS FUTUROS
# ============================================================
#
# O sistema NÃO altera resultados passados e NÃO promete WIN.
# Ele registra o contexto do sinal, espera o M1 fechar e usa o
# histórico para ajustar levemente a pontuação dos próximos sinais.
#
# O aprendizado é conservador:
# - exige uma amostra mínima;
# - usa taxa suavizada para evitar extremos com poucos dados;
# - limita o ajuste a +/- 2 pontos;
# - não faz martingale e não força CALL/PUT.
#
# Em Render, o disco local pode ser efêmero. O arquivo abaixo é um
# cache persistente enquanto a instância permanecer disponível.
# Se o serviço reiniciar, o sistema volta a aprender do zero.
# ============================================================

APRENDIZADO_ATIVO = True
APRENDIZADO_MINIMO = 8
APRENDIZADO_MAX_REGISTROS = 2000
APRENDIZADO_MAX_AJUSTE = 2

ARQUIVO_APRENDIZADO = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "historico_aprendizado.json",
)

ARQUIVO_PENDENTES = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "sinais_pendentes.json",
)

_lock_aprendizado = threading.Lock()


def _ler_json_seguro(caminho, padrao):
    try:
        if not os.path.isfile(caminho):
            return padrao.copy() if isinstance(padrao, dict) else list(padrao)
        with open(caminho, "r", encoding="utf-8") as arquivo:
            dados = json.load(arquivo)
        return dados if isinstance(dados, type(padrao)) else padrao.copy() if isinstance(padrao, dict) else list(padrao)
    except Exception:
        return padrao.copy() if isinstance(padrao, dict) else list(padrao)


def _salvar_json_seguro(caminho, dados):
    temporario = caminho + ".tmp"
    try:
        with open(temporario, "w", encoding="utf-8") as arquivo:
            json.dump(dados, arquivo, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporario, caminho)
        return True
    except Exception:
        try:
            if os.path.exists(temporario):
                os.remove(temporario)
        except Exception:
            pass
        return False


def _faixa_rsi(rsi):
    if rsi is None:
        return "SEM_RSI"
    try:
        valor = float(rsi)
    except (TypeError, ValueError):
        return "SEM_RSI"
    if valor < 30:
        return "RSI_<30"
    if valor < 45:
        return "RSI_30_45"
    if valor < 55:
        return "RSI_45_55"
    if valor < 70:
        return "RSI_55_70"
    return "RSI_>=70"


def _contexto_direcao(direcao, tendencia, rsi, rompimento, pullback, mhi, fibo):
    return "|".join([
        str(direcao),
        str(tendencia),
        _faixa_rsi(rsi),
        str(rompimento),
        str(pullback),
        str(mhi),
        "FIBO_ZONA" if str(fibo).startswith("ZONA OURO") else "FIBO_FORA",
    ])


def _estatistica_contexto(contexto):
    historico = _ler_json_seguro(ARQUIVO_APRENDIZADO, [])
    registros = [
        item for item in historico
        if isinstance(item, dict) and item.get("contexto") == contexto
    ]
    wins = sum(1 for item in registros if item.get("resultado") == "WIN")
    losses = sum(1 for item in registros if item.get("resultado") == "LOSS")
    total = wins + losses
    taxa = ((wins + 3.0) / (total + 6.0) * 100.0) if total else None
    return total, wins, losses, taxa


def _ajuste_contexto(contexto):
    if not APRENDIZADO_ATIVO:
        return 0, {"amostra": 0, "wins": 0, "loss": 0, "taxa": None, "ajuste": 0}
    total, wins, losses, taxa = _estatistica_contexto(contexto)
    if total < APRENDIZADO_MINIMO or taxa is None:
        return 0, {"amostra": total, "wins": wins, "loss": losses, "taxa": taxa, "ajuste": 0}
    ajuste = round((taxa - 50.0) / 10.0)
    ajuste = max(-APRENDIZADO_MAX_AJUSTE, min(APRENDIZADO_MAX_AJUSTE, ajuste))
    return ajuste, {
        "amostra": total,
        "wins": wins,
        "loss": losses,
        "taxa": round(taxa, 2),
        "ajuste": ajuste,
    }


# ------------------------------------------------------------
# CORREÇÃO 1 — O PAINEL APAGAVA A MARCAÇÃO
# ------------------------------------------------------------
# registrar_sinal_pendente() roda TODA vez que o painel
# atualiza (/candles e /candles/<par>). Antes ela sobrescrevia
# o registro inteiro, apagando "telegram_enviado" que o envio
# tinha acabado de gravar.
#
# Efeito: telegram_processar_resultados() pulava o sinal para
# sempre. Nenhum WIN/LOSS saía, e sem WIN/LOSS não aparecia
# G1/G2 nenhum.
#
# Agora os campos de estado são preservados quando o mesmo
# par + candle de entrada é registrado de novo.
# ------------------------------------------------------------

CAMPOS_ESTADO_PENDENTE = (
    "telegram_enviado",
    "telegram_enviado_em",
    "resultado",
    "resultado_enviado",
    "resultado_enviado_em",
    "abertura_resultado",
    "fechamento_resultado",
    "registrado",
)


def registrar_sinal_pendente(par, analise):
    if not analise or analise.get("sinal") not in ("CALL", "PUT"):
        return
    entrada_em = analise.get("entrada_em")
    if not entrada_em:
        return
    chave = f"{str(par).upper()}|{int(entrada_em)}"
    registro = {
        "par": str(par).upper(),
        "entrada_em": int(entrada_em),
        "sinal": analise.get("sinal"),
        "pontos_call": analise.get("pontos_call", 0),
        "pontos_put": analise.get("pontos_put", 0),
        "contexto_call": analise.get("contexto_call"),
        "contexto_put": analise.get("contexto_put"),
        "criado_em": int(time.time()),
        "registrado": False,
    }
    with _lock_aprendizado:
        pendentes = _ler_json_seguro(ARQUIVO_PENDENTES, {})

        # Preserva o estado já gravado para esta mesma entrada.
        anterior = pendentes.get(chave)
        if isinstance(anterior, dict):
            for campo in CAMPOS_ESTADO_PENDENTE:
                if campo in anterior:
                    registro[campo] = anterior[campo]
            if anterior.get("criado_em"):
                registro["criado_em"] = anterior["criado_em"]

        pendentes[chave] = registro

        if len(pendentes) > APRENDIZADO_MAX_REGISTROS:
            ordenados = sorted(pendentes.items(), key=lambda par_item: par_item[1].get("criado_em", 0))
            pendentes = dict(ordenados[-APRENDIZADO_MAX_REGISTROS:])
        _salvar_json_seguro(ARQUIVO_PENDENTES, pendentes)


def marcar_pendente_enviado_telegram(par, entrada_em):
    """Grava que este sinal realmente chegou ao grupo.

    O monitor de WIN/LOSS só confere pendentes com esta marca.
    """
    try:
        chave = f"{str(par).upper()}|{int(entrada_em)}"
    except (TypeError, ValueError):
        return False

    with _lock_aprendizado:
        pendentes = _ler_json_seguro(ARQUIVO_PENDENTES, {})
        pendente = pendentes.get(chave)

        if not isinstance(pendente, dict):
            print(
                "TELEGRAM MARCAR: pendente nao encontrado para",
                chave,
            )
            return False

        pendente["telegram_enviado"] = True
        pendente["telegram_enviado_em"] = int(time.time())
        pendente.setdefault("resultado_enviado", False)
        pendentes[chave] = pendente

        return _salvar_json_seguro(ARQUIVO_PENDENTES, pendentes)


def registrar_resultado_aprendizado(par, inicio_candle, resultado):
    if resultado not in ("WIN", "LOSS"):
        return False
    chave = f"{str(par).upper()}|{int(inicio_candle)}"
    with _lock_aprendizado:
        pendentes = _ler_json_seguro(ARQUIVO_PENDENTES, {})
        pendente = pendentes.get(chave)
        if not isinstance(pendente, dict) or pendente.get("registrado"):
            return False
        contexto = pendente.get("contexto_call") if pendente.get("sinal") == "CALL" else pendente.get("contexto_put")
        if not contexto:
            return False
        historico = _ler_json_seguro(ARQUIVO_APRENDIZADO, [])
        historico.append({
            "par": pendente.get("par"),
            "entrada_em": pendente.get("entrada_em"),
            "sinal": pendente.get("sinal"),
            "pontos_call": pendente.get("pontos_call", 0),
            "pontos_put": pendente.get("pontos_put", 0),
            "contexto": contexto,
            "resultado": resultado,
            "quando": int(time.time()),
        })
        historico = historico[-APRENDIZADO_MAX_REGISTROS:]
        pendente["registrado"] = True
        pendentes[chave] = pendente
        _salvar_json_seguro(ARQUIVO_APRENDIZADO, historico)
        _salvar_json_seguro(ARQUIVO_PENDENTES, pendentes)
        return True


def resumo_aprendizado_futuro(analise):
    return {
        "ativo": APRENDIZADO_ATIVO,
        "call": analise.get("aprendizado_call", {}),
        "put": analise.get("aprendizado_put", {}),
        "observacao": "Ajuste histórico conservador; não representa probabilidade de WIN.",
    }


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


def analisar_sinal(candles, par=None):

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
    # APRENDIZADO - AJUSTE PEQUENO PARA O PRÓXIMO SINAL
    # --------------------------------------------------------
    # O histórico só pode ajustar a decisão futura. Nunca altera
    # o resultado de uma entrada já encerrada.
    contexto_call = _contexto_direcao(
        "CALL",
        tendencia,
        rsi,
        rompimento["direcao"] if rompimento["rompimento"] else "NÃO",
        "SIM" if pullback["pullback"] else "NÃO",
        mhi["direcao"],
        fibo["texto"],
    )
    contexto_put = _contexto_direcao(
        "PUT",
        tendencia,
        rsi,
        rompimento["direcao"] if rompimento["rompimento"] else "NÃO",
        "SIM" if pullback["pullback"] else "NÃO",
        mhi["direcao"],
        fibo["texto"],
    )

    ajuste_call, aprendizado_call = _ajuste_contexto(contexto_call)
    ajuste_put, aprendizado_put = _ajuste_contexto(contexto_put)

    pontos_call_base = pontos_call
    pontos_put_base = pontos_put

    pontos_call += ajuste_call
    pontos_put += ajuste_put

    pontos_call = max(0, pontos_call)
    pontos_put = max(0, pontos_put)

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

    # ----------------------------------------------------
    # CORREÇÃO 3 — QUASE NENHUM SINAL PASSAVA
    # ----------------------------------------------------
    # Estava em 5. Como o MHI é contrarian, ele quase sempre
    # dá 1 ponto para o lado oposto — a diferença caía para 4
    # e o sinal era descartado. Somando o cooldown de 180s e
    # 4 pares por ciclo, passavam pouquíssimos sinais por dia.
    #
    # Com 3, os sinais MÉDIO e FORTE continuam passando e o
    # volume volta ao normal. Se vier sinal fraco demais,
    # sobe para 4 e testa de novo.
    # ----------------------------------------------------
    DIFERENCA_MINIMA = 3

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

        "pontos_call_base":
            pontos_call_base,

        "pontos_put_base":
            pontos_put_base,

        "ajuste_aprendizado_call":
            ajuste_call,

        "ajuste_aprendizado_put":
            ajuste_put,

        "contexto_call":
            contexto_call,

        "contexto_put":
            contexto_put,

        "aprendizado_call":
            aprendizado_call,

        "aprendizado_put":
            aprendizado_put,

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
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def telegram_configurado():
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def telegram_enviar(mensagem):
    """Envia mensagem pela API oficial do Telegram."""
    if not telegram_configurado():
        return False

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_BOT_TOKEN
        + "/sendMessage"
    )

    # CORREÇÃO 5: sem este try/except, uma queda de rede
    # levantava exceção aqui e derrubava o ciclo inteiro.
    try:
        resposta = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": mensagem,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception as erro:
        print(
            "TELEGRAM EXCEPTION:",
            type(erro).__name__,
            str(erro),
        )
        return False

    if not resposta.ok:
        print(
            "TELEGRAM ERROR:",
            resposta.status_code,
            resposta.text[:1000],
        )
        return False

    dados = resposta.json()

    if not dados.get("ok"):
        print("TELEGRAM API ERROR:", dados)
        return False

    print("TELEGRAM OK: mensagem enviada.")
    return True


# ============================================================
# RESULTADO AUTOMÁTICO — WIN / LOSS
# ============================================================
# REGRA DW ACADEMY: 1 entrada = 1 resultado.
# Após um LOSS, confirmar LOSS imediatamente no fechamento do M1.
# O próximo sinal só pode ser uma nova entrada normal.


TELEGRAM_RESULTADOS_ATIVOS = (
    os.getenv("TELEGRAM_RESULTADOS_ATIVOS", "1").strip().lower()
    not in ("0", "false", "nao", "não", "off")
)

TELEGRAM_INTERVALO_RESULTADOS = 10


def telegram_formatar_resultado(pendente, resultado, abertura, fechamento):
    par = pendente.get("par", "--")
    sinal = str(pendente.get("sinal", "--")).upper()
    entrada_em = int(pendente.get("entrada_em", 0))

    hora = (
        datetime.fromtimestamp(
            entrada_em,
            tz=FUSO_BR,
        ).strftime("%H:%M")
        if entrada_em
        else "--:--"
    )

    if resultado == "WIN":
        titulo = "WIN"
        emoji = "✅"
    elif resultado == "LOSS":
        titulo = "LOSS"
        emoji = "❌"
    else:
        titulo = "EMPATE"
        emoji = "⚪"

    # Abertura, fechamento e variação NÃO entram mais na
    # mensagem — o aluno não usava esses números. Eles
    # continuam gravados no arquivo de pendentes e aparecem em
    # /telegram/pendentes, para conferir contra o gráfico da
    # corretora se algum dia precisar auditar um resultado.

    if resultado == "WIN":
        rodape = "🔥 Entrada vencedora."
    elif resultado == "LOSS":
        # A orientação de gale vira UMA LINHA aqui, em vez de
        # uma mensagem separada. Mensagem separada de G1 e G2
        # enchia o grupo e atrapalhava a leitura.
        rodape = (
            "📉 Entrada encerrada.\n"
            "🔁 Martingale: até G2 (opcional)"
        )
    else:
        rodape = "➖ Vela fechou no mesmo preço."

    return (
        f"{emoji} <b>{titulo}</b> · <b>{par}</b>\n"
        "─────────────\n"
        f"📌 Sinal  <b>{sinal}</b> · M1\n"
        f"⏰ Entrada  <b>{hora}</b>\n\n"
        f"{rodape}"
    )


def telegram_enviar_resultado_pendente(
    pendente,
    resultado,
    abertura,
    fechamento,
):
    """
    Animação do resultado no próprio Telegram.
    Primeiro mostra APURANDO, depois CONFIRMANDO e por fim
    substitui pela mensagem final WIN/LOSS/EMPATE.
    """
    if not telegram_configurado():
        return False

    par = pendente.get("par", "--")
    sinal = str(pendente.get("sinal", "--")).upper()

    # Frame 1 — começa a animação.
    frame1 = (
        f"🔎 <b>APURANDO</b> · <b>{par}</b>\n"
        "─────────────\n"
        f"📌 Sinal  <b>{sinal}</b> · M1\n"
        "⏳ Calculando resultado...\n\n"
        "▰▱▱▱▱▱▱▱▱▱"
    )

    # Frame 2 — confirmação.
    frame2 = (
        f"⚡ <b>CONFERINDO</b> · <b>{par}</b>\n"
        "─────────────\n"
        f"📌 Sinal  <b>{sinal}</b> · M1\n"
        "🔄 Lendo o fechamento da vela...\n\n"
        "▰▰▰▰▰▰▰▰▱▱"
    )

    final = telegram_formatar_resultado(
        pendente,
        resultado,
        abertura,
        fechamento,
    )

    try:
        send_url = (
            "https://api.telegram.org/bot"
            + TELEGRAM_BOT_TOKEN
            + "/sendMessage"
        )

        resposta = requests.post(
            send_url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": frame1,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if not resposta.ok:
            print(
                "TELEGRAM RESULTADO ANIM ERROR:",
                resposta.status_code,
                resposta.text[:1000],
            )
            return False

        dados = resposta.json()

        if not dados.get("ok"):
            print("TELEGRAM RESULTADO API ERROR:", dados)
            return False

        message_id = dados["result"]["message_id"]

        edit_url = (
            "https://api.telegram.org/bot"
            + TELEGRAM_BOT_TOKEN
            + "/editMessageText"
        )

        time.sleep(0.65)

        r2 = requests.post(
            edit_url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": message_id,
                "text": frame2,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if not r2.ok:
            print(
                "TELEGRAM RESULTADO FRAME2 ERROR:",
                r2.status_code,
                r2.text[:1000],
            )

        time.sleep(0.65)

        r3 = requests.post(
            edit_url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": message_id,
                "text": final,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if not r3.ok:
            print(
                "TELEGRAM RESULTADO FINAL ERROR:",
                r3.status_code,
                r3.text[:1000],
            )
            return False

        dados_final = r3.json()

        if not dados_final.get("ok"):
            print(
                "TELEGRAM RESULTADO FINAL API ERROR:",
                dados_final,
            )
            return False

        print(
            "TELEGRAM RESULTADO ANIMADO OK:",
            par,
            resultado,
        )
        return True

    except Exception as erro:
        print(
            "TELEGRAM RESULTADO ANIM EXCEPTION:",
            type(erro).__name__,
            str(erro),
        )
        return False


def conferir_resultado_pendente_telegram(par, pendente, iq):
    entrada_em = int(pendente.get("entrada_em", 0))
    sinal = str(pendente.get("sinal", "")).upper()

    if not entrada_em or sinal not in ("CALL", "PUT"):
        return None

    # Só confere depois do fechamento do M1.
    if time.time() < entrada_em + TIMEFRAME:
        return None

    candles = buscar_candles_com_timeout(
        iq,
        par,
        CANDLE_COUNT,
        timeout_segundos=5,
    )

    por_inicio = {
        int(c.get("from")): c
        for c in candles
        if c.get("from") is not None
    }

    alvo = por_inicio.get(entrada_em)

    if not alvo:
        return None

    # Garante que o candle realmente terminou.
    if time.time() < entrada_em + TIMEFRAME:
        return None

    abertura = alvo.get("open")
    fechamento = alvo.get("close")

    if abertura is None or fechamento is None:
        return None

    if fechamento > abertura:
        direcao = "ALTA"
    elif fechamento < abertura:
        direcao = "BAIXA"
    else:
        direcao = "DOJI"

    if direcao == "DOJI":
        resultado = "EMPATE"
    elif sinal == "CALL" and direcao == "ALTA":
        resultado = "WIN"
    elif sinal == "PUT" and direcao == "BAIXA":
        resultado = "WIN"
    else:
        resultado = "LOSS"

    return {
        "resultado": resultado,
        "abertura": abertura,
        "fechamento": fechamento,
    }


def telegram_processar_resultados():
    if not TELEGRAM_RESULTADOS_ATIVOS:
        return

    if not telegram_configurado():
        return

    try:
        agora = int(time.time())

        with _lock_aprendizado:
            pendentes = _ler_json_seguro(
                ARQUIVO_PENDENTES,
                {},
            )

        candidatos = []

        for chave, pendente in pendentes.items():
            if not isinstance(pendente, dict):
                continue

            # Só confere o que realmente foi anunciado no grupo.
            if not pendente.get("telegram_enviado"):
                continue

            if pendente.get("resultado_enviado"):
                continue

            entrada_em = int(
                pendente.get("entrada_em", 0) or 0
            )

            # Não deixa crescer indefinidamente.
            if entrada_em <= 0:
                continue

            # Ainda não fechou o M1: nem tenta.
            if agora < entrada_em + TIMEFRAME:
                continue

            # Depois de alguns minutos, ainda pode ser conferido,
            # mas não processa sinais muito antigos.
            if agora - entrada_em > 15 * 60:
                continue

            candidatos.append((chave, pendente))

        # Processa no máximo alguns por ciclo.
        candidatos = candidatos[:8]

        if not candidatos:
            return

        iq = conectar()

        for chave, pendente in candidatos:
            try:
                resultado = conferir_resultado_pendente_telegram(
                    pendente.get("par"),
                    pendente,
                    iq,
                )

                if not resultado:
                    continue

                enviado = telegram_enviar_resultado_pendente(
                    pendente,
                    resultado["resultado"],
                    resultado["abertura"],
                    resultado["fechamento"],
                )

                if enviado:
                    # Aprendizado usa somente WIN/LOSS.
                    if resultado["resultado"] in ("WIN", "LOSS"):
                        registrar_resultado_aprendizado(
                            pendente.get("par"),
                            int(pendente.get("entrada_em")),
                            resultado["resultado"],
                        )

                    # Martingale SOMENTE depois do resultado.
                    # WIN encerra; LOSS mostra G1/G2 disponível.
                    if resultado["resultado"] == "LOSS":
                        telegram_registrar_loss_e_mostrar_gale(
                            pendente
                        )
                    elif resultado["resultado"] == "WIN":
                        telegram_encerrar_martingale(
                            pendente,
                            "WIN",
                        )

                    with _lock_aprendizado:
                        atuais = _ler_json_seguro(
                            ARQUIVO_PENDENTES,
                            {},
                        )
                        item = atuais.get(chave)

                        if isinstance(item, dict):
                            item["resultado"] = resultado["resultado"]
                            item["resultado_enviado"] = True
                            item["resultado_enviado_em"] = int(time.time())
                            item["abertura_resultado"] = resultado["abertura"]
                            item["fechamento_resultado"] = resultado["fechamento"]
                            atuais[chave] = item

                            _salvar_json_seguro(
                                ARQUIVO_PENDENTES,
                                atuais,
                            )

                    print(
                        "TELEGRAM RESULTADO OK:",
                        pendente.get("par"),
                        pendente.get("sinal"),
                        resultado["resultado"],
                    )

            except Exception as erro:
                print(
                    "TELEGRAM RESULTADO:",
                    pendente.get("par"),
                    type(erro).__name__,
                    str(erro),
                )

    except Exception as erro:
        invalidar_conexao()
        print(
            "TELEGRAM RESULTADOS CICLO:",
            type(erro).__name__,
            str(erro),
        )


def iniciar_monitor_resultados_telegram():
    if not TELEGRAM_RESULTADOS_ATIVOS:
        print("TELEGRAM RESULTADOS: desativado.")
        return

    def trabalhador():
        time.sleep(12)

        while True:
            try:
                telegram_processar_resultados()
            except Exception as erro:
                print(
                    "TELEGRAM RESULTADOS MONITOR:",
                    type(erro).__name__,
                    str(erro),
                )

            time.sleep(TELEGRAM_INTERVALO_RESULTADOS)

    thread = threading.Thread(
        target=trabalhador,
        name="telegram-resultados",
        daemon=True,
    )
    thread.start()

    print("TELEGRAM RESULTADOS: monitor iniciado.")


iniciar_monitor_resultados_telegram()


# ============================================================
# MARTINGALE APÓS RESULTADO
# ============================================================
#
# SEQUÊNCIA: ENTRADA → LOSS → G1 → LOSS → G2 → LOSS FINAL
# WIN em qualquer etapa encerra a sequência.
#
# Regra:
# - Entrada normal é enviada primeiro.
# - Depois do fechamento do M1, envia WIN/LOSS.
# - SOMENTE depois de LOSS aparece a disponibilidade de G1.
# - Após G1 LOSS, aparece G2.
# - Após WIN em qualquer etapa, encerra a sequência.
# - Após G2 LOSS, encerra como LOSS FINAL.
# - Nunca mostra Martingale antes do resultado.
#
# Observação: esta camada é de alertas Telegram e não executa
# ordens na IQ Option.
#
# AVISO PARA O ALUNO: o gale dobra a exposição justamente no
# momento em que a leitura já errou. É etapa opcional.

TELEGRAM_MARTINGALE_ATIVO = (
    os.getenv("TELEGRAM_MARTINGALE_ATIVO", "1").strip().lower()
    not in ("0", "false", "nao", "não", "off")
)

TELEGRAM_MAX_GALES = 2
_martingale_lock = threading.Lock()
_martingale_sequencias = {}


def telegram_martingale_key(pendente):
    return (
        f"{str(pendente.get('par', '')).upper()}|"
        f"{int(pendente.get('entrada_em', 0) or 0)}"
    )


def telegram_registrar_loss_e_mostrar_gale(pendente):
    """Apenas CONTA a etapa da sequência. Não envia mensagem.

    Antes, cada LOSS disparava uma mensagem separada "G1
    DISPONÍVEL" / "G2 DISPONÍVEL" no grupo. Duas mensagens
    extras por sequência, no meio dos sinais, atrapalhavam a
    leitura no celular.

    Agora a orientação aparece como UMA LINHA no rodapé da
    própria mensagem de LOSS. A contagem continua aqui porque
    é ela que encerra a sequência depois do G2.
    """
    if not TELEGRAM_MARTINGALE_ATIVO:
        return

    chave = telegram_martingale_key(pendente)

    with _martingale_lock:
        sequencia = _martingale_sequencias.get(
            chave,
            {
                "gale_atual": 0,
                "finalizada": False,
            },
        )

        if sequencia["finalizada"]:
            return

        proximo_gale = int(sequencia["gale_atual"]) + 1

        if proximo_gale > TELEGRAM_MAX_GALES:
            sequencia["finalizada"] = True
            _martingale_sequencias[chave] = sequencia
            return

        sequencia["gale_atual"] = proximo_gale
        _martingale_sequencias[chave] = sequencia


def telegram_encerrar_martingale(pendente, resultado):
    chave = telegram_martingale_key(pendente)

    with _martingale_lock:
        sequencia = _martingale_sequencias.get(
            chave,
            {
                "gale_atual": 0,
                "finalizada": False,
            },
        )

        if resultado == "WIN":
            sequencia["finalizada"] = True

        elif (
            resultado == "LOSS"
            and int(sequencia.get("gale_atual", 0))
            >= TELEGRAM_MAX_GALES
        ):
            sequencia["finalizada"] = True

        _martingale_sequencias[chave] = sequencia


# ============================================================
# TESTE TELEGRAM
# ============================================================

@app.get("/telegram/test")
def telegram_test():
    try:
        if not TELEGRAM_BOT_TOKEN:
            return jsonify({
                "ok": False,
                "erro": "TELEGRAM_BOT_TOKEN não configurado no Render.",
            }), 503

        if not TELEGRAM_CHAT_ID:
            return jsonify({
                "ok": False,
                "erro": "TELEGRAM_CHAT_ID não configurado no Render.",
            }), 503

        mensagem = (
            "🤖 <b>DW TRADING — TESTE TELEGRAM</b>\n\n"
            "✅ Bot conectado com sucesso!\n"
            "📊 Grupo: DW Trading — IQ Option\n"
            "⏱️ Timeframe: M1\n"
            "🔔 Integração Telegram funcionando.\n\n"
            "Este é apenas um teste."
        )

        enviado = telegram_enviar(mensagem)

        if enviado:
            return jsonify({
                "ok": True,
                "mensagem": "Mensagem enviada ao Telegram.",
                "chat_id_configurado": True,
            })

        return jsonify({
            "ok": False,
            "erro": (
                "A API do Telegram recusou o envio. "
                "Verifique se o bot está no grupo e se "
                "TELEGRAM_CHAT_ID está correto."
            ),
            "chat_id_configurado": True,
        }), 502

    except Exception as erro:
        print(
            "ERRO /telegram/test:",
            type(erro).__name__,
            str(erro),
        )

        return jsonify({
            "ok": False,
            "erro": "Erro interno ao testar o Telegram.",
            "tipo": type(erro).__name__,
        }), 500


# ============================================================
# DIAGNÓSTICO DA FILA DE PENDENTES
# ============================================================
#
# Mostra o que está na fila e em que estado, sem adivinhação.
#
#   telegram_enviado: true   -> o sinal chegou ao grupo
#   resultado_enviado: true  -> o WIN/LOSS já saiu
#
# Uso: /telegram/pendentes
# ============================================================

@app.get("/telegram/pendentes")
def telegram_pendentes():
    with _lock_aprendizado:
        pendentes = _ler_json_seguro(ARQUIVO_PENDENTES, {})

    agora = int(time.time())

    lista = []

    for chave, item in pendentes.items():
        if not isinstance(item, dict):
            continue

        entrada_em = int(item.get("entrada_em", 0) or 0)

        lista.append({
            "chave": chave,
            "par": item.get("par"),
            "sinal": item.get("sinal"),
            "entrada_em": entrada_em,
            "entrada": (
                datetime.fromtimestamp(
                    entrada_em,
                    tz=FUSO_BR,
                ).strftime("%H:%M")
                if entrada_em
                else "--:--"
            ),
            "telegram_enviado": bool(item.get("telegram_enviado")),
            "resultado": item.get("resultado"),
            "resultado_enviado": bool(item.get("resultado_enviado")),
            "idade_segundos": (agora - entrada_em) if entrada_em else None,
        })

    lista.sort(
        key=lambda x: x.get("entrada_em") or 0,
        reverse=True,
    )

    return jsonify({
        "ok": True,
        "total": len(lista),
        "enviados_telegram": sum(
            1 for x in lista if x["telegram_enviado"]
        ),
        "aguardando_resultado": sum(
            1 for x in lista
            if x["telegram_enviado"] and not x["resultado_enviado"]
        ),
        "pendentes": lista[:50],
        "timestamp": agora,
    })


# ============================================================
# SINAIS AUTOMÁTICOS NO TELEGRAM
# ============================================================
#
# Este módulo SOMENTE envia alertas. Não abre ordens.
#
# Ele usa a mesma análise existente em analisar_sinal().
# Para evitar mensagens repetidas, cada par + candle de entrada
# é enviado apenas uma vez.
#
# O serviço verifica um pequeno grupo de pares por ciclo e gira
# a lista. Isso evita sobrecarregar o worker do Render.

TELEGRAM_SINAIS_ATIVOS = (
    os.getenv("TELEGRAM_SINAIS_ATIVOS", "1").strip().lower()
    not in ("0", "false", "nao", "não", "off")
)

TELEGRAM_INTERVALO_SINAIS = 60
TELEGRAM_ESPERA_ENTRE_ENTRADAS = 180  # 3 minutos
_telegram_ultima_entrada_enviada = 0
_telegram_lock_cooldown = threading.Lock()
TELEGRAM_MAX_PARES_CICLO = 4

_lock_sinais_telegram = threading.Lock()
_sinais_telegram_enviados = {}


def _barra_forca(pontos, maximo=10):
    """Transforma a pontuação numa barra curta de 5 marcas.

    Um número solto ("Confirmações: 7") não diz nada para quem
    está começando. A barra mostra de bate-pronto se a leitura
    veio folgada ou apertada.
    """
    try:
        valor = int(pontos)
    except (TypeError, ValueError):
        valor = 0

    valor = max(0, min(maximo, valor))

    # int(x + 0.5), não round(). O round() do Python arredonda
    # 2.5 para 2 e 4.5 para 4 (regra do banqueiro), o que fazia
    # forças diferentes desenharem a mesma barra.
    cheios = int((valor / maximo * 5) + 0.5)
    cheios = max(0, min(5, cheios))

    return "●" * cheios + "○" * (5 - cheios)


def telegram_formatar_sinal(par, analise):
    sinal = str(analise.get("sinal", "")).upper()
    entrada = analise.get("entrada", "--:--")
    preco = analise.get("preco")
    confianca = analise.get("confianca", 0)
    tendencia = str(analise.get("tendencia", "NEUTRA")).upper()
    rsi = analise.get("rsi")

    if sinal == "CALL":
        emoji = "🟢"
        titulo = "CALL"
    elif sinal == "PUT":
        emoji = "🔴"
        titulo = "PUT"
    else:
        return None

    if tendencia == "ALTA":
        seta = "📈"
    elif tendencia == "BAIXA":
        seta = "📉"
    else:
        seta = "➖"

    rsi_texto = f"{float(rsi):.1f}" if isinstance(rsi, (int, float)) else "--"
    preco_texto = f"{float(preco):.6f}" if isinstance(preco, (int, float)) else "--"

    # SEM moldura de caracteres. O Telegram no celular usa fonte
    # de largura variável, então "╭──╮" e "│" nunca alinham e a
    # mensagem fica torta. Espaço em branco e negrito resolvem.
    return (
        f"{emoji} <b>{titulo}</b> · <b>{par}</b>\n"
        "─────────────\n"
        f"⏰ Entrada  <b>{entrada}</b>\n"
        f"⏱ Duração  <b>M1</b>\n\n"
        f"{seta} Tendência  <b>{tendencia}</b>\n"
        f"⭐ Força  {_barra_forca(confianca)}  <b>{confianca}</b>\n"
        f"📊 RSI  <b>{rsi_texto}</b>\n"
        f"💰 Preço  <b>{preco_texto}</b>\n\n"
        "🔁 Martingale: até G2 (opcional)\n"
        "⚠️ <i>Alerta técnico e educacional.</i>"
    )


def _limpar_cache_sinais_enviados():
    """Mantém o cache de duplicatas em tamanho razoável."""
    with _lock_sinais_telegram:
        if len(_sinais_telegram_enviados) > 500:
            antigas = sorted(
                _sinais_telegram_enviados.items(),
                key=lambda item: item[1],
            )
            for chave_antiga, _ in antigas[:-300]:
                _sinais_telegram_enviados.pop(chave_antiga, None)


def telegram_enviar_sinal_animado(par, analise):
    """
    Envia o sinal com uma pequena animação feita por edição da mensagem.
    O Telegram recebe uma mensagem e ela é atualizada em poucos frames,
    criando o efeito visual de animação sem precisar de GIF externo.
    """
    global _telegram_ultima_entrada_enviada

    if not telegram_configurado():
        return False

    sinal = str(analise.get("sinal", "")).upper()
    entrada_em = analise.get("entrada_em")

    if sinal not in ("CALL", "PUT") or not entrada_em:
        return False

    # Só permite uma nova entrada depois de 3 minutos.
    # Isso evita vários CALL/PUT quase simultâneos no grupo.
    agora = time.time()
    with _telegram_lock_cooldown:
        if (
            _telegram_ultima_entrada_enviada
            and agora - _telegram_ultima_entrada_enviada
            < TELEGRAM_ESPERA_ENTRE_ENTRADAS
        ):
            restante = int(
                TELEGRAM_ESPERA_ENTRE_ENTRADAS
                - (agora - _telegram_ultima_entrada_enviada)
            )
            print(
                f"TELEGRAM COOLDOWN: aguardando {restante}s "
                f"antes da próxima entrada."
            )
            return False

    chave = f"{str(par).upper()}|{int(entrada_em)}|{sinal}"

    with _lock_sinais_telegram:
        if chave in _sinais_telegram_enviados:
            return False

    if sinal == "CALL":
        emoji = "🟢"
        titulo = "CALL"
    else:
        emoji = "🔴"
        titulo = "PUT"

    # Animação compacta para leitura rápida no celular.
    pre = (
        "🤖 <b>DW TRADING</b>\n"
        "─────────────\n"
        f"🔎 Analisando <b>{par}</b>\n\n"
        "▰▱▱▱▱▱▱▱▱▱"
    )

    meio = (
        f"⚡ <b>SINAL {titulo}</b> · <b>{par}</b>\n"
        "─────────────\n"
        "🎯 Confirmando indicadores...\n\n"
        "▰▰▰▰▰▰▰▱▱▱"
    )

    try:
        url = (
            "https://api.telegram.org/bot"
            + TELEGRAM_BOT_TOKEN
            + "/sendMessage"
        )

        resposta = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": pre,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if not resposta.ok:
            print("TELEGRAM ANIM ERROR:", resposta.status_code, resposta.text[:1000])
            return False

        dados = resposta.json()

        if not dados.get("ok"):
            print("TELEGRAM ANIM API ERROR:", dados)
            return False

        message_id = dados["result"]["message_id"]

        time.sleep(0.7)

        edit_url = (
            "https://api.telegram.org/bot"
            + TELEGRAM_BOT_TOKEN
            + "/editMessageText"
        )

        requests.post(
            edit_url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": message_id,
                "text": meio,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        time.sleep(0.7)

        final = telegram_formatar_sinal(par, analise)

        if not final:
            return False

        resposta_final = requests.post(
            edit_url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": message_id,
                "text": final.replace(
                    f"{emoji} <b>{titulo}</b>",
                    f"{emoji} <b>{titulo} CONFIRMADO</b>",
                    1,
                ),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if not resposta_final.ok:
            print(
                "TELEGRAM ANIM FINAL ERROR:",
                resposta_final.status_code,
                resposta_final.text[:1000],
            )
            return False

        dados_final = resposta_final.json()

        if not dados_final.get("ok"):
            print("TELEGRAM ANIM FINAL API ERROR:", dados_final)
            return False

        with _lock_sinais_telegram:
            _sinais_telegram_enviados[chave] = int(time.time())

        with _telegram_lock_cooldown:
            _telegram_ultima_entrada_enviada = time.time()

        # ------------------------------------------------
        # Marca no arquivo de pendentes que este sinal
        # realmente chegou ao Telegram. O monitor de
        # WIN/LOSS só confere quem tem esta marca.
        # ------------------------------------------------
        marcar_pendente_enviado_telegram(par, entrada_em)

        # CORREÇÃO 4: a limpeza do cache estava DENTRO do
        # "except" do bloco de marcação, ou seja, só rodava
        # quando dava erro. Agora roda no caminho normal.
        _limpar_cache_sinais_enviados()

        print("TELEGRAM ANIMADO OK:", par, sinal)
        return True

    except Exception as erro:
        print(
            "TELEGRAM ANIM EXCEPTION:",
            type(erro).__name__,
            str(erro),
        )
        return False


def telegram_enviar_sinal_se_novo(par, analise):
    return telegram_enviar_sinal_animado(par, analise)


def telegram_processar_sinais():
    if not TELEGRAM_SINAIS_ATIVOS:
        return

    if not telegram_configurado():
        print(
            "TELEGRAM SINAIS: aguardando "
            "TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID."
        )
        return

    try:
        iq = conectar()

        # Rotação: cada ciclo analisa poucos pares.
        indice = (
            int(time.time() // TELEGRAM_INTERVALO_SINAIS)
            * TELEGRAM_MAX_PARES_CICLO
        ) % len(PARES)

        pares_ciclo = [
            PARES[
                (indice + i) % len(PARES)
            ]
            for i in range(
                min(TELEGRAM_MAX_PARES_CICLO, len(PARES))
            )
        ]

        for par in pares_ciclo:
            try:
                candles = buscar_candles_com_timeout(
                    iq,
                    par,
                    CANDLE_COUNT,
                    timeout_segundos=5,
                )

                if not candles:
                    continue

                analise = analisar_sinal(
                    candles,
                    par,
                )

                # A ORDEM IMPORTA: grava o pendente ANTES de
                # enviar. A marcação só consegue atualizar um
                # registro que já existe.
                registrar_sinal_pendente(
                    par,
                    analise,
                )

                if analise.get("sinal") in ("CALL", "PUT"):
                    telegram_enviar_sinal_se_novo(
                        par,
                        analise,
                    )

            except Exception as erro:
                print(
                    "TELEGRAM SINAIS:",
                    par,
                    type(erro).__name__,
                    str(erro),
                )

    except Exception as erro:
        invalidar_conexao()
        print(
            "TELEGRAM SINAIS CICLO:",
            type(erro).__name__,
            str(erro),
        )


def iniciar_monitor_telegram():
    """
    Inicia um único monitor dentro do processo Gunicorn.
    Ele apenas envia alertas; nunca executa ordens.
    """
    if not TELEGRAM_SINAIS_ATIVOS:
        print("TELEGRAM SINAIS: desativado.")
        return

    def trabalhador():
        # Pequeno atraso para deixar Flask/Gunicorn iniciar primeiro.
        time.sleep(8)

        while True:
            try:
                telegram_processar_sinais()
            except Exception as erro:
                print(
                    "TELEGRAM MONITOR:",
                    type(erro).__name__,
                    str(erro),
                )

            time.sleep(TELEGRAM_INTERVALO_SINAIS)

    thread = threading.Thread(
        target=trabalhador,
        name="telegram-sinais",
        daemon=True,
    )
    thread.start()
    print("TELEGRAM SINAIS: monitor iniciado.")


@app.get("/telegram/sinais/test")
def telegram_sinais_test():
    """
    Força uma leitura dos pares e envia o primeiro CALL/PUT
    encontrado. Se não houver sinal confirmado, informa isso.
    """
    try:
        if not telegram_configurado():
            return jsonify({
                "ok": False,
                "erro": (
                    "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID "
                    "não configurado."
                ),
            }), 503

        iq = conectar()

        for par in PARES:
            try:
                candles = buscar_candles_com_timeout(
                    iq,
                    par,
                    CANDLE_COUNT,
                    timeout_segundos=5,
                )

                if not candles:
                    continue

                analise = analisar_sinal(candles, par)

                if analise.get("sinal") in ("CALL", "PUT"):

                    # ----------------------------------------
                    # CORREÇÃO 2 — SINAL DE TESTE SEM RESULTADO
                    # ----------------------------------------
                    # Esta rota enviava o sinal SEM gravar o
                    # pendente antes. A marcação procurava um
                    # registro que não existia e não fazia nada,
                    # então o WIN/LOSS desse sinal nunca saía.
                    # ----------------------------------------
                    registrar_sinal_pendente(par, analise)

                    enviado = telegram_enviar_sinal_se_novo(
                        par,
                        analise,
                    )

                    return jsonify({
                        "ok": True,
                        "enviado": enviado,
                        "par": par,
                        "sinal": analise.get("sinal"),
                        "entrada": analise.get("entrada"),
                        "confianca": analise.get("confianca"),
                        "mensagem": (
                            "Sinal enviado ao Telegram."
                            if enviado
                            else (
                                "Sinal nao enviado: ja foi enviado "
                                "antes ou ainda esta no intervalo "
                                "de espera entre entradas."
                            )
                        ),
                    })

            except Exception as erro:
                print(
                    "TELEGRAM SINAIS TEST:",
                    par,
                    type(erro).__name__,
                    str(erro),
                )

        return jsonify({
            "ok": True,
            "enviado": False,
            "mensagem": (
                "Nenhum CALL/PUT confirmado foi encontrado "
                "nos pares testados agora."
            ),
        })

    except Exception as erro:
        invalidar_conexao()
        return jsonify({
            "ok": False,
            "erro": "Erro ao testar sinais Telegram.",
            "tipo": type(erro).__name__,
        }), 503


# O monitor é iniciado quando o processo Gunicorn carrega o app.
iniciar_monitor_telegram()


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

    conectado = False

    cliente = _iq

    if cliente is not None:

        try:

            conectado = bool(
                cliente.check_connect()
            )

        except Exception:

            conectado = False

    return jsonify({

        "ok": True,

        "servico":
            "iq-option-candles",

        "iq_conectada":
            conectado,

        "telegram_configurado":
            telegram_configurado(),

        "telegram_sinais_ativos":
            TELEGRAM_SINAIS_ATIVOS,

        "telegram_resultados_ativos":
            TELEGRAM_RESULTADOS_ATIVOS,

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
                "PARES, PARES_FOREX ou PARES_ACOES do app.py."
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

    # Aprende SOMENTE com o M1 encerrado. M2/M3 são métricas extras
    # da mesma entrada e não devem duplicar o treinamento.
    aprendizado_registrado = False
    if m1 in ("WIN", "LOSS"):
        aprendizado_registrado = registrar_resultado_aprendizado(
            par,
            inicio_candle,
            m1,
        )

    # Devolve também os preços usados na conferência, para que
    # o resultado possa ser auditado contra o gráfico da
    # corretora. Sem isso não há como saber se uma divergência
    # veio do cálculo ou do momento da entrada.
    return jsonify({
        "par": par,
        "sinal": sinal,
        "status": m1 or "AGUARDANDO",
        "m1": m1,
        "m2": m2,
        "m3": m3,
        "abertura": abertura,
        "fechamento": alvo["close"],
        "maxima": alvo["high"],
        "minima": alvo["low"],
        "candle_de": alvo["from"],
        "candle_ate": alvo["to"],
        "inicio": inicio_candle,
        "aprendizado_registrado": aprendizado_registrado,
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
            candles,
            par
        )

        tempo = round(
            time.time() - inicio,
            2
        )

        registrar_sinal_pendente(par, analise)

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

            "pontos_call_base":
                analise.get("pontos_call_base"),

            "pontos_put_base":
                analise.get("pontos_put_base"),

            "ajuste_aprendizado_call":
                analise.get("ajuste_aprendizado_call"),

            "ajuste_aprendizado_put":
                analise.get("ajuste_aprendizado_put"),

            "aprendizado":
                resumo_aprendizado_futuro(analise),

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
        invalidar_conexao()

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

        invalidar_conexao()

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
        # Cada par tem 7s, e existe um teto total de 22s para a
        # chamada inteira. Quando o teto estoura, os pares
        # restantes voltam com status PULADO em vez de
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
                    dados,
                    par
                )

                registrar_sinal_pendente(par, analise)

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

                    "pontos_call_base":
                        analise.get("pontos_call_base"),

                    "pontos_put_base":
                        analise.get("pontos_put_base"),

                    "ajuste_aprendizado_call":
                        analise.get("ajuste_aprendizado_call"),

                    "ajuste_aprendizado_put":
                        analise.get("ajuste_aprendizado_put"),

                    "aprendizado":
                        resumo_aprendizado_futuro(analise),

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

        invalidar_conexao()

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
