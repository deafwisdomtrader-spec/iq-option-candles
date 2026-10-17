"""
DW AUDITOR — agora DENTRO do serviço iq-option-candles-1
=========================================================

O QUE É
-------
O card "Auditor" do Academy: o aluno escolhe o par, e a análise é
feita na hora, para a PRÓXIMA vela de 1 minuto.

POR QUE MUDOU DE LUGAR (16/10/2026)
-----------------------------------
O Auditor era um serviço separado no Render (dw-auditor). Os dois
serviços dividiam as mesmas 750 horas grátis do mês, e em outubro a
conta acabou no dia 16 — os dois foram suspensos. O robô passou para
o plano pago, e o Auditor veio morar dentro dele: um serviço só,
7 dólares por mês, os dois funcionando.

O QUE MUDOU POR DENTRO
----------------------
As análises e as configurações do aluno estão IGUAIS, linha por
linha. Mudaram só três coisas:

  1. ENDEREÇO. As rotas ganharam o prefixo /auditor:
         /auditor/saude
         /auditor/pares?mercado=otc|forex|cripto
         /auditor/auditar?par=EURUSD-OTC
         /auditor/resultado?par=..&inicio=..&sinal=CALL|PUT

  2. CONEXÃO. O Auditor usa a MESMA conexão com a IQ Option que o
     robô usa, com o castigo, o vigia e a limpeza automática dele.
     Abrir uma segunda conexão no mesmo processo não é seguro: a
     biblioteca da IQ Option guarda o estado da conexão em
     variáveis globais, e as duas se atropelariam.

  3. PARES. Antes a lista vinha da corretora (get_all_open_time) e o
     aluno podia mandar qualquer nome. Agora só os pares que o robô
     já testou. Motivo: um nome que a corretora não tem TRAVA a
     biblioteca — e agora a conexão é a mesma do Telegram. Um nome
     errado no Auditor derrubaria os sinais do grupo.
     (Além disso, o get_all_open_time desta biblioteca quebra com
     "'NoneType' object is not subscriptable" em vários dias.)

A SENHA (CHAVE_API)
-------------------
Continua igual: todas as rotas, menos /auditor/saude, pedem o
cabeçalho X-Chave igual à variável CHAVE_API. Ela precisa estar
cadastrada no Environment do iq-option-candles-1 — copie o valor que
estava no dw-auditor.
"""

import os
import time

from flask import Blueprint, jsonify, request


VELA = 60          # M1
PREPARO_MIN = 8    # faltando menos que isso para virar o minuto, entra no seguinte

def numero(nome, padrao, minimo, maximo):
    """Número que o aluno escolheu na aba Configuração (sempre dentro do limite)."""
    try:
        v = int(float(request.args.get(nome, padrao)))
    except (TypeError, ValueError):
        v = padrao
    return max(minimo, min(maximo, v))


def numero_f(nome, padrao, minimo, maximo):
    try:
        v = float(str(request.args.get(nome, padrao)).replace(",", "."))
    except (TypeError, ValueError):
        v = padrao
    return max(minimo, min(maximo, v))


def ligado(nome, padrao):
    v = request.args.get(nome)
    return padrao if v is None else v in ("1", "true", "on", "sim")


def config_do_aluno():
    cfg = {
        "ema_rapida": numero("ema_rapida", 21, 3, 100),
        "ema_lenta": numero("ema_lenta", 50, 5, 200),
        "rsi_periodo": numero("rsi_periodo", 14, 3, 50),
        "rsi_baixo": numero("rsi_baixo", 30, 5, 45),
        "rsi_alto": numero("rsi_alto", 70, 55, 95),
        "sr_velas": numero("sr_velas", 30, 10, 150),
    }
    cfg.update({
        # Filtro: com tendência forte (ADX), só sinal A FAVOR da tendência
        "usar_filtro": ligado("usar_filtro", True),
        # MACD
        "usar_macd": ligado("usar_macd", True),
        "macd_rapida": numero("macd_rapida", 12, 2, 50),
        "macd_lenta": numero("macd_lenta", 26, 5, 100),
        "macd_sinal": numero("macd_sinal", 9, 2, 50),
        # Bandas de Bollinger
        "usar_bb": ligado("usar_bb", True),
        "bb_periodo": numero("bb_periodo", 20, 5, 100),
        "bb_desvio": numero_f("bb_desvio", 2.0, 0.5, 4.0),
        # Estocástico
        "usar_st": ligado("usar_st", True),
        "st_k": numero("st_k", 14, 3, 50),
        "st_d": numero("st_d", 3, 1, 20),
        "st_baixo": numero("st_baixo", 20, 5, 40),
        "st_alto": numero("st_alto", 80, 60, 95),
        # ADX (força da tendência)
        "usar_adx": ligado("usar_adx", True),
        "adx_periodo": numero("adx_periodo", 14, 5, 50),
        "adx_forte": numero("adx_forte", 25, 10, 50),
        # CCI
        "usar_cci": ligado("usar_cci", False),
        "cci_periodo": numero("cci_periodo", 20, 5, 100),
        "cci_limite": numero("cci_limite", 100, 50, 300),
        # Parabolic SAR
        "usar_sar": ligado("usar_sar", False),
        "sar_passo": numero_f("sar_passo", 0.02, 0.005, 0.1),
        "sar_max": numero_f("sar_max", 0.2, 0.05, 0.5),
    })
    if cfg["ema_lenta"] <= cfg["ema_rapida"]:
        cfg["ema_lenta"] = cfg["ema_rapida"] + 1
    if cfg["macd_lenta"] <= cfg["macd_rapida"]:
        cfg["macd_lenta"] = cfg["macd_rapida"] + 1
    return cfg


# ------------------------------------------------------------- análise
def ema(valores, n):
    k = 2 / (n + 1)
    e = sum(valores[:n]) / n
    for v in valores[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(fech, n=14):
    ganhos, perdas = [], []
    for a, b in zip(fech[-n - 1:-1], fech[-n:]):
        d = b - a
        ganhos.append(max(d, 0))
        perdas.append(max(-d, 0))
    mg, mp = sum(ganhos) / n, sum(perdas) / n
    if mp == 0:
        return 100.0
    return 100 - 100 / (1 + mg / mp)


def ema_serie(valores, n):
    k = 2 / (n + 1)
    e = sum(valores[:n]) / n
    saida = [None] * (n - 1) + [e]
    for v in valores[n:]:
        e = v * k + e * (1 - k)
        saida.append(e)
    return saida


def media(v):
    return sum(v) / len(v)


def macd(fech, rap, len_, sin):
    er, el = ema_serie(fech, rap), ema_serie(fech, len_)
    linha = [a - b for a, b in zip(er, el) if a is not None and b is not None]
    sinal = ema_serie(linha, sin)
    return linha[-1], sinal[-1], linha[-2], sinal[-2]


def bollinger(fech, n, k):
    j = fech[-n:]
    m = media(j)
    dp = (sum((x - m) ** 2 for x in j) / n) ** 0.5
    return m - k * dp, m, m + k * dp


def estocastico(velas, n, d):
    brutos = []
    for i in range(len(velas) - n - d * 2, len(velas)):
        j = velas[i - n + 1:i + 1]
        hh, ll = max(float(c["max"]) for c in j), min(float(c["min"]) for c in j)
        brutos.append(50.0 if hh == ll else (float(velas[i]["close"]) - ll) / (hh - ll) * 100)
    ks = [media(brutos[i - d + 1:i + 1]) for i in range(d - 1, len(brutos))]
    ds = [media(ks[i - d + 1:i + 1]) for i in range(d - 1, len(ks))]
    return ks[-1], ds[-1], ks[-2], ds[-2]


def adx(velas, n):
    tr, mp, mn = [], [], []
    for a, b in zip(velas[:-1], velas[1:]):
        h, l, cp = float(b["max"]), float(b["min"]), float(a["close"])
        tr.append(max(h - l, abs(h - cp), abs(l - cp)))
        sobe, desce = h - float(a["max"]), float(a["min"]) - l
        mp.append(sobe if sobe > desce and sobe > 0 else 0.0)
        mn.append(desce if desce > sobe and desce > 0 else 0.0)
    atr, sp, sn = sum(tr[:n]), sum(mp[:n]), sum(mn[:n])
    dxs = []
    for i in range(n, len(tr)):
        atr = atr - atr / n + tr[i]
        sp = sp - sp / n + mp[i]
        sn = sn - sn / n + mn[i]
        dip, din = (100 * sp / atr if atr else 0), (100 * sn / atr if atr else 0)
        dxs.append(0 if dip + din == 0 else 100 * abs(dip - din) / (dip + din))
    v = media(dxs[:n])
    for x in dxs[n:]:
        v = (v * (n - 1) + x) / n
    return v, dip, din


def cci(velas, n):
    tp = [(float(c["max"]) + float(c["min"]) + float(c["close"])) / 3 for c in velas[-n:]]
    m = media(tp)
    md = media([abs(x - m) for x in tp])
    return 0.0 if md == 0 else (tp[-1] - m) / (0.015 * md)


def parabolic_sar(velas, passo, maximo):
    alta = float(velas[1]["close"]) >= float(velas[0]["close"])
    sar = float(velas[0]["min"]) if alta else float(velas[0]["max"])
    ep = float(velas[0]["max"]) if alta else float(velas[0]["min"])
    af = passo
    for c in velas[1:]:
        h, l = float(c["max"]), float(c["min"])
        sar = sar + af * (ep - sar)
        if alta:
            if l < sar:
                alta, sar, ep, af = False, ep, l, passo
            elif h > ep:
                ep, af = h, min(af + passo, maximo)
        else:
            if h > sar:
                alta, sar, ep, af = True, ep, h, passo
            elif l < ep:
                ep, af = l, min(af + passo, maximo)
    return sar, alta


def padrao_vela(ant, ult):
    """Nome do padrão da última vela fechada e para qual lado ele puxa."""
    o, c, h, l = ult["open"], ult["close"], ult["max"], ult["min"]
    corpo = abs(c - o)
    faixa = max(h - l, 1e-12)
    sup = h - max(o, c)
    inf = min(o, c) - l
    ao, ac = ant["open"], ant["close"]
    if c > o and ac < ao and c >= ao and o <= ac:
        return "Engolfo de alta", "CALL"
    if c < o and ac > ao and c <= ao and o >= ac:
        return "Engolfo de baixa", "PUT"
    if corpo <= faixa * 0.1:
        return "Doji (indecisão)", None
    if inf >= corpo * 2 and sup <= corpo:
        return "Martelo (rejeição de baixa)", "CALL"
    if sup >= corpo * 2 and inf <= corpo:
        return "Estrela cadente (rejeição de alta)", "PUT"
    if corpo >= faixa * 0.7:
        return ("Vela longa de alta", "CALL") if c > o else ("Vela longa de baixa", "PUT")
    return ("Vela de alta" if c > o else "Vela de baixa"), None


def casas(v):
    return 3 if v > 20 else 5


def analisar(velas, cfg=None):
    cfg = cfg or {"ema_rapida": 21, "ema_lenta": 50, "rsi_periodo": 14, "rsi_baixo": 30, "rsi_alto": 70, "sr_velas": 30}
    call = put = 0
    er, el = cfg["ema_rapida"], cfg["ema_lenta"]
    fech = [float(c["close"]) for c in velas]
    ult, ant = velas[-1], velas[-2]
    preco = fech[-1]
    e21, e50 = ema(fech, er), ema(fech, el)
    r = rsi(fech, cfg["rsi_periodo"])
    janela = velas[-cfg["sr_velas"]:]
    suporte = min(float(c["min"]) for c in janela)
    resistencia = max(float(c["max"]) for c in janela)
    faixa = max(resistencia - suporte, 1e-12)

    motivos = []

    if e21 > e50 and preco > e21:
        tendencia = "Alta"; call += 2; motivos.append(f"médias em alta (EMA {er} acima da {el})")
    elif e21 < e50 and preco < e21:
        tendencia = "Baixa"; put += 2; motivos.append(f"médias em baixa (EMA {er} abaixo da {el})")
    else:
        tendencia = "Lateral"; motivos.append("médias misturadas, mercado lateral")

    if r <= cfg["rsi_baixo"]:
        call += 2; motivos.append(f"RSI {r:.0f} sobrevendido")
    elif r >= cfg["rsi_alto"]:
        put += 2; motivos.append(f"RSI {r:.0f} sobrecomprado")
    elif r > 55:
        call += 1; motivos.append(f"RSI {r:.0f} com força compradora")
    elif r < 45:
        put += 1; motivos.append(f"RSI {r:.0f} com força vendedora")

    nome_padrao, lado = padrao_vela(ant, ult)
    if lado == "CALL":
        call += 2
    elif lado == "PUT":
        put += 2
    motivos.append(f"última vela: {nome_padrao.lower()}")

    if preco - suporte <= faixa * 0.15:
        call += 1; motivos.append("preço perto do suporte")
    elif resistencia - preco <= faixa * 0.15:
        put += 1; motivos.append("preço perto da resistência")

    indicadores = []   # o card mostra cada um com a cor do lado

    def marca(nome, valor, lado, pts, motivo):
        nonlocal call, put
        if lado == "CALL":
            call += pts
        elif lado == "PUT":
            put += pts
        indicadores.append({"nome": nome, "valor": valor, "lado": lado})
        if motivo:
            motivos.append(motivo)

    if cfg.get("usar_macd"):
        m, sg, m0, sg0 = macd(fech, cfg["macd_rapida"], cfg["macd_lenta"], cfg["macd_sinal"])
        cruzou_cima, cruzou_baixo = m0 <= sg0 and m > sg, m0 >= sg0 and m < sg
        if cruzou_cima:
            marca("MACD", "cruzou para cima", "CALL", 2, "MACD cruzou para cima")
        elif cruzou_baixo:
            marca("MACD", "cruzou para baixo", "PUT", 2, "MACD cruzou para baixo")
        else:
            marca("MACD", "acima do sinal" if m > sg else "abaixo do sinal", "CALL" if m > sg else "PUT", 1, "")

    if cfg.get("usar_bb"):
        inf, meio, sup = bollinger(fech, cfg["bb_periodo"], cfg["bb_desvio"])
        if preco <= inf:
            marca("Bollinger", "tocou a banda de baixo", "CALL", 2, "preço na banda inferior de Bollinger")
        elif preco >= sup:
            marca("Bollinger", "tocou a banda de cima", "PUT", 2, "preço na banda superior de Bollinger")
        else:
            marca("Bollinger", "dentro das bandas", None, 0, "")

    if cfg.get("usar_st"):
        k, dd, k0, d0 = estocastico(velas, cfg["st_k"], cfg["st_d"])
        v = f"%K {k:.0f} / %D {dd:.0f}"
        if k <= cfg["st_baixo"] and k0 <= d0 and k > dd:
            marca("Estocástico", v, "CALL", 2, "estocástico virou para cima na zona baixa")
        elif k >= cfg["st_alto"] and k0 >= d0 and k < dd:
            marca("Estocástico", v, "PUT", 2, "estocástico virou para baixo na zona alta")
        elif k <= cfg["st_baixo"]:
            marca("Estocástico", v, "CALL", 1, "estocástico sobrevendido")
        elif k >= cfg["st_alto"]:
            marca("Estocástico", v, "PUT", 1, "estocástico sobrecomprado")
        else:
            marca("Estocástico", v, None, 0, "")

    if cfg.get("usar_adx"):
        a, dip, din = adx(velas, cfg["adx_periodo"])
        if a >= cfg["adx_forte"]:
            lado = "CALL" if dip > din else "PUT"
            marca("ADX", f"{a:.0f} — tendência forte", lado, 1, f"ADX {a:.0f}: tendência forte")
        else:
            marca("ADX", f"{a:.0f} — tendência fraca", None, 0, f"ADX {a:.0f}: tendência fraca")

    if cfg.get("usar_cci"):
        c = cci(velas, cfg["cci_periodo"])
        lim = cfg["cci_limite"]
        if c <= -lim:
            marca("CCI", f"{c:.0f}", "CALL", 1, f"CCI {c:.0f} sobrevendido")
        elif c >= lim:
            marca("CCI", f"{c:.0f}", "PUT", 1, f"CCI {c:.0f} sobrecomprado")
        else:
            marca("CCI", f"{c:.0f}", None, 0, "")

    if cfg.get("usar_sar"):
        sar_v, sar_alta = parabolic_sar(velas[-120:], cfg["sar_passo"], cfg["sar_max"])
        marca("Parabolic SAR", "pontos abaixo do preço" if sar_alta else "pontos acima do preço",
              "CALL" if sar_alta else "PUT", 1, "")

    if call > put:
        sinal = "CALL"
    elif put > call:
        sinal = "PUT"
    else:
        sinal = "AGUARDAR"
        motivos.append("forças empatadas — melhor não entrar")

    # Força da tendência (ADX) — usada no filtro e no "estado do mercado"
    try:
        adx_v, _, _ = adx(velas, cfg.get("adx_periodo", 14))
    except Exception:
        adx_v = 0
    limiar = cfg.get("adx_forte", 25)
    tendencia_forte = adx_v >= limiar and tendencia in ("Alta", "Baixa")

    filtrado = False
    if cfg.get("usar_filtro", True) and tendencia_forte and sinal in ("CALL", "PUT"):
        a_favor = "CALL" if tendencia == "Alta" else "PUT"
        if sinal != a_favor:
            filtrado = True
            motivos.append(f"sinal {sinal} seria CONTRA a tendência forte de {tendencia.lower()} (ADX {adx_v:.0f}) — melhor não entrar")
            sinal = "AGUARDAR"

    # Estado do mercado
    if tendencia_forte:
        estado = "EM TENDÊNCIA DE " + tendencia.upper()
    elif adx_v >= limiar:
        estado = "VOLÁTIL"
    else:
        estado = "LATERAL"

    d = casas(preco)
    return {
        "estado": estado,
        "filtrado": filtrado,
        "sinal": sinal,
        "pontos_call": call,
        "pontos_put": put,
        "tendencia": tendencia,
        "padrao": nome_padrao,
        "rsi": round(r, 1),
        "indicadores": indicadores,
        "suporte": f"{suporte:.{d}f}",
        "resistencia": f"{resistencia:.{d}f}",
        "preco": f"{preco:.{d}f}",
        "logica": (lambda t: t[:1].upper() + t[1:])("; ".join(motivos) + "."),
    }


# ============================================================
# ROTAS — montadas com as funções de conexão do robô
# ============================================================

def criar_auditor(
    conectar,
    buscar,
    pares_otc,
    pares_forex,
    pares_cripto,
    forex_aberto,
):
    """Devolve o blueprint do Auditor, já ligado à conexão do robô.

    As funções e listas vêm de fora (do app.py) para este arquivo
    não importar o app.py — isso criaria um import circular.
    """

    bp = Blueprint("auditor", __name__, url_prefix="/auditor")

    CHAVE_API = os.environ.get("CHAVE_API", "")

    def permitidos():
        return set(pares_otc) | set(pares_forex) | set(pares_cripto)

    def candles(par, quantidade):
        """Velas M1 pela conexão do robô, no formato do Auditor."""
        iq = conectar()
        dados = buscar(iq, par, int(quantidade), timeout_segundos=15) or []
        saida = []
        for c in dados:
            c = dict(c)
            # O robô entrega high/low; a análise do Auditor usa max/min.
            c["max"] = c.get("high", c.get("max"))
            c["min"] = c.get("low", c.get("min"))
            saida.append(c)
        return sorted(saida, key=lambda c: c["from"])

    # --------------------------------------------------- segurança
    @bp.before_request
    def conferir_chave():
        if request.path.rstrip("/") in ("/auditor", "/auditor/saude"):
            return None
        if CHAVE_API and request.headers.get("X-Chave", "") != CHAVE_API:
            return jsonify({"erro": "Acesso negado."}), 403
        return None

    @bp.after_request
    def sem_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------- rotas
    @bp.get("/")
    @bp.get("/saude")
    def saude():
        return jsonify({
            "ok": True,
            "app": "dw-auditor",
            "dentro_de": "iq-option-candles-1",
            "hora": int(time.time()),
        })

    @bp.get("/pares")
    def pares():
        mercado = request.args.get("mercado", "otc").lower()
        if mercado == "forex":
            lista = list(pares_forex) if forex_aberto() else []
        elif mercado in ("cripto", "acoes"):
            lista = list(pares_cripto)
        else:
            mercado = "otc"
            lista = list(pares_otc)
        return jsonify({"mercado": mercado, "pares": lista})

    @bp.get("/auditar")
    def auditar():
        par = (request.args.get("par") or "").upper().strip()
        if not par or len(par) > 20:
            return jsonify({"erro": "Escolha um par."}), 422
        if par not in permitidos():
            return jsonify({"erro": "Par não disponível no Auditor."}), 422
        cfg = config_do_aluno()
        precisa = max(cfg["ema_lenta"], cfg["sr_velas"], cfg["rsi_periodo"] + 1,
                      cfg["macd_lenta"] + cfg["macd_sinal"], cfg["bb_periodo"],
                      cfg["st_k"] + cfg["st_d"] * 2, cfg["adx_periodo"] * 2 + 1,
                      cfg["cci_periodo"]) + 10
        try:
            velas = candles(par, min(max(120, precisa + 20), 400))
        except Exception as e:
            return jsonify({"erro": str(e)[:160]}), 503

        agora = time.time()
        # Só velas FECHADAS entram na análise.
        fechadas = [c for c in velas if c["from"] + VELA <= agora]
        if len(fechadas) < precisa:
            return jsonify({"erro": "Poucos candles para analisar este par agora."}), 422

        r = analisar(fechadas, cfg)

        proximo = (int(agora) // VELA + 1) * VELA
        if proximo - agora < PREPARO_MIN:
            proximo += VELA
        r.update({
            "par": par,
            "entrada_em": proximo,
            "expira_em": proximo + VELA,
            "expiracao": "1 minuto",
            "tipo_grafico": "candlestick M1",
            "analisado_em": int(agora),
            "config": cfg,
        })
        return jsonify(r)

    @bp.get("/resultado")
    def resultado():
        par = (request.args.get("par") or "").upper().strip()
        sinal = (request.args.get("sinal") or "").upper()
        try:
            inicio = int(request.args.get("inicio", "0"))
        except ValueError:
            inicio = 0
        if not par or sinal not in ("CALL", "PUT") or inicio <= 0:
            return jsonify({"erro": "Dados inválidos."}), 422
        if par not in permitidos():
            return jsonify({"erro": "Par não disponível no Auditor."}), 422
        agora = time.time()
        if agora < inicio + VELA + 2:
            return jsonify({"status": "AGUARDANDO"})

        # A conexão do robô sempre busca as velas que terminam AGORA.
        # Pede o suficiente para alcançar a vela de entrada.
        idade = int((agora - inicio) // VELA) + 3
        if idade > 400:
            return jsonify({"erro": "Entrada antiga demais para conferir."}), 422
        try:
            velas = candles(par, max(5, idade))
        except Exception as e:
            return jsonify({"erro": str(e)[:160]}), 503

        vela = next((c for c in velas if int(c["from"]) == inicio), None)
        if not vela:
            return jsonify({"status": "AGUARDANDO"})
        abre, fecha = float(vela["open"]), float(vela["close"])
        if fecha == abre:
            status = "EMPATE"
        elif (fecha > abre) == (sinal == "CALL"):
            status = "WIN"
        else:
            status = "LOSS"
        return jsonify({"status": status, "abertura": abre, "fechamento": fecha})

    return bp
