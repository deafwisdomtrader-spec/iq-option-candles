/* =========================================================
   PAINEL DE INDICADORES
   M1 · OTC & FOREX
   script.js

   IMPORTANTE:
   - Use window.ESTATISTICAS_API quando o Flask estiver em
     outro domínio, por exemplo:
     window.ESTATISTICAS_API = "https://seu-servidor.onrender.com/estatisticas";
   - Sem essa variável, o script tenta automaticamente:
       /estatisticas
       /api/estatisticas
*/

"use strict";

const META = 53.8;

const URL_FLASK = "https://iq-option-candles-1.onrender.com";

const API_CONFIGURADA = `${URL_FLASK}/estatisticas`;

const ENDPOINTS_PADRAO = [
  API_CONFIGURADA,
];

const $ = (selector) => document.querySelector(selector);

function dataLocal(date = new Date()) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function hoje() {
  return dataLocal();
}

function diasAtras(n) {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  d.setDate(d.getDate() - Number(n || 0));
  return dataLocal(d);
}

function numero(valor, fallback = 0) {
  const n = Number(valor);
  return Number.isFinite(n) ? n : fallback;
}

function pctSeguro(valor) {
  return Math.max(0, Math.min(100, numero(valor, 0)));
}

function faixa(pct) {
  if (pct >= META) return "passou";
  if (pct >= 52) return "quase";
  if (pct >= 50) return "moeda";
  return "fraco";
}

function escapar(texto) {
  return String(texto ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function criarLinha(rotulo, valor) {
  const p = pctSeguro(valor);
  const classe = faixa(p);

  return `
    <div class="linha ${classe}">
      <span class="rotulo">${escapar(rotulo)}</span>
      <span class="pct">${p.toFixed(1).replace(".", ",")}%</span>
    </div>
    <div class="barra">
      <div class="fill" style="width:${p}%"></div>
      <span class="meta"></span>
    </div>
  `;
}

function criarCard(indicador) {
  const nome = indicador?.indicador ?? indicador?.nome ?? "Indicador";
  const sinais = Math.max(0, Math.floor(numero(indicador?.sinais, 0)));
  const seg = numero(indicador?.seg_pct, 0);
  const tel = numero(indicador?.tel_pct, seg);
  const poucos = sinais < 500;

  return `
    <article class="card">
      <div class="card-head">
        <h3>${escapar(nome)}</h3>
        <span class="sinais ${poucos ? "roxo" : ""}">
          ${sinais.toLocaleString("pt-BR")} sinais
        </span>
      </div>
      ${criarLinha("Sem gale", seg)}
      ${criarLinha("Resultado final", tel)}
    </article>
  `;
}

function mostrarErro(mensagem) {
  const status = $("#status");
  const grid = $("#grid");

  if (grid) {
    grid.innerHTML = "";
  }

  if (status) {
    status.classList.add("erro");
    status.textContent = mensagem;
  }
}

function obterEndpoints() {
  return [API_CONFIGURADA];
}

console.log("INDICADORES: usando Render =", API_CONFIGURADA);\n\nasync function consultarEndpoint(endpoint, params) {
  const url = endpoint.includes("?")
    ? `${endpoint}&${params.toString()}`
    : `${endpoint}?${params.toString()}`;

  const resposta = await fetch(url, {
    method: "GET",
    headers: {
      Accept: "application/json",
    },
    cache: "no-store",
  });

  const texto = await resposta.text();
  let data = null;

  try {
    data = texto ? JSON.parse(texto) : null;
  } catch (_) {
    data = null;
  }

  if (!resposta.ok) {
    const erro = new Error(
      data?.erro || `Erro HTTP ${resposta.status}`
    );
    erro.status = resposta.status;
    erro.endpoint = endpoint;
    throw erro;
  }

  if (!data || typeof data !== "object") {
    const erro = new Error("Resposta inválida do servidor.");
    erro.status = resposta.status;
    erro.endpoint = endpoint;
    throw erro;
  }

  if (data.ok === false) {
    const erro = new Error(data.erro || "O servidor recusou a consulta.");
    erro.status = resposta.status;
    erro.endpoint = endpoint;
    throw erro;
  }

  return { data, endpoint };
}

async function buscarIndicadores(params) {
  const endpoints = obterEndpoints();
  const erros = [];

  for (const endpoint of endpoints) {
    try {
      return await consultarEndpoint(endpoint, params);
    } catch (erro) {
      erros.push(erro);

      // 404/405 significa que esta rota não existe neste servidor.
      // Tenta a próxima automaticamente.
      if (erro.status !== 404 && erro.status !== 405) {
        throw erro;
      }
    }
  }

  const primeiro = erros[0];
  const urlBase = endpoints.join(" ou ");
  const erroFinal = new Error(
    `A rota de indicadores não foi encontrada. Tentado: ${urlBase}. ` +
    `O app.py/servidor precisa publicar /estatisticas.`
  );
  erroFinal.status = primeiro?.status || 404;
  throw erroFinal;
}

async function recalcular() {
  const inicio = $("#inicio")?.value;
  const fim = $("#fim")?.value;
  const mercado = $("#mercado")?.value || "ambos";
  const botao = $("#btn-recalcular");
  const grid = $("#grid");
  const status = $("#status");

  if (!inicio || !fim) {
    mostrarErro("Escolha a data inicial e a data final.");
    return;
  }

  if (inicio > fim) {
    mostrarErro("A data inicial não pode ser maior que a data final.");
    return;
  }

  if (botao) {
    botao.disabled = true;
    botao.textContent = "Carregando...";
  }

  if (status) {
    status.classList.remove("erro");
    status.textContent = "Consultando histórico...";
  }

  if (grid) {
    grid.innerHTML = "";
  }

  try {
    const params = new URLSearchParams({
      inicio,
      fim,
      mercado,
    });

    const resultado = await buscarIndicadores(params);
    const data = resultado.data;
    const endpointUsado = resultado.endpoint;

    const indicadores = Array.isArray(data.indicadores)
      ? data.indicadores
      : [];

    if (grid) {
      grid.innerHTML = indicadores.length
        ? indicadores.map(criarCard).join("")
        : `<div class="vazio">Nenhum sinal encontrado neste período/mercado.</div>`;
    }

    const fortes = indicadores.filter(
      (ind) => numero(ind?.seg_pct, 0) >= META
    ).length;

    if (status) {
      status.classList.remove("erro");
      status.textContent =
        `${indicadores.length} indicadores · ${fortes} acima da meta · ` +
        `${inicio.split("-").reverse().join("/")} a ${fim.split("-").reverse().join("/")} · ` +
        `${mercado.toUpperCase()}`;

      status.title = `Fonte: ${endpointUsado}`;
    }
  } catch (erro) {
    console.error("Painel de Indicadores:", erro);

    let mensagem = `Não foi possível carregar os indicadores: ${erro?.message || erro}`;

    if (erro?.status === 404) {
      mensagem =
        "Erro HTTP 404: o servidor do app.py não publicou /estatisticas. " +
        "O JavaScript está funcionando, mas a API não foi encontrada.";
    }

    mostrarErro(mensagem);
  } finally {
    if (botao) {
      botao.disabled = false;
      botao.textContent = "Recalcular";
    }
  }
}

function aplicarPeriodo(dias) {
  const inicio = $("#inicio");
  const fim = $("#fim");
  if (!inicio || !fim) return;

  fim.value = hoje();
  inicio.value = diasAtras(dias);
  recalcular();
}

function iniciarPainel() {
  const inicio = $("#inicio");
  const fim = $("#fim");
  const botao = $("#btn-recalcular");

  if (!inicio || !fim || !botao) {
    console.error("Painel de Indicadores: elementos do HTML não encontrados.");
    return;
  }

  inicio.value = diasAtras(14);
  fim.value = hoje();

  botao.addEventListener("click", recalcular);

  document.querySelectorAll(".atalhos button[data-dias]").forEach((el) => {
    el.addEventListener("click", () => aplicarPeriodo(el.dataset.dias));
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", iniciarPainel);
} else {
  iniciarPainel();
}
