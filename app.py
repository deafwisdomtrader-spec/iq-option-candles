"""
CORREÇÃO — RESULTADOS (WIN, G1, G2, LOSS + stickers) NO TELEGRAM

Como usar (na pasta onde está o app.py):

    python corrigir_resultados.py

Ele faz backup em app.py.bak, aplica as correções e confere se cada
trecho foi encontrado. Se algum trecho não bater, NÃO altera nada.
Depois é só subir o app.py novo no GitHub / Render.
"""

import os
import shutil
import sys

ARQUIVO = sys.argv[1] if len(sys.argv) > 1 else "app.py"

# ------------------------------------------------------------
# Função nova: o resultado deixa de depender do banco
# ------------------------------------------------------------
FUNCAO_NOVA = '''_RESULTADOS_TG = set()
_RESULTADOS_TG_LOCK = threading.Lock()


def _marcar_resultado_tg(par, entrada_em, sinal):
    """True só na PRIMEIRA vez que o resultado deste sinal vai ao grupo.

    POR QUE ISTO EXISTE
    -------------------
    Antes o resultado só ia ao grupo se a gravação no banco
    (registrar_resultado_historico) devolvesse True. Só que o UPDATE
    não acha linha quando o INSERT do sinal falhou (banco ocupado,
    disco novo depois de deploy) — e aí o sinal ia ao grupo, mas o
    WIN / G1 / G2 / LOSS NUNCA saía, sem erro nenhum no log.

    Agora a decisão de mandar é daqui: memória (rápida) + tabela
    relatorios_enviados (vale entre processos e reinícios).
    """

    chave_mem = _chave_aviso(par, entrada_em, sinal)

    if chave_mem is None:
        return False

    with _RESULTADOS_TG_LOCK:
        if chave_mem in _RESULTADOS_TG:
            return False
        _RESULTADOS_TG.add(chave_mem)
        if len(_RESULTADOS_TG) > 500:
            for velha in list(_RESULTADOS_TG)[:100]:
                _RESULTADOS_TG.discard(velha)

    if not _DB_PRONTO:
        return True

    try:
        with _db_lock:
            conexao = _conectar_db()
            cursor = conexao.execute(
                "INSERT OR IGNORE INTO relatorios_enviados "
                "(chave, enviado_em) VALUES (?, ?)",
                (
                    "res|" + "|".join(str(x) for x in chave_mem),
                    int(time.time()),
                ),
            )
            conexao.commit()
            novo = cursor.rowcount > 0
            conexao.close()
        return novo
    except Exception:
        # Banco com problema: a memória já garantiu o envio único
        # dentro deste processo. Melhor mandar do que ficar mudo.
        return True


def estatistica_combinacao(par, sinal, forca):'''

# ------------------------------------------------------------
# Bloco que decide se o resultado vai ao grupo
# ------------------------------------------------------------
DECISAO_NOVA = '''            # é consultada várias vezes pro mesmo sinal.
            #
            # CORREÇÃO: quem decide é _marcar_resultado_tg, não o
            # banco. Resultado com mais de 15 min não vai ao grupo
            # (chegaria fora de hora).
            vai_ao_grupo = (
                _marcar_resultado_tg(par, inicio_candle, sinal)
                and (time.time() - inicio_candle <= 900)
            )

            print(
                "RESULTADO:", par, sinal,
                resultado_final, "G" + str(etapa_final),
                "| banco:", gravado,
                "| vai ao grupo:", vai_ao_grupo,
            )

            if gravado or vai_ao_grupo:'''

TROCAS = [
    # 1) Sticker que falhou de vez também tenta como foto.
    (
        "        if estado == RECUSADO:\n"
        "            _enviar_telegram_photo_sync(",
        "        if estado in (RECUSADO, FALHOU):\n"
        "            _enviar_telegram_photo_sync(",
    ),
    # 2) "Talvez chegou" conta como enviado (senão o resultado
    #    nunca saía quando o Telegram demorava a confirmar).
    (
        "    return estado_texto == ENVIADO\n",
        "    # TALVEZ = o Telegram recebeu e só demorou a confirmar.\n"
        "    # Tratar como falha fazia o resultado NUNCA ser mandado.\n"
        "    return estado_texto in (ENVIADO, TALVEZ)\n",
    ),
    # 3) Cartão prioritário: resultado nunca é descartado pela fila.
    (
        "def enviar_telegram_foto(\n"
        "    caminho_imagem,\n"
        "    caption,\n"
        "    chat_id=None,\n"
        "    ao_confirmar=None,\n"
        "):",
        "def enviar_telegram_foto(\n"
        "    caminho_imagem,\n"
        "    caption,\n"
        "    chat_id=None,\n"
        "    ao_confirmar=None,\n"
        "    prioritario=False,\n"
        "):",
    ),
    (
        "        if _fila_telegram >= _FILA_TELEGRAM_MAXIMA:",
        "        if _fila_telegram >= _FILA_TELEGRAM_MAXIMA and not prioritario:",
    ),
    # 4) Cópia do resultado e resumo no grupo grátis: prioritários.
    (
        "enviar_telegram_foto(imagem, caption, chat_id=TELEGRAM_CHAT_ID_GRATIS)",
        "enviar_telegram_foto(\n"
        "        imagem, caption, chat_id=TELEGRAM_CHAT_ID_GRATIS,\n"
        "        prioritario=True,\n"
        "    )",
    ),
    (
        "enviar_telegram_foto(None, texto, chat_id=TELEGRAM_CHAT_ID_GRATIS)",
        "enviar_telegram_foto(\n"
        "        None, texto, chat_id=TELEGRAM_CHAT_ID_GRATIS,\n"
        "        prioritario=True,\n"
        "    )",
    ),
    # 5) Função nova.
    (
        "def estatistica_combinacao(par, sinal, forca):",
        FUNCAO_NOVA,
    ),
    # 6) Decisão de mandar o resultado.
    (
        "            # é consultada várias vezes pro mesmo sinal.\n"
        "            if gravado:",
        DECISAO_NOVA,
    ),
    (
        "                if foi_avisado_telegram(par, inicio_candle, sinal):",
        "                if vai_ao_grupo and foi_avisado_telegram(\n"
        "                    par, inicio_candle, sinal\n"
        "                ):",
    ),
    (
        "                else:\n"
        "                    # A entrada não foi confirmada no Telegram.",
        "                elif vai_ao_grupo:\n"
        "                    print(\n"
        "                        \"RESULTADO:\", par, \"— entrada não foi \"\n"
        "                        \"anunciada no grupo, resultado não enviado.\"\n"
        "                    )\n"
        "                    # A entrada não foi confirmada no Telegram.",
    ),
    (
        "                            ao_confirmar=_resultado_confirmado,\n"
        "                        )",
        "                            ao_confirmar=_resultado_confirmado,\n"
        "                            prioritario=True,\n"
        "                        )",
    ),
]


def main():

    if not os.path.isfile(ARQUIVO):
        print("Não achei", ARQUIVO, "- rode na pasta do app.py.")
        sys.exit(1)

    with open(ARQUIVO, "r", encoding="utf-8", newline="") as f:
        texto = f.read().replace("\r\n", "\n")

    if "_marcar_resultado_tg" in texto:
        print("Este app.py já tem a correção. Nada a fazer.")
        sys.exit(0)

    for numero, (antigo, novo) in enumerate(TROCAS, start=1):
        vezes = texto.count(antigo)
        if vezes != 1:
            print(
                "PARADO: o trecho", numero, "apareceu", vezes,
                "vez(es) (era para ser 1). Nada foi alterado."
            )
            print("Trecho procurado:\n" + antigo)
            sys.exit(1)
        texto = texto.replace(antigo, novo)

    try:
        compile(texto, ARQUIVO, "exec")
    except SyntaxError as erro:
        print("PARADO: o resultado ficou com erro de sintaxe:", erro)
        print("Nada foi alterado.")
        sys.exit(1)

    shutil.copyfile(ARQUIVO, ARQUIVO + ".bak")

    with open(ARQUIVO, "w", encoding="utf-8", newline="\n") as f:
        f.write(texto)

    print("Pronto! Correção aplicada em", ARQUIVO)
    print("Backup do original em", ARQUIVO + ".bak")


if __name__ == "__main__":
    main()
