import os
import time

import requests
from flask import Flask, jsonify, request
from iqoptionapi.stable_api import IQ_Option


app = Flask(__name__)


# =========================================================
# CONFIGURAÇÃO
# =========================================================

TIMEFRAME = 60
CANDLE_COUNT = 100

PARES = [
    "EURUSD-OTC",
    "GBPUSD-OTC",
    "USDJPY-OTC",
    "EURJPY-OTC",
    "AUDUSD-OTC",
    "USDCAD-OTC",
    "GBPJPY-OTC",
    "EURGBP-OTC",
    "USDCHF-OTC",
    "AUDJPY-OTC",
    "NZDUSD-OTC",
    "EURCAD-OTC",
    "GBPAUD-OTC",
    "CADJPY-OTC",
    "EURAUD-OTC",
    "XAUUSD-OTC",
]


# =========================================================
# IQ OPTION
# =========================================================

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
        raise RuntimeError(
            "IQ_EMAIL e IQ_PASSWORD não configurados no Render."
        )

    if _iq is not None and getattr(_iq, "check_connect", False):
        return _iq

    _iq = IQ_Option(email, password)

    conectado, motivo = _iq.connect()

    if not conectado:
        _iq = None

        raise RuntimeError(
            f"Não foi possível conectar à IQ Option: {motivo}"
        )

    _ultima_conexao = int(time.time())

    return _iq


def buscar_candles(iq, par):
    timestamp = iq.get_server_timestamp()

    candles = iq.get_candles(
        par,
        TIMEFRAME,
        CANDLE_COUNT,
        timestamp,
    )

    if not candles:
        return []

    resultado = [
        normalizar_candle(candle)
        for candle in candles
    ]

    resultado.sort(
        key=lambda candle: candle["from"]
    )

    return resultado


# =========================================================
# TELEGRAM
# =========================================================

_telegram_chat_id = os.getenv(
    "TELEGRAM_CHAT_ID",
    "",
).strip()


def telegram_token():
    token = os.getenv(
        "TELEGRAM_BOT_TOKEN",
        "",
    ).strip()

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN não configurado no Render."
        )

    return token


def telegram_url(method):
    return (
        f"https://api.telegram.org/"
        f"bot{telegram_token()}/{method}"
    )


def telegram_api(method, payload=None):
    response = requests.post(
        telegram_url(method),
        json=payload or {},
        timeout=20,
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(
            data.get(
                "description",
                "Erro desconhecido da API do Telegram.",
            )
        )

    return data


def remover_webhook():
    """
    Remove webhook antigo para permitir getUpdates.
    Não apaga as mensagens pendentes.
    """

    return telegram_api(
        "deleteWebhook",
        {
            "drop_pending_updates": False,
        },
    )


def buscar_updates():
    """
    Busca atualizações pendentes do bot.
    """

    return telegram_api(
        "getUpdates",
        {
            "limit": 100,
            "timeout": 1,
            "allowed_updates": [
                "message",
                "channel_post",
            ],
        },
    )


def descobrir_chat_id():
    global _telegram_chat_id

    if _telegram_chat_id:
        return _telegram_chat_id

    # Primeiro garante que não existe webhook
    # impedindo o uso do getUpdates.
    remover_webhook()

    data = buscar_updates()

    updates = data.get(
        "result",
        [],
    )

    candidatos = []

    for update in updates:

        message = (
            update.get("message")
            or update.get("channel_post")
        )

        if not message:
            continue

        chat = message.get(
            "chat",
            {},
        )

        chat_type = chat.get("type")

        if chat_type in (
            "group",
            "supergroup",
        ):

            chat_id = chat.get("id")

            if chat_id is not None:

                candidatos.append(
                    {
                        "id": str(chat_id),
                        "titulo": chat.get(
                            "title",
                            "Grupo sem nome",
                        ),
                        "tipo": chat_type,
                    }
                )

    if not candidatos:
        return None

    # Usa o último grupo encontrado.
    ultimo = candidatos[-1]

    _telegram_chat_id = ultimo["id"]

    return _telegram_chat_id


def enviar_telegram(texto):
    chat_id = descobrir_chat_id()

    if not chat_id:
        raise RuntimeError(
            "Grupo não encontrado. "
            "Envie uma nova mensagem no grupo "
            "DW Trading — IQ Option e tente novamente."
        )

    return telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": texto,
        },
    )


# =========================================================
# ROTA PRINCIPAL
# =========================================================

@app.get("/")
def inicio():

    return jsonify(
        {
            "ok": True,
            "servico": (
                "Academy Trading - "
                "IQ Option Candles"
            ),
            "somente_dados": True,
            "operacao": False,
            "telegram": bool(
                os.getenv(
                    "TELEGRAM_BOT_TOKEN"
                )
            ),
        }
    )


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():

    return jsonify(
        {
            "ok": True,
            "servico": "iq-option-candles",
            "timestamp": int(
                time.time()
            ),
        }
    )


# =========================================================
# CANDLES IQ OPTION
# =========================================================

@app.get("/candles")
def candles():

    try:

        iq = conectar()

        pares_param = request.args.get(
            "pares",
            "",
        ).strip()

        if pares_param:

            pares = [
                p.strip().upper()
                for p in pares_param.split(",")
                if p.strip()
            ]

        else:

            pares = PARES

        pares = pares[:20]

        resultados = []

        for par in pares:

            try:

                dados = buscar_candles(
                    iq,
                    par,
                )

                resultados.append(
                    {
                        "par": par,
                        "timeframe": "M1",
                        "candles": dados,
                        "quantidade": len(
                            dados
                        ),
                    }
                )

            except Exception as erro:

                resultados.append(
                    {
                        "par": par,
                        "timeframe": "M1",
                        "candles": [],
                        "quantidade": 0,
                        "erro": str(erro),
                    }
                )

        return jsonify(
            {
                "ok": True,
                "fonte": "IQ Option",
                "somente_dados": True,
                "operacao": False,
                "timeframe": "M1",
                "timestamp": int(
                    time.time()
                ),
                "resultados": resultados,
            }
        )

    except Exception as erro:

        return jsonify(
            {
                "ok": False,
                "fonte": "IQ Option",
                "somente_dados": True,
                "operacao": False,
                "erro": str(erro),
            }
        ), 503


# =========================================================
# TELEGRAM STATUS
# =========================================================

@app.get("/telegram/status")
def telegram_status():

    return jsonify(
        {
            "ok": True,
            "telegram_token_configurado": bool(
                os.getenv(
                    "TELEGRAM_BOT_TOKEN"
                )
            ),
            "chat_id_configurado": bool(
                os.getenv(
                    "TELEGRAM_CHAT_ID",
                    "",
                ).strip()
            ),
            "bot": "DWTradingIQOptionBot",
        }
    )


# =========================================================
# TELEGRAM - VERIFICAR UPDATES
# =========================================================

@app.get("/telegram/updates")
def telegram_updates():

    try:

        remover_webhook()

        data = buscar_updates()

        grupos = []

        for update in data.get(
            "result",
            [],
        ):

            message = (
                update.get("message")
                or update.get("channel_post")
            )

            if not message:
                continue

            chat = message.get(
                "chat",
                {},
            )

            if chat.get("type") in (
                "group",
                "supergroup",
            ):

                grupos.append(
                    {
                        "chat_id": str(
                            chat.get("id")
                        ),
                        "titulo": chat.get(
                            "title"
                        ),
                        "tipo": chat.get(
                            "type"
                        ),
                    }
                )

        return jsonify(
            {
                "ok": True,
                "grupos_encontrados": grupos,
                "quantidade": len(grupos),
            }
        )

    except Exception as erro:

        return jsonify(
            {
                "ok": False,
                "erro": str(erro),
            }
        ), 503


# =========================================================
# TELEGRAM - TESTE
# =========================================================

@app.get("/telegram/test")
def telegram_test():

    try:

        texto = (
            "🤖 DW TRADING — TESTE IQ OPTION\n\n"
            "✅ Bot conectado com sucesso!\n"
            "📊 Grupo: DW Trading — IQ Option\n"
            "⏱️ Timeframe: M1\n"
            "🔐 Integração Telegram funcionando.\n\n"
            "Este é apenas um teste.\n"
            "Nenhuma operação foi realizada."
        )

        resultado = enviar_telegram(
            texto
        )

        return jsonify(
            {
                "ok": True,
                "mensagem": (
                    "Mensagem de teste enviada "
                    "ao Telegram."
                ),
                "chat_id_encontrado": (
                    _telegram_chat_id
                ),
                "telegram_result": resultado,
            }
        )

    except Exception as erro:

        return jsonify(
            {
                "ok": False,
                "mensagem": (
                    "Não foi possível enviar "
                    "o teste."
                ),
                "erro": str(erro),
            }
        ), 503


# =========================================================
# SERVIDOR
# =========================================================

if __name__ == "__main__":

    porta = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=porta,
    )
