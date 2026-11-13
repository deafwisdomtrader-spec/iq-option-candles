# ============================================================
# MT4 -> TELEGRAM  (rota /mt4/sinal)
# ============================================================
#
# O EA "DWT_EnviarSinais" (MetaTrader 4) manda para ca os sinais
# dos indicadores DWT (Arrow Sniper, Sinais OTC, MHI, Resultado)
# e depois o resultado de cada entrada. Esta rota confere a senha
# e repassa para o Telegram, com as mesmas imagens do robo.
#
# Fica num arquivo separado (igual o auditor.py): se der qualquer
# problema aqui, o robo do Telegram continua funcionando normal.
#
# VARIAVEIS NO RENDER (Settings -> Environment):
#   MT4_TOKEN    -> senha secreta. A MESMA senha vai no EA do MT4.
#                   Sem ela a rota recusa tudo (so o teste responde).
#   MT4_DESTINO  -> para onde vao os sinais do MT4:
#                     vip    = grupo pago (TELEGRAM_CHAT_ID)   [padrao]
#                     gratis = grupo gratis (TELEGRAM_CHAT_ID_GRATIS)
#                     ambos  = os dois grupos
#                     nenhum = recebe, mas nao manda (para testar)
#   MT4_CHAT_ID  -> (opcional) id de outro grupo so para o MT4.
#                   Se existir, ganha do MT4_DESTINO.
#
# FORMATO QUE O EA MANDA (POST, JSON):
#   sinal:     {"token","tipo":"sinal","par","direcao":"CALL|PUT",
#               "tf":1,"indicador":"SNIPER","entrada":<epoch UTC>,"conta"}
#   resultado: {"token","tipo":"resultado","par","direcao","tf",
#               "indicador","entrada","resultado":"WIN|LOSS|EMPATE",
#               "etapa":0|1|2,"conta"}
#   teste:     {"teste":true, ...}  -> responde OK sem mandar nada
# ============================================================

import hmac
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request

FUSO_BR = timezone(timedelta(hours=-3))

NOMES_INDICADOR = {
    "SNIPER": "Arrow Sniper",
    "OTC": "Sinais OTC",
    "MHI": "MHI",
    "RES": "Resultado",
    "TESTE": "Teste",
}


def criar_mt4(enviar_foto, imagens, nome_bonito, chat_vip, chat_gratis):
    """Cria a rota /mt4/sinal.

    enviar_foto  -> enviar_telegram_foto(imagem, caption, chat_id) do app.py
    imagens      -> IMAGENS_TELEGRAM do app.py (call, put, win, loss...)
    nome_bonito  -> nome_bonito(par) do app.py
    """

    bp = Blueprint("mt4", __name__)

    token_certo = (os.getenv("MT4_TOKEN") or "").strip()
    destino = (os.getenv("MT4_DESTINO") or "vip").strip().lower()
    chat_proprio = (os.getenv("MT4_CHAT_ID") or "").strip()

    def _chats():
        if chat_proprio:
            return [chat_proprio]
        if destino == "nenhum":
            return []
        lista = []
        if destino in ("vip", "ambos") and chat_vip:
            lista.append(chat_vip)
        if destino in ("gratis", "ambos") and chat_gratis:
            lista.append(chat_gratis)
        return lista

    # Nao manda a mesma coisa duas vezes (o EA pode repetir se a
    # internet cair no meio). Guarda por 3 horas.
    vistos = {}
    trava = threading.Lock()

    def _ja_visto(chave):
        agora = time.time()
        with trava:
            for k in [k for k, t in vistos.items() if agora - t > 3 * 3600]:
                vistos.pop(k, None)
            if chave in vistos:
                return True
            vistos[chave] = agora
            return False

    # ---------------------------------------------------------
    def _par_bonito(par):
        p = str(par or "").upper().strip()
        # MT4 da corretora: "EURUSD-OTCi" -> "EURUSD-OTC"
        if p.endswith("-OTCI"):
            p = p[:-1]
        try:
            return nome_bonito(p)
        except Exception:
            return p

    def _mercado(par):
        return "OTC" if "OTC" in str(par).upper() else "FOREX"

    def _hora(entrada):
        try:
            return datetime.fromtimestamp(int(entrada), tz=FUSO_BR).strftime("%H:%M")
        except Exception:
            return "--:--"

    def _caption_sinal(d):
        sinal = d["direcao"]
        emoji = "🟢" if sinal == "CALL" else "🔴"
        ind = NOMES_INDICADOR.get(d["indicador"], d["indicador"])
        return (
            f"{emoji} <b>{sinal} CONFIRMADO</b> · {_par_bonito(d['par'])}\n"
            "────────────\n"
            f"🏛 Mercado  {_mercado(d['par'])}\n"
            f"⏰ Entrada  {_hora(d['entrada'])}\n"
            f"⏱ Duração  M{d['tf']}\n\n"
            f"📊 Indicador  {ind} (MT4)\n\n"
            "🔁 Martingale: até G2 (opcional)\n"
            "⚠️ <i>Alerta técnico e educacional.</i>"
        )

    def _resultado(d):
        par = _par_bonito(d["par"])
        hora = _hora(d["entrada"])
        sinal = d["direcao"]
        tf = d["tf"]
        res = d["resultado"]
        etapa = d["etapa"]

        if res == "WIN":
            if etapa == 0:
                img, titulo, linha, rod = imagens.get("win"), "WIN", "", "\n🔥 Entrada vencedora."
            elif etapa == 1:
                img, titulo, linha, rod = imagens.get("win_g1"), "WIN 1G", "🔁 Recuperado em G1\n", "\n🔥 Recuperado no gale 1."
            else:
                img, titulo, linha, rod = imagens.get("win_g2"), "WIN 2G", "🔁 Recuperado em G2\n", "\n🔥 Recuperado no gale 2."
            return img, (
                f"✅ <b>{titulo}</b> · {par}\n"
                "────────────\n"
                f"↩️ Entrada das {hora}\n"
                f"📌 Direção {sinal} · M{tf}\n"
                f"{linha}{rod}"
            )

        if res == "EMPATE":
            return imagens.get("empate"), (
                f"⚪ <b>EMPATE</b> · {par}\n"
                "────────────\n"
                f"↩️ Entrada das {hora}\n"
                f"📌 Direção {sinal} · M{tf}\n"
                "\n➖ Vela fechou no mesmo preço."
            )

        etapas = {0: "entrada", 1: "entrada + G1"}.get(etapa, "entrada + G1 + G2")
        return imagens.get("loss"), (
            f"❌ <b>LOSS</b> · {par}\n"
            "────────────\n"
            f"↩️ Entrada das {hora}\n"
            f"📌 Direção {sinal} · M{tf}\n"
            f"🔁 Etapas  {etapas}\n"
            "\n⛔ Sequência encerrada."
        )

    # ---------------------------------------------------------
    @bp.get("/mt4/sinal")
    def mt4_status():
        """Abrir no navegador mostra se a rota esta pronta."""
        return jsonify({
            "ok": True,
            "rota": "/mt4/sinal",
            "senha_configurada": bool(token_certo),
            "destino": "MT4_CHAT_ID" if chat_proprio else destino,
            "grupos": len(_chats()),
        })

    @bp.post("/mt4/sinal")
    def mt4_sinal():
        d = request.get_json(silent=True) or {}

        # Teste do script DWT_TesteRender: responde OK, nao manda nada.
        if d.get("teste"):
            return jsonify({
                "ok": True,
                "teste": True,
                "senha_configurada": bool(token_certo),
                "mensagem": "Rota /mt4/sinal pronta.",
            })

        if not token_certo:
            return jsonify({"ok": False, "erro": "MT4_TOKEN nao configurado no Render"}), 503

        token = str(d.get("token") or request.headers.get("X-DWT-Token") or "")
        if not hmac.compare_digest(token, token_certo):
            return jsonify({"ok": False, "erro": "senha errada"}), 401

        tipo = str(d.get("tipo") or "").lower()
        direcao = str(d.get("direcao") or "").upper()
        par = str(d.get("par") or "").strip()
        indicador = str(d.get("indicador") or "MT4").upper()[:20]
        try:
            entrada = int(d.get("entrada") or 0)
            tf = int(d.get("tf") or 1)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "erro": "entrada/tf invalidos"}), 400

        if tipo not in ("sinal", "resultado") or direcao not in ("CALL", "PUT") or not par or entrada <= 0:
            return jsonify({"ok": False, "erro": "dados incompletos"}), 400

        # sinal com mais de 10 minutos de atraso nao vai (entrada ja passou)
        if tipo == "sinal" and time.time() - entrada > 600:
            return jsonify({"ok": False, "erro": "sinal velho, nao enviado"}), 200

        dados = {"par": par, "direcao": direcao, "tf": tf, "indicador": indicador, "entrada": entrada}

        if tipo == "sinal":
            chave = f"S|{par}|{entrada}|{indicador}"
            if _ja_visto(chave):
                return jsonify({"ok": True, "repetido": True})
            img = imagens.get("call" if direcao == "CALL" else "put")
            caption = _caption_sinal(dados)
        else:
            res = str(d.get("resultado") or "").upper()
            if res not in ("WIN", "LOSS", "EMPATE"):
                return jsonify({"ok": False, "erro": "resultado invalido"}), 400
            try:
                etapa = max(0, min(2, int(d.get("etapa") or 0)))
            except (TypeError, ValueError):
                etapa = 0
            dados["resultado"] = res
            dados["etapa"] = etapa
            chave = f"R|{par}|{entrada}|{indicador}"
            if _ja_visto(chave):
                return jsonify({"ok": True, "repetido": True})
            img, caption = _resultado(dados)

        chats = _chats()
        for chat in chats:
            enviar_foto(img, caption, chat_id=chat)

        print("MT4:", tipo, indicador, par, direcao, _hora(entrada), "->", len(chats), "grupo(s)")
        return jsonify({"ok": True, "enviado_para": len(chats)})

    return bp
