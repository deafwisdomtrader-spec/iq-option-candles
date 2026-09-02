import os
import time
import json
import sqlite3
import threading
import concurrent.futures
from datetime import datetime, timezone, timedelta

import requests

# Brasil não tem mais horário de verão desde 2019,
# então um offset fixo de UTC-3 é sempre correto
# (evita depender de tzdata instalado no servidor do Render).
FUSO_BR = timezone(timedelta(hours=-3))

from flask import Flask, jsonify, request
from iqoptionapi.stable_api import IQ_Option

app = Flask(__name__)

# ============================================================
# TELEGRAM — SINAL (IMAGEM + CARTÃO) E RESULTADO (COM GALE)
# ============================================================
#
# Manda uma mensagem com FOTO pro Telegram: uma pro sinal
# (seta CALL/PUT) e outra pro resultado (WIN/LOSS), igual ao
# formato original do canal.
#
# As variáveis TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID já devem
# estar configuradas no Render (Settings -> Environment). Se
# não estiverem, o envio é silenciosamente pulado — a análise
# de sinais nunca pode quebrar por causa do Telegram.
#
# As imagens (call_dw.webp, put_dw.webp, win_dw.webp, gale1_dw.webp,
# gale2_dw.webp, loss_dw.webp, empate_dw.webp) precisam estar na raiz
# do repositório, do lado do app.py — é de lá que elas são
# lidas e enviadas pro Telegram.

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DIRETORIO_APP = os.path.dirname(os.path.abspath(__file__))

IMAGENS_TELEGRAM = {
    "call": os.path.join(DIRETORIO_APP, "call_dw.webp"),
    "put": os.path.join(DIRETORIO_APP, "put_dw.webp"),
    "win": os.path.join(DIRETORIO_APP, "win_dw.webp"),
    "win_g1": os.path.join(DIRETORIO_APP, "gale1_dw.webp"),
    "win_g2": os.path.join(DIRETORIO_APP, "gale2_dw.webp"),
    "loss": os.path.join(DIRETORIO_APP, "loss_dw.webp"),
    "empate": os.path.join(DIRETORIO_APP, "empate_dw.webp"),
}

# Pool próprio, separado do de candles, pra um Telegram lento
# nunca competir com a busca de dados por vagas de thread.
#
# max_workers=1 DE PROPÓSITO: cada card é sticker + texto, em
# duas chamadas separadas à API do Telegram. Com mais de uma
# vaga, cards de pares diferentes rodavam em paralelo e os
# stickers chegavam todos juntos, antes dos textos — bagunçado.
# Com 1 vaga só, um card inteiro (sticker + texto) termina
# antes do próximo começar, então a ordem sempre sai certinha.
_executor_telegram = concurrent.futures.ThreadPoolExecutor(
    max_workers=1
)


def _enviar_telegram_texto_sync(mensagem):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    try:

        url = (
            "https://api.telegram.org/bot"
            + TELEGRAM_BOT_TOKEN
            + "/sendMessage"
        )

        resposta = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": mensagem,
                "parse_mode": "HTML",
            },
            timeout=8,
        )

        return resposta.ok

    except Exception:

        return False


def _enviar_telegram_sticker_sync(caminho_imagem):
    """Manda a imagem como STICKER (sem moldura, sem fundo
    branco). É assim que o Telegram exibe imagem "colada" no
    fundo do chat, em vez da caixa clara que sendPhoto sempre
    desenha atrás da imagem.
    """

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    if not caminho_imagem or not os.path.isfile(caminho_imagem):
        return False

    try:

        url = (
            "https://api.telegram.org/bot"
            + TELEGRAM_BOT_TOKEN
            + "/sendSticker"
        )

        with open(caminho_imagem, "rb") as arquivo_imagem:

            resposta = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID},
                files={"sticker": arquivo_imagem},
                timeout=15,
            )

        return resposta.ok

    except Exception:

        return False


def _enviar_telegram_photo_sync(caminho_imagem):
    """Reserva: só usado se o arquivo for rejeitado como
    sticker (ex: não bate com o tamanho/tipo exigido pelo
    Telegram). Melhor a imagem chegar com moldura do que não
    chegar de jeito nenhum.
    """

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    try:

        url = (
            "https://api.telegram.org/bot"
            + TELEGRAM_BOT_TOKEN
            + "/sendPhoto"
        )

        with open(caminho_imagem, "rb") as arquivo_imagem:

            resposta = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID},
                files={"photo": arquivo_imagem},
                timeout=15,
            )

        return resposta.ok

    except Exception:

        return False


def _enviar_cartao_telegram_sync(caminho_imagem, caption):
    """Manda o ícone como STICKER primeiro (mensagem própria,
    sem legenda — sticker não aceita legenda no Telegram) e
    depois o cartão de texto, como mensagem separada logo em
    seguida. É o mesmo padrão de duas mensagens do canal
    original: ícone em cima, cartão embaixo.
    """

    if caminho_imagem and os.path.isfile(caminho_imagem):

        enviado_como_sticker = _enviar_telegram_sticker_sync(
            caminho_imagem
        )

        if not enviado_como_sticker:
            _enviar_telegram_photo_sync(caminho_imagem)

    _enviar_telegram_texto_sync(caption)


def enviar_telegram_foto(caminho_imagem, caption):
    """Dispara o envio (sticker + cartão) em segundo plano,
    sem travar a rota.
    """

    try:
        _executor_telegram.submit(
            _enviar_cartao_telegram_sync,
            caminho_imagem,
            caption,
        )
    except Exception:
        pass


def _mercado_do_par(par):

    if par.endswith("-OTC"):
        return "OTC"

    if par in PARES_ACOES:
        return "AÇÕES"

    return "FOREX"


def montar_caption_sinal(par, analise):
    """Cartão de sinal: mercado, entrada, duração, tendência,
    força (em bolinhas) e RSI — igual ao formato original.
    """

    sinal = analise.get("sinal")

    emoji_titulo = "🟢" if sinal == "CALL" else "🔴"
    titulo = f"{sinal} CONFIRMADO"

    confianca = analise.get("confianca") or 0
    # int(x + 0.5), não round(): o round() do Python arredonda
    # 2.5 para 2 e 4.5 para 4 (regra do banqueiro), o que fazia
    # forças diferentes desenharem a mesma barra.
    preenchidas = max(0, min(5, int((confianca / 8) * 5 + 0.5)))
    forca_bolinhas = ("●" * preenchidas) + ("○" * (5 - preenchidas))

    rsi = analise.get("rsi")
    rsi_texto = str(rsi) if rsi is not None else "--"

    return (
        f"{emoji_titulo} <b>{titulo}</b> · {par}\n"
        "────────────\n"
        f"🏛 Mercado  {_mercado_do_par(par)}\n"
        f"⏰ Entrada  {analise.get('entrada', '--:--')}\n"
        f"⏱ Duração  M1\n\n"
        f"📈 Tendência  {analise.get('tendencia')}\n"
        f"⭐ Força  {forca_bolinhas} {confianca}\n"
        f"🩸 RSI  {rsi_texto}\n\n"
        "🔁 Martingale: até G2 (opcional)\n"
        "⚠️ <i>Alerta técnico e educacional.</i>"
    )


def escolher_imagem_sinal(sinal):
    return IMAGENS_TELEGRAM["call" if sinal == "CALL" else "put"]


def nivel_gale_atual(par, entrada_em):
    """Conta derrotas em velas IMEDIATAMENTE anteriores.

    A versão antiga só olhava "as 2 últimas derrotas do par",
    sem conferir QUANDO elas foram. Um LOSS de horas atrás
    fazia uma entrada nova, sem relação nenhuma, aparecer no
    grupo como "WIN 2G" — o aluno via um gale que nunca houve.

    Gale de verdade é vela colada na anterior. Se existir
    buraco de tempo, a sequência quebrou: é entrada nova.
    """

    if not _DB_PRONTO:
        return 0

    try:

        with _db_lock:

            conexao = _conectar_db()

            linhas = conexao.execute(
                """
                SELECT entrada_em, resultado FROM historico_sinais
                 WHERE par = ?
                   AND resultado IN ('WIN', 'LOSS')
                   AND entrada_em < ?
                   AND entrada_em >= ?
              ORDER BY entrada_em DESC
                 LIMIT 2
                """,
                (
                    str(par),
                    int(entrada_em),
                    int(entrada_em) - (2 * TIMEFRAME),
                ),
            ).fetchall()

            conexao.close()

    except Exception:

        return 0

    nivel = 0
    esperado = int(entrada_em) - TIMEFRAME

    for linha in linhas:

        # Buraco no tempo: não é sequência de gale.
        if int(linha["entrada_em"]) != esperado:
            break

        if linha["resultado"] != "LOSS":
            break

        nivel += 1
        esperado -= TIMEFRAME

    return nivel


def montar_resultado_telegram(par, sinal, entrada_em, resultado):
    """Devolve (caminho_da_imagem, legenda) pro resultado,
    já considerando em que Gale ele aconteceu.
    """

    nivel = nivel_gale_atual(par, entrada_em)

    hora_entrada = datetime.fromtimestamp(
        entrada_em,
        tz=FUSO_BR,
    ).strftime("%H:%M")

    if resultado == "WIN":

        if nivel == 0:
            imagem = IMAGENS_TELEGRAM["win"]
            titulo = "WIN"
            rodape = ""
        elif nivel == 1:
            imagem = IMAGENS_TELEGRAM["win_g1"]
            titulo = "WIN 1G"
            rodape = "\n🔥 Recuperado no gale 1."
        else:
            imagem = IMAGENS_TELEGRAM["win_g2"]
            titulo = "WIN 2G"
            rodape = "\n🔥 Recuperado no gale 2."

        recuperado = (
            f"🔁 Recuperado em G{nivel}\n" if nivel > 0 else ""
        )

        caption = (
            f"✅ <b>{titulo}</b> · {par}\n"
            "────────────\n"
            f"🔁 Entrada das {hora_entrada}\n"
            f"🎯 Direção {sinal} · M1\n"
            f"{recuperado}"
            f"{rodape}"
        )

        return imagem, caption

    # LOSS
    imagem = IMAGENS_TELEGRAM["loss"]

    # A mensagem antiga prometia "Segue pro Gale 1", mas o robô
    # NÃO emite sinal de gale. O aluno ficava esperando uma
    # entrada que nunca chegava. O texto agora diz só o que é
    # verdade: o gale existe, é opcional e é decisão dele.
    if nivel < 2:
        rodape = "\n🔁 Martingale: até G2 (opcional)"
    else:
        rodape = "\n⛔ Sequência encerrada no Gale 2."

    caption = (
        f"❌ <b>LOSS</b> · {par}\n"
        "────────────\n"
        f"🔁 Entrada das {hora_entrada}\n"
        f"🎯 Direção {sinal} · M1"
        f"{rodape}"
    )

    return imagem, caption

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
# BANCO DE HISTÓRICO (SQLite)
# ============================================================
#
# Guarda o CONTEXTO de cada sinal no momento em que ele nasce,
# e depois grava o resultado quando o candle de entrada fecha.
#
# Por que SQLite e não JSON:
#   - grava sem reescrever o arquivo inteiro
#   - a chave única impede resultado duplicado
#   - aguenta duas requisições ao mesmo tempo
#
# ATENÇÃO SOBRE PERSISTÊNCIA NO RENDER:
#   No plano gratuito o disco é efêmero. O histórico sobrevive
#   a reinícios do worker, mas é APAGADO a cada novo deploy.
#   Para histórico permanente é preciso anexar um disco no
#   Render e apontar HISTORICO_DB para dentro dele.
#   Ex.: HISTORICO_DB=/var/data/historico.db
# ============================================================

CAMINHO_DB = os.getenv(
    "HISTORICO_DB",
    "historico_sinais.db"
)

_db_lock = threading.Lock()

# Amostra mínima antes de o histórico poder influenciar.
# Abaixo disso, a estratégia técnica decide sozinha.
MINIMO_AMOSTRA = 15

# Abaixo desta taxa, com amostra suficiente, o sinal é
# bloqueado. 53,8% é o ponto de equilíbrio com pagamento
# de 86%; 45% é uma margem tolerante abaixo disso.
TAXA_BLOQUEIO = 45.0

# Acima desta taxa a combinação recebe reforço.
TAXA_REFORCO = 58.0

# Teto do ajuste. O histórico ajuda ou atrapalha, nunca manda.
AJUSTE_MAXIMO = 2

# Quantos registros recentes entram na conta.
JANELA_HISTORICO = 200

# Registros mais novos que isto pesam o dobro.
RECENTES_PESO_DOBRO = 50


def _conectar_db():
    """Abre a conexão do SQLite já configurada.

    check_same_thread=False porque o Flask atende em threads
    diferentes. O acesso é serializado por _db_lock.
    """

    conexao = sqlite3.connect(
        CAMINHO_DB,
        timeout=10,
        check_same_thread=False,
    )

    conexao.row_factory = sqlite3.Row

    return conexao


def iniciar_db():
    """Cria a tabela se ela ainda não existir.

    Nunca levanta exceção: se o disco for somente leitura, o
    sistema segue funcionando sem aprendizado.
    """

    try:

        with _db_lock:

            conexao = _conectar_db()

            conexao.execute("""
                CREATE TABLE IF NOT EXISTS historico_sinais (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    par          TEXT    NOT NULL,
                    entrada_em   INTEGER NOT NULL,
                    sinal        TEXT    NOT NULL,
                    pontos_call  INTEGER,
                    pontos_put   INTEGER,
                    diferenca    INTEGER,
                    forca        TEXT,
                    tendencia    TEXT,
                    rsi          REAL,
                    mhi          TEXT,
                    rompimento   TEXT,
                    pullback     TEXT,
                    fibo         TEXT,
                    pivo         TEXT,
                    hora         TEXT,
                    resultado    TEXT,
                    criado_em    INTEGER,
                    resolvido_em INTEGER,
                    UNIQUE (par, entrada_em, sinal)
                )
            """)

            conexao.execute("""
                CREATE INDEX IF NOT EXISTS idx_combinacao
                ON historico_sinais (par, sinal, forca, resultado)
            """)

            conexao.commit()
            conexao.close()

        return True

    except Exception:

        return False


_DB_PRONTO = iniciar_db()


def faixa_forca(pontos_call, pontos_put):
    """Agrupa a diferença de pontos em faixas.

    Sem isso cada placar (7x1, 8x2...) viraria uma combinação
    diferente e nunca juntaria amostra suficiente.
    """

    try:
        diferenca = abs(int(pontos_call) - int(pontos_put))
    except Exception:
        return "FRACA"

    if diferenca >= 7:
        return "MUITO_FORTE"

    if diferenca >= 5:
        return "FORTE"

    if diferenca >= 3:
        return "MEDIA"

    return "FRACA"


def registrar_sinal(par, analise):
    """Guarda o contexto de um sinal recém-gerado.

    Só grava CALL/PUT: AGUARDANDO não tem o que conferir.
    A chave única (par, entrada_em, sinal) impede duplicata
    quando o mesmo candle é buscado mais de uma vez.

    Retorna True SÓ quando a linha é realmente NOVA (inserida
    agora pela primeira vez). Uma tentativa repetida do mesmo
    candle/sinal (o painel gira e rebusca o mesmo par) devolve
    False, mesmo sem erro — isso é o que permite notificar o
    Telegram uma única vez por sinal, nunca a cada rotação.
    """

    if not _DB_PRONTO:
        return False

    try:

        sinal = analise.get("sinal")
        entrada_em = analise.get("entrada_em")

        if sinal not in ("CALL", "PUT") or not entrada_em:
            return False

        mhi = analise.get("mhi") or {}

        with _db_lock:

            conexao = _conectar_db()

            cursor = conexao.execute(
                """
                INSERT OR IGNORE INTO historico_sinais (
                    par, entrada_em, sinal,
                    pontos_call, pontos_put, diferenca, forca,
                    tendencia, rsi, mhi, rompimento, pullback,
                    fibo, pivo, hora, resultado, criado_em
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, ?
                )
                """,
                (
                    str(par),
                    int(entrada_em),
                    str(sinal),
                    int(analise.get("pontos_call") or 0),
                    int(analise.get("pontos_put") or 0),
                    abs(
                        int(analise.get("pontos_call") or 0)
                        - int(analise.get("pontos_put") or 0)
                    ),
                    faixa_forca(
                        analise.get("pontos_call"),
                        analise.get("pontos_put"),
                    ),
                    str(analise.get("tendencia") or ""),
                    (
                        float(analise.get("rsi"))
                        if analise.get("rsi") is not None
                        else None
                    ),
                    str(mhi.get("direcao") or ""),
                    str(analise.get("rompimento") or ""),
                    str(analise.get("pullback") or ""),
                    str(analise.get("fibo") or ""),
                    str(analise.get("pivo") or ""),
                    str(analise.get("hora") or ""),
                    int(time.time()),
                ),
            )

            conexao.commit()
            linha_nova = cursor.rowcount > 0
            conexao.close()

        return linha_nova

    except Exception:

        # Falha no histórico nunca pode derrubar a rota.
        return False


def registrar_resultado_historico(par, entrada_em, sinal, resultado):
    """Grava WIN/LOSS no sinal correspondente.

    Regras:
      - só atualiza linha cujo resultado ainda é NULL, então
        um resultado antigo NUNCA é modificado;
      - a linha é achada pela chave exata do sinal, então não
        há como um resultado cair no sinal errado;
      - consultar o mesmo resultado várias vezes não duplica.
    """

    if not _DB_PRONTO:
        return False

    if resultado not in ("WIN", "LOSS"):
        return False

    try:

        with _db_lock:

            conexao = _conectar_db()

            cursor = conexao.execute(
                """
                UPDATE historico_sinais
                   SET resultado = ?, resolvido_em = ?
                 WHERE par = ?
                   AND entrada_em = ?
                   AND sinal = ?
                   AND resultado IS NULL
                """,
                (
                    resultado,
                    int(time.time()),
                    str(par),
                    int(entrada_em),
                    str(sinal),
                ),
            )

            conexao.commit()
            alterou = cursor.rowcount > 0
            conexao.close()

        return alterou

    except Exception:

        return False


def estatistica_combinacao(par, sinal, forca):
    """Taxa histórica de uma combinação par + direção + força.

    Os registros mais recentes pesam o dobro, para o sistema
    acompanhar mudanças de mercado sem esquecer o passado.
    """

    vazio = {
        "amostra": 0,
        "wins": 0,
        "losses": 0,
        "taxa": None,
    }

    if not _DB_PRONTO:
        return vazio

    try:

        with _db_lock:

            conexao = _conectar_db()

            linhas = conexao.execute(
                """
                SELECT resultado FROM historico_sinais
                 WHERE par = ? AND sinal = ? AND forca = ?
                   AND resultado IN ('WIN', 'LOSS')
              ORDER BY id DESC
                 LIMIT ?
                """,
                (str(par), str(sinal), str(forca),
                 int(JANELA_HISTORICO)),
            ).fetchall()

            conexao.close()

    except Exception:

        return vazio

    if not linhas:
        return vazio

    wins = 0
    losses = 0
    peso_win = 0.0
    peso_total = 0.0

    for posicao, linha in enumerate(linhas):

        # As primeiras linhas são as mais novas (ORDER BY DESC).
        peso = 2.0 if posicao < RECENTES_PESO_DOBRO else 1.0

        peso_total += peso

        if linha["resultado"] == "WIN":
            wins += 1
            peso_win += peso
        else:
            losses += 1

    taxa = (peso_win / peso_total) * 100 if peso_total else None

    return {
        "amostra": wins + losses,
        "wins": wins,
        "losses": losses,
        "taxa": round(taxa, 1) if taxa is not None else None,
    }


def ajuste_historico(par, sinal, pontos_call, pontos_put):
    """Quanto o histórico reforça ou enfraquece esta direção.

    Devolve o ajuste em pontos (limitado por AJUSTE_MAXIMO),
    a estatística usada e se a combinação deve ser bloqueada.

    O ajuste NUNCA cria um sinal: ele é aplicado apenas depois
    que a análise técnica já confirmou uma direção.
    """

    forca = faixa_forca(pontos_call, pontos_put)

    est = estatistica_combinacao(par, sinal, forca)

    resposta = {
        "ajuste": 0,
        "bloquear": False,
        "forca": forca,
        "amostra": est["amostra"],
        "taxa": est["taxa"],
        "motivo": "SEM_AMOSTRA",
    }

    if est["amostra"] < MINIMO_AMOSTRA or est["taxa"] is None:
        return resposta

    taxa = est["taxa"]

    if taxa < TAXA_BLOQUEIO:
        resposta["ajuste"] = -AJUSTE_MAXIMO
        resposta["bloquear"] = True
        resposta["motivo"] = "HISTORICO_RUIM"
        return resposta

    if taxa < 50.0:
        resposta["ajuste"] = -1
        resposta["motivo"] = "HISTORICO_ABAIXO_DA_MEDIA"
        return resposta

    if taxa >= TAXA_REFORCO:
        resposta["ajuste"] = AJUSTE_MAXIMO
        resposta["motivo"] = "HISTORICO_BOM"
        return resposta

    resposta["motivo"] = "HISTORICO_NEUTRO"
    return resposta


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

# POOL DE THREADS COM RECICLAGEM
#
# Quando a biblioteca da IQ Option trava, a thread é abandonada
# pelo timeout — mas ela NUNCA morre. Ela fica ocupando uma vaga
# do pool para sempre.
#
# Com o tempo as vagas acabam, toda busca passa a esperar por
# uma thread livre que nunca aparece, o gunicorn mata o worker
# e o serviço responde 502.
#
# A solução é contar as travadas e, ao passar do limite, jogar
# o pool fora e criar outro. As threads velhas continuam
# penduradas, mas não atrapalham mais ninguém.

MAX_WORKERS_POOL = 12

# Quantas threads podem ficar penduradas antes de reciclar.
LIMITE_THREADS_TRAVADAS = 6

_executor_candles = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_WORKERS_POOL
)

_threads_travadas = 0
_pool_lock = threading.Lock()


def registrar_thread_travada():
    """Conta uma thread abandonada e recicla o pool se preciso."""

    global _executor_candles
    global _threads_travadas

    with _pool_lock:

        _threads_travadas += 1

        if _threads_travadas < LIMITE_THREADS_TRAVADAS:
            return False

        antigo = _executor_candles

        _executor_candles = concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_WORKERS_POOL
        )

        _threads_travadas = 0

    # shutdown sem esperar: as threads travadas seguem
    # penduradas, mas o pool novo já está livre.
    try:
        antigo.shutdown(wait=False)
    except Exception:
        pass

    # Sessão nova também, já que a antiga é a causa provável.
    invalidar_conexao()

    return True

def conectar_com_timeout(timeout_segundos=12):
    """Conecta com limite de tempo.

    conectar() pode ficar pendurada dentro da biblioteca da
    corretora — em check_connect(), no handshake ou numa
    reconexão que nunca termina. Sem limite, a requisição
    inteira estoura o tempo do Render e volta 504, mesmo com
    o orçamento de tempo da busca de candles funcionando.

    Se estourar, a conexão é invalidada para a próxima
    tentativa começar do zero.
    """

    futuro = _executor_candles.submit(conectar)

    try:

        return futuro.result(timeout=timeout_segundos)

    except concurrent.futures.TimeoutError:

        invalidar_conexao()
        registrar_thread_travada()

        raise TimeoutError(
            "Conexao com a corretora demorou mais de "
            f"{timeout_segundos}s e foi abandonada."
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

        registrar_thread_travada()

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


def analisar_sinal(candles, par=None):
    """Analisa os candles e devolve o sinal.

    O parâmetro `par` é opcional e serve só para consultar o
    histórico daquela combinação. Sem ele, a função se comporta
    exatamente como antes, sem aprendizado.
    """

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
            "ajuste_historico_call": 0,
            "ajuste_historico_put": 0,
            "taxa_historica": None,
            "amostra_historica": 0,
            "qualidade_sinal": "AGUARDANDO",
            "motivo_filtro": "POUCOS_DADOS",
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
            "ajuste_historico_call": 0,
            "ajuste_historico_put": 0,
            "taxa_historica": None,
            "amostra_historica": 0,
            "confirmacoes_call": [],
            "confirmacoes_put": [],
            "qualidade_sinal": "AGUARDANDO",
            "motivo_filtro": "MERCADO_FECHADO",
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

    # --------------------------------------------------------
    # DECISÃO TÉCNICA (base, sem histórico)
    # --------------------------------------------------------
    #
    # O histórico NÃO participa desta etapa. Ele só entra
    # depois, para reforçar ou enfraquecer uma direção que a
    # análise técnica já confirmou. Assim o aprendizado nunca
    # cria um CALL ou PUT sozinho.

    direcao_base = None

    if (
        pontos_call >= PONTUACAO_MINIMA
        and pontos_call - pontos_put >= DIFERENCA_MINIMA
    ):
        direcao_base = "CALL"

    elif (
        pontos_put >= PONTUACAO_MINIMA
        and pontos_put - pontos_call >= DIFERENCA_MINIMA
    ):
        direcao_base = "PUT"

    # --------------------------------------------------------
    # AJUSTE PELO HISTÓRICO
    # --------------------------------------------------------

    ajuste_call = 0
    ajuste_put = 0
    taxa_historica = None
    amostra_historica = 0
    motivo_filtro = "SEM_CONFIRMACAO_TECNICA"

    if direcao_base and par:

        try:
            info = ajuste_historico(
                par,
                direcao_base,
                pontos_call,
                pontos_put,
            )
        except Exception:
            info = {
                "ajuste": 0,
                "bloquear": False,
                "amostra": 0,
                "taxa": None,
                "motivo": "ERRO_HISTORICO",
            }

        taxa_historica = info.get("taxa")
        amostra_historica = info.get("amostra", 0)
        motivo_filtro = info.get("motivo", "SEM_AMOSTRA")

        if direcao_base == "CALL":
            ajuste_call = info.get("ajuste", 0)
        else:
            ajuste_put = info.get("ajuste", 0)

        if info.get("bloquear"):

            # Combinação com histórico ruim e amostra
            # suficiente: melhor não operar do que operar mal.
            direcao_base = None
            status = "AGUARDE — HISTÓRICO DESFAVORÁVEL"
            motivo_filtro = "BLOQUEADO_HISTORICO_RUIM"

    # Pontuação final, já com o ajuste aplicado.
    pontos_call_final = max(0, pontos_call + ajuste_call)
    pontos_put_final = max(0, pontos_put + ajuste_put)

    # --------------------------------------------------------
    # CONFIRMAÇÃO FINAL
    # --------------------------------------------------------

    if direcao_base == "CALL":

        # O ajuste negativo pode derrubar um sinal fraco.
        if (
            pontos_call_final >= PONTUACAO_MINIMA
            and pontos_call_final - pontos_put_final
            >= DIFERENCA_MINIMA
        ):

            sinal = "CALL"
            status = "CONFIRMAÇÃO DE ALTA"

            # NÃO é probabilidade de acerto. É apenas a soma
            # técnica dos indicadores que concordaram.
            confianca = pontos_call_final

        else:
            status = "AGUARDE — ENFRAQUECIDO PELO HISTÓRICO"
            motivo_filtro = "ENFRAQUECIDO_PELO_HISTORICO"

    elif direcao_base == "PUT":

        if (
            pontos_put_final >= PONTUACAO_MINIMA
            and pontos_put_final - pontos_call_final
            >= DIFERENCA_MINIMA
        ):

            sinal = "PUT"
            status = "CONFIRMAÇÃO DE BAIXA"

            confianca = pontos_put_final

        else:
            status = "AGUARDE — ENFRAQUECIDO PELO HISTÓRICO"
            motivo_filtro = "ENFRAQUECIDO_PELO_HISTORICO"

    if sinal in ("CALL", "PUT"):
        motivo_filtro = "APROVADO"

    # Classificação de qualidade — descritiva, não é promessa.
    if sinal in ("CALL", "PUT"):

        diferenca_final = abs(
            pontos_call_final - pontos_put_final
        )

        if diferenca_final >= 7:
            qualidade_sinal = "ALTA"
        elif diferenca_final >= 5:
            qualidade_sinal = "MEDIA"
        else:
            qualidade_sinal = "BAIXA"

    else:
        qualidade_sinal = "AGUARDANDO"

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
            pontos_call_final,

        "pontos_put":
            pontos_put_final,

        # Pontuação técnica pura, antes do histórico.
        "pontos_call_base":
            pontos_call,

        "pontos_put_base":
            pontos_put,

        "ajuste_historico_call":
            ajuste_call,

        "ajuste_historico_put":
            ajuste_put,

        # Frequência histórica desta combinação. NÃO é
        # probabilidade garantida do próximo sinal.
        "taxa_historica":
            taxa_historica,

        "amostra_historica":
            amostra_historica,

        "qualidade_sinal":
            qualidade_sinal,

        "motivo_filtro":
            motivo_filtro,

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

        iq = conectar_com_timeout()

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
# HISTÓRICO E APRENDIZADO
# ============================================================
#
# Mostra o que o sistema aprendeu até agora. Útil para conferir
# se o filtro está bloqueando alguma combinação e por quê.
#
# Uso:  /historico              -> resumo por combinação
#       /historico?par=EURUSD-OTC
# ============================================================

@app.get("/historico")
def ver_historico():

    if not _DB_PRONTO:

        return jsonify({
            "ok": False,
            "erro": (
                "Banco de histórico indisponível "
                "(disco somente leitura?)."
            ),
        }), 200

    par_filtro = (
        request.args.get("par", "")
        .strip()
        .upper()
    )

    try:

        with _db_lock:

            conexao = _conectar_db()

            if par_filtro:
                linhas = conexao.execute(
                    """
                    SELECT par, sinal, forca,
                           COUNT(*) AS total,
                           SUM(resultado = 'WIN') AS wins,
                           SUM(resultado = 'LOSS') AS losses
                      FROM historico_sinais
                     WHERE resultado IN ('WIN','LOSS')
                       AND par = ?
                  GROUP BY par, sinal, forca
                  ORDER BY total DESC
                    """,
                    (par_filtro,),
                ).fetchall()
            else:
                linhas = conexao.execute(
                    """
                    SELECT par, sinal, forca,
                           COUNT(*) AS total,
                           SUM(resultado = 'WIN') AS wins,
                           SUM(resultado = 'LOSS') AS losses
                      FROM historico_sinais
                     WHERE resultado IN ('WIN','LOSS')
                  GROUP BY par, sinal, forca
                  ORDER BY total DESC
                    """
                ).fetchall()

            pendentes = conexao.execute(
                """
                SELECT COUNT(*) AS n FROM historico_sinais
                 WHERE resultado IS NULL
                """
            ).fetchone()

            conexao.close()

    except Exception as erro:

        return jsonify({
            "ok": False,
            "erro": str(erro)[:200],
        }), 200

    combinacoes = []

    for linha in linhas:

        total = linha["total"] or 0
        wins = linha["wins"] or 0

        taxa = round((wins / total) * 100, 1) if total else None

        combinacoes.append({
            "par": linha["par"],
            "sinal": linha["sinal"],
            "forca": linha["forca"],
            "amostra": total,
            "wins": wins,
            "losses": linha["losses"] or 0,
            "taxa": taxa,
            "influencia": (
                "sem amostra suficiente"
                if total < MINIMO_AMOSTRA
                else "bloqueia"
                if taxa is not None and taxa < TAXA_BLOQUEIO
                else "reforça"
                if taxa is not None and taxa >= TAXA_REFORCO
                else "neutra"
            ),
        })

    return jsonify({
        "ok": True,
        "aviso": (
            "Taxa histórica é frequência do passado, "
            "não probabilidade garantida do próximo sinal."
        ),
        "regras": {
            "minimo_amostra": MINIMO_AMOSTRA,
            "taxa_bloqueio": TAXA_BLOQUEIO,
            "taxa_reforco": TAXA_REFORCO,
            "ajuste_maximo": AJUSTE_MAXIMO,
            "janela": JANELA_HISTORICO,
        },
        "aguardando_resultado": (
            pendentes["n"] if pendentes else 0
        ),
        "combinacoes": combinacoes,
    })


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

        iq = conectar_com_timeout()

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

    # Grava o resultado no histórico.
    #
    # O UPDATE só atinge a linha daquele par + entrada + sinal,
    # e só se ela ainda estiver sem resultado. Por isso:
    #   - consultar várias vezes não duplica;
    #   - um resultado antigo nunca é alterado;
    #   - o resultado não cai no sinal errado.
    gravado = False

    if m1 in ("WIN", "LOSS"):
        try:
            gravado = registrar_resultado_historico(
                par,
                inicio_candle,
                sinal,
                m1,
            )
        except Exception:
            gravado = False

        # Só manda o Telegram na primeira vez que este
        # resultado é gravado (gravado=True). Essa rota pode
        # ser chamada várias vezes em segundo plano pro mesmo
        # sinal — sem essa checagem o WIN/LOSS repetiria a
        # cada consulta.
        if gravado:
            try:
                imagem, caption = montar_resultado_telegram(
                    par,
                    sinal,
                    inicio_candle,
                    m1,
                )
                enviar_telegram_foto(imagem, caption)
            except Exception:
                pass

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
        "historico_gravado": gravado,
    })


# ============================================================
# UM PAR
# ============================================================

@app.get("/candles/<par>")
def candles_par(par):

    inicio = time.time()

    par = par.strip().upper()

    try:

        iq = conectar_com_timeout()

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
            par=par
        )

        try:
            sinal_e_novo = registrar_sinal(par, analise)
            if sinal_e_novo and analise.get("sinal") in ("CALL", "PUT"):
                enviar_telegram_foto(
                    escolher_imagem_sinal(analise.get("sinal")),
                    montar_caption_sinal(par, analise),
                )
        except Exception:
            pass

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

            "ajuste_historico_call":
                analise.get("ajuste_historico_call", 0),

            "ajuste_historico_put":
                analise.get("ajuste_historico_put", 0),

            "taxa_historica":
                analise.get("taxa_historica"),

            "amostra_historica":
                analise.get("amostra_historica", 0),

            "qualidade_sinal":
                analise.get("qualidade_sinal", "AGUARDANDO"),

            "motivo_filtro":
                analise.get("motivo_filtro"),

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

        iq = conectar_com_timeout()

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

        iq = conectar_com_timeout()

        pares_param = request.args.get(
            "pares",
            ""
        ).strip()

        # Qual mercado usar quando NÃO vier ?pares= explícito.
        #
        # PADRÃO (sem ?mercado= ou com "todos"): OTC + Forex
        # juntos, rotacionando entre os dois. OTC garante que
        # sempre tem sinal aparecendo (roda 24h); Forex entra
        # quando o mercado real está aberto e some sozinho
        # (via checagem de MERCADO FECHADO em analisar_sinal)
        # quando não está.
        #
        # "otc"   -> só os pares sintéticos.
        # "forex" -> só o mercado real.
        # "acoes" -> só ações.
        mercado = request.args.get(
            "mercado",
            "todos"
        ).strip().lower()

        if mercado == "forex":
            lista_base = PARES_FOREX
        elif mercado == "acoes":
            lista_base = PARES_ACOES
        elif mercado == "otc":
            lista_base = PARES
        else:
            # "todos" ou qualquer valor não reconhecido:
            # combina OTC + Forex.
            lista_base = PARES + PARES_FOREX

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

        # PARES FIXOS
        #
        # Diferente de ?pares=, que substitui a rotação, o
        # ?fixos= apenas GARANTE que aqueles pares venham na
        # resposta. O restante do grupo continua girando
        # normalmente.
        #
        # Serve para o painel travar um par que está no meio de
        # uma sequência de gale: sem isso o card sumiria da
        # tela na próxima rotação e não haveria como continuar
        # a recuperação naquele mesmo ativo.
        fixos_param = request.args.get(
            "fixos",
            ""
        ).strip()

        fixos = [
            p.strip().upper()
            for p in fixos_param.split(",")
            if p.strip()
        ]

        # Só aceita pares que realmente existem na lista do
        # mercado, para o parâmetro não virar porta de entrada
        # para ativo inválido.
        fixos = [p for p in fixos if p in lista_base]

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

        # Junta os pares fixos na frente, sem repetir.
        # Eles entram primeiro porque são os que o painel
        # precisa manter na tela (sequência de gale aberta).
        if fixos:

            pares = fixos + [
                p for p in pares if p not in fixos
            ]

        # Segurança extra: nunca busca mais que 5 pares
        # numa chamada só, mesmo se pedirem explicitamente
        # mais que isso via ?pares= ou ?fixos=.
        #
        # Como os fixos vêm na frente, eles nunca são cortados.

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

        # Orçamento apertado de propósito.
        #
        # A conexão já pode ter consumido até 12s antes de
        # chegar aqui. Somando, o pior caso fica em torno de
        # 30s — dentro do limite do Render, que devolve 504
        # quando a resposta demora demais.
        ORCAMENTO_TOTAL = 18
        TIMEOUT_POR_PAR = 6

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
                    par=par
                )

                # Guarda o contexto do sinal para o histórico.
                # Falhar aqui não pode derrubar a resposta.
                # Só notifica o Telegram quando é linha NOVA,
                # pra rotação repetida do mesmo par/candle não
                # mandar o mesmo sinal de novo.
                try:
                    sinal_e_novo = registrar_sinal(par, analise)
                    if (
                        sinal_e_novo
                        and analise.get("sinal") in ("CALL", "PUT")
                    ):
                        enviar_telegram_foto(
                            escolher_imagem_sinal(analise.get("sinal")),
                            montar_caption_sinal(par, analise),
                        )
                except Exception:
                    pass

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

                    "ajuste_historico_call":
                        analise.get("ajuste_historico_call", 0),

                    "ajuste_historico_put":
                        analise.get("ajuste_historico_put", 0),

                    "taxa_historica":
                        analise.get("taxa_historica"),

                    "amostra_historica":
                        analise.get("amostra_historica", 0),

                    "qualidade_sinal":
                        analise.get("qualidade_sinal", "AGUARDANDO"),

                    "motivo_filtro":
                        analise.get("motivo_filtro"),

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
                        iq = conectar_com_timeout()
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
# TRABALHO AUTOMÁTICO — SINAIS + RESULTADOS
# ============================================================
#
# O navegador antes fazia duas coisas importantes:
#   1) consultava /candles para descobrir sinais novos;
#   2) consultava /resultado/<par> para fechar os sinais.
#
# Isso fazia o Telegram parar quando o navegador era fechado.
# Este worker faz as mesmas chamadas internamente, sem depender
# do navegador. O cron do Render em / apenas mantém o serviço
# acordado; este worker é quem executa o trabalho.
#
# A proteção do SQLite já existente impede sinal duplicado e a
# atualização do resultado só acontece uma vez.
# ============================================================

_WORKER_ATIVO = False
_WORKER_LOCK = threading.Lock()

# ------------------------------------------------------------
# UM WORKER SÓ — TRAVA DE ARQUIVO
# ------------------------------------------------------------
# _WORKER_ATIVO é uma variável de memória, e memória NÃO é
# compartilhada entre processos. Se o gunicorn subir com mais
# de um worker, cada um cria o seu loop e o grupo recebe a
# mesma mensagem duas ou três vezes.
#
# A trava abaixo é do sistema de arquivos: só um processo
# consegue segurá-la. Os outros seguem servindo HTTP normal,
# sem worker. Se o dono morrer, o sistema solta a trava e
# outro assume na próxima tentativa.
#
# CUIDADO COM O FORK: um arquivo aberto é HERDADO pelos
# processos filhos. Sem conferir o PID, todo worker nascido de
# um fork acharia que já tem a trava — porque vê o arquivo
# aberto pelo pai — e voltaríamos à duplicata.

ARQUIVO_TRAVA_WORKER = os.path.join(
    DIRETORIO_APP,
    "worker.lock",
)

_trava_worker_aberta = None
_trava_worker_pid = None


def sou_o_dono_do_worker():
    """True só para o processo que conquistou a trava."""

    global _trava_worker_aberta
    global _trava_worker_pid

    pid_atual = os.getpid()

    if (
        _trava_worker_aberta is not None
        and _trava_worker_pid == pid_atual
    ):
        return True

    # Herdada do pai pelo fork: não vale, descarta.
    if _trava_worker_aberta is not None:
        try:
            _trava_worker_aberta.close()
        except Exception:
            pass
        _trava_worker_aberta = None
        _trava_worker_pid = None

    try:

        import fcntl

        arquivo = open(ARQUIVO_TRAVA_WORKER, "w")

        try:
            fcntl.flock(arquivo, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            arquivo.close()
            return False

        arquivo.write(str(pid_atual))
        arquivo.flush()

        # Guardado de propósito para o arquivo NÃO fechar.
        # Fechar solta a trava.
        _trava_worker_aberta = arquivo
        _trava_worker_pid = pid_atual

        print("WORKER: este processo assumiu, pid", pid_atual)
        return True

    except Exception:

        # Sem fcntl ou outro problema: não trava nada, para
        # não deixar o serviço sem worker nenhum.
        return True


def _buscar_pendentes_worker():
    """Retorna sinais M1 ainda sem resultado e já fechados."""
    if not _DB_PRONTO:
        return []

    agora = int(time.time())
    limite = agora - TIMEFRAME

    try:
        with _db_lock:
            conexao = _conectar_db()
            linhas = conexao.execute(
                """
                SELECT par, entrada_em, sinal
                  FROM historico_sinais
                 WHERE resultado IS NULL
                   AND entrada_em <= ?
              ORDER BY entrada_em ASC
                 LIMIT 20
                """,
                (limite,),
            ).fetchall()
            conexao.close()

        return [
            (
                str(linha["par"]),
                int(linha["entrada_em"]),
                str(linha["sinal"]).upper(),
            )
            for linha in linhas
            if str(linha["sinal"]).upper() in ("CALL", "PUT")
        ]
    except Exception:
        return []


def _processar_resultados_worker():
    """Fecha automaticamente os sinais pendentes."""
    for par, entrada_em, sinal in _buscar_pendentes_worker():
        try:
            # Reutiliza exatamente a mesma lógica da rota pública.
            # Assim não criamos uma segunda regra de WIN/LOSS.
            caminho = (
                f"/resultado/{par}"
                f"?inicio={entrada_em}&sinal={sinal}"
            )
            with app.test_request_context(caminho):
                resultado_sinal(par)
        except Exception:
            pass


def _processar_sinais_worker():
    """Executa a mesma análise de pares usada pelo painel."""
    try:
        # OTC é o mercado contínuo usado para manter sinais
        # disponíveis também quando o Forex está fechado.
        with app.test_request_context("/candles?mercado=otc"):
            candles()
    except Exception:
        pass


def _loop_worker():
    """Loop único: primeiro fecha resultados, depois procura sinais."""
    global _WORKER_ATIVO

    with _WORKER_LOCK:
        if _WORKER_ATIVO:
            return
        _WORKER_ATIVO = True

    while True:
        try:
            _processar_resultados_worker()
            _processar_sinais_worker()
        except Exception:
            pass

        # Pequena pausa para não sobrecarregar a IQ Option/Render.
        time.sleep(5)


def iniciar_worker_automatico():
    """Sobe o worker — só no processo DONO, e só uma vez.

    Também recupera a thread se ela tiver morrido: com
    --preload as threads criadas antes da divisão em workers
    morrem no fork, sem deixar erro nenhum no log.
    """

    vivas = {
        t.name for t in threading.enumerate() if t.is_alive()
    }

    if "dw-academy-worker" in vivas:
        return

    with _WORKER_LOCK:

        # Confere de novo dentro do lock: outra requisição
        # pode ter subido a thread no meio do caminho.
        vivas = {
            t.name for t in threading.enumerate() if t.is_alive()
        }

        if "dw-academy-worker" in vivas:
            return

        if not sou_o_dono_do_worker():
            return

        try:
            thread = threading.Thread(
                target=_loop_worker,
                name="dw-academy-worker",
                daemon=True,
            )
            thread.start()
            print("WORKER: iniciado, pid", os.getpid())
        except Exception:
            pass


@app.before_request
def _garantir_worker():
    """Sobe o worker na primeira visita ao serviço.

    POR QUE NÃO NO CARREGAMENTO DO MÓDULO:
    com o gunicorn em --preload, o código de carregamento roda
    no processo PAI, antes da divisão em workers. Duas coisas
    dão errado ao mesmo tempo:

      1. A thread morre no fork e o robô fica mudo;
      2. O PAI fica segurando a trava para sempre, e nenhum
         worker consegue assumir.

    Aqui isto roda sempre dentro de um worker de verdade, a
    trava é disputada de forma limpa e apenas um vence.
    """
    try:
        iniciar_worker_automatico()
    except Exception:
        pass


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
