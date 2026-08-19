import json
import os
import sys
import time

from iqoptionapi.stable_api import IQ_Option


def erro(mensagem, etapa, tipo="Error"):
    print(
        json.dumps(
            {
                "ok": False,
                "fonte": "IQ Option",
                "operacao": False,
                "somente_dados": True,
                "erro": mensagem,
                "etapa": etapa,
                "tipo_erro": tipo,
            },
            ensure_ascii=False,
        )
    )

    sys.exit(1)


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
                candle.get("high", 0)
            )
        ),
        "low": float(
            candle.get(
                "min",
                candle.get("low", 0)
            )
        ),
        "close": float(
            candle.get("close", 0)
        ),
        "volume": float(
            candle.get("volume", 0)
        ),
    }


def main():

    email = os.getenv("IQ_EMAIL")
    password = os.getenv("IQ_PASSWORD")

    if not email:
        erro(
            "IQ_EMAIL não configurado no Render.",
            "configuração",
            "MissingCredentials",
        )

    if not password:
        erro(
            "IQ_PASSWORD não configurado no Render.",
            "configuração",
            "MissingCredentials",
        )

    par = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "EURUSD-OTC"
    )

    try:

        timeframe = int(
            sys.argv[2]
            if len(sys.argv) > 2
            else 60
        )

    except ValueError:

        timeframe = 60

    try:

        count = int(
            sys.argv[3]
            if len(sys.argv) > 3
            else 100
        )

    except ValueError:

        count = 100

    iq = None

    ultimo_erro = None

    # ========================================================
    # TENTATIVAS DE CONEXÃO
    # ========================================================

    for tentativa in range(1, 4):

        try:

            iq = IQ_Option(
                email,
                password
            )

            conectado, motivo = iq.connect()

            if conectado:
                break

            ultimo_erro = str(motivo)

            iq = None

        except Exception as exc:

            ultimo_erro = str(exc)

            iq = None

        if tentativa < 3:
            time.sleep(3)

    if iq is None:

        erro(
            ultimo_erro
            or "Não foi possível conectar à IQ Option.",
            "conectando na IQ Option",
            "ConnectionError",
        )

    try:

        # ====================================================
        # OBTÉM HORÁRIO DO SERVIDOR
        # ====================================================

        try:

            timestamp = iq.get_server_timestamp()

        except Exception:

            timestamp = int(
                time.time()
            )

        # ====================================================
        # CANDLES
        # ====================================================

        candles = iq.get_candles(
            par,
            timeframe,
            count,
            timestamp,
        )

        if not candles:

            erro(
                "A IQ Option não retornou candles para "
                + par,
                "buscando candles",
                "EmptyCandles",
            )

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

        if not resultado:

            erro(
                "Nenhum candle válido foi recebido.",
                "normalizando candles",
                "EmptyCandles",
            )

        print(
            json.dumps(
                {
                    "ok": True,
                    "fonte": "IQ Option",
                    "operacao": False,
                    "somente_dados": True,
                    "par": par,
                    "timeframe": "M1",
                    "candles": resultado,
                },
                ensure_ascii=False,
            )
        )

    except Exception as exc:

        erro(
            str(exc),
            "buscando candles",
            type(exc).__name__,
        )

    finally:

        try:
            iq.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
