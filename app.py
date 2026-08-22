<?php
declare(strict_types=1);

/*
|--------------------------------------------------------------------------
| ACADEMY TRADING - API DE CANDLES
|--------------------------------------------------------------------------
| O PHP NÃO executa Python na Hostinger.
| Ele apenas consulta o servidor Render.
|
| O backend (app.py no Render) agora alterna sozinho quais 5 pares
| buscar a cada 2 minutos, então aqui não precisa mais dividir em
| lotes nem montar lista de pares — só repassa a chamada.
|--------------------------------------------------------------------------
*/

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store, no-cache, must-revalidate, max-age=0');
header('Pragma: no-cache');
header('Expires: 0');

function resposta(array $dados, int $codigo = 200): never
{
    http_response_code($codigo);

    echo json_encode(
        $dados,
        JSON_UNESCAPED_UNICODE |
        JSON_UNESCAPED_SLASHES |
        JSON_PRETTY_PRINT
    );

    exit;
}

/*
|--------------------------------------------------------------------------
| CONFIGURAÇÃO DO RENDER
|--------------------------------------------------------------------------
*/

$renderUrl = 'https://iq-option-candles.onrender.com/candles';

$paresPermitidosOtc = [
    'EURUSD-OTC',
    'GBPUSD-OTC',
    'USDJPY-OTC',
    'EURJPY-OTC',
    'GBPJPY-OTC',
    'EURGBP-OTC',
    'USDCHF-OTC',
    'AUDJPY-OTC',
    'NZDUSD-OTC',
    'EURCAD-OTC',
    'GBPAUD-OTC',
    'CADJPY-OTC',
    'EURAUD-OTC'
];

$paresPermitidosAcoes = [
    'APPLE',
    'AMAZON',
    'GOOGLE',
    'MICROSOFT',
    'TESLA',
    'NETFLIX',
    'INTEL',
    'ALIBABA'
];

$paresPermitidosAcoesOtc = [
    'APPLE-OTC',
    'AMAZON-OTC',
    'GOOGLE-OTC',
    'MICROSOFT-OTC',
    'TESLA-OTC',
    'NETFLIX-OTC',
    'INTEL-OTC',
    'ALIBABA-OTC'
];

$paresPermitidosForex = [
    'EURUSD',
    'GBPUSD',
    'USDJPY',
    'EURJPY',
    'AUDUSD',
    'USDCAD',
    'GBPJPY',
    'EURGBP',
    'USDCHF',
    'AUDJPY',
    'NZDUSD',
    'EURCAD',
    'GBPAUD',
    'CADJPY',
    'EURAUD'
];

/*
|--------------------------------------------------------------------------
| MERCADO (otc ou forex)
|--------------------------------------------------------------------------
*/

$mercado = strtolower(trim($_GET['mercado'] ?? 'otc'));

if (!in_array($mercado, ['otc', 'forex', 'acoes', 'acoes_otc'], true)) {
    $mercado = 'otc';
}

if ($mercado === 'forex') {
    $paresPermitidos = $paresPermitidosForex;
} elseif ($mercado === 'acoes') {
    $paresPermitidos = $paresPermitidosAcoes;
} elseif ($mercado === 'acoes_otc') {
    $paresPermitidos = $paresPermitidosAcoesOtc;
} else {
    $paresPermitidos = $paresPermitidosOtc;
}

/*
|--------------------------------------------------------------------------
| CONFERIR RESULTADO DE UM SINAL
|--------------------------------------------------------------------------
| Se o front mandar ?acao=resultado, repassa para a rota
| /resultado/<par> do Render, que compara abertura x fechamento
| do candle de entrada e devolve WIN, LOSS ou EMPATE.
|
| Uso: sinais.php?acao=resultado&par=EURUSD&inicio=1755792060&sinal=PUT
|--------------------------------------------------------------------------
*/

if (strtolower(trim($_GET['acao'] ?? '')) === 'resultado') {

    $parAlvo   = strtoupper(trim($_GET['par'] ?? ''));
    $inicioTs  = (int) ($_GET['inicio'] ?? 0);
    $sinalAlvo = strtoupper(trim($_GET['sinal'] ?? ''));

    $todosPares = array_merge(
        $paresPermitidosOtc,
        $paresPermitidosForex,
        $paresPermitidosAcoes,
        $paresPermitidosAcoesOtc
    );

    if (!in_array($parAlvo, $todosPares, true)) {
        resposta([
            'ok'   => false,
            'erro' => 'Par nao permitido.',
            'par'  => $parAlvo
        ], 400);
    }

    if ($inicioTs <= 0 || !in_array($sinalAlvo, ['CALL', 'PUT'], true)) {
        resposta([
            'ok'   => false,
            'erro' => 'Parametros invalidos.'
        ], 400);
    }

    $baseRender = preg_replace('#/candles/?$#', '', $renderUrl);

    $urlResultado = $baseRender
        . '/resultado/' . rawurlencode($parAlvo)
        . '?inicio=' . $inicioTs
        . '&sinal=' . rawurlencode($sinalAlvo);

    $ch = curl_init($urlResultado);

    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 25,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_HTTPHEADER     => ['Accept: application/json']
    ]);

    $corpo   = curl_exec($ch);
    $codigo  = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $falha   = curl_error($ch);

    curl_close($ch);

    if ($corpo === false || $falha !== '') {
        resposta([
            'ok'   => false,
            'erro' => 'Falha ao consultar o servidor.'
        ], 502);
    }

    http_response_code($codigo > 0 ? $codigo : 200);
    header('Content-Type: application/json; charset=utf-8');
    echo $corpo;
    exit;
}

/*
|--------------------------------------------------------------------------
| PAR ESPECÍFICO (opcional)
|--------------------------------------------------------------------------
| Se o front mandar ?par=ALGUMPAR, busca só esse.
| Se não mandar nada, deixa o backend decidir sozinho quais
| 5 pares mostrar (ele alterna automaticamente a cada 2 minutos).
|--------------------------------------------------------------------------
*/

$parUnico = strtoupper(trim($_GET['par'] ?? ''));

$url = $renderUrl . '?mercado=' . rawurlencode($mercado);

if ($parUnico !== '') {

    if (!in_array($parUnico, $paresPermitidos, true)) {

        resposta([
            'ok' => false,
            'erro' => 'Par não permitido.',
            'par' => $parUnico,
            'pares_permitidos' => $paresPermitidos
        ], 400);
    }

    $url .= '&pares=' . rawurlencode($parUnico);
}

/*
|--------------------------------------------------------------------------
| CONSULTA AO RENDER
|--------------------------------------------------------------------------
*/

$ch = curl_init();

curl_setopt_array($ch, [
    CURLOPT_URL => $url,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT => 60,
    CURLOPT_CONNECTTIMEOUT => 10,
    CURLOPT_FOLLOWLOCATION => true,
    CURLOPT_SSL_VERIFYPEER => true,
    CURLOPT_SSL_VERIFYHOST => 2,
    CURLOPT_HTTPHEADER => [
        'Accept: application/json'
    ],
]);

$saida = curl_exec($ch);

$erroCurl = curl_error($ch);
$httpCode = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);

curl_close($ch);

/*
|--------------------------------------------------------------------------
| ERRO DE CONEXÃO
|--------------------------------------------------------------------------
*/

if ($saida === false || $erroCurl !== '') {

    resposta([
        'ok' => false,
        'erro' => 'Não foi possível conectar ao servidor de candles.',
        'detalhe' => $erroCurl,
        'fonte' => 'Render',
        'servico' => 'Academy Trading',
        'somente_dados' => true,
        'operacao' => false
    ], 503);
}

/*
|--------------------------------------------------------------------------
| HTTP ERROR
|--------------------------------------------------------------------------
*/

if ($httpCode < 200 || $httpCode >= 300) {

    resposta([
        'ok' => false,
        'erro' => 'O servidor Render retornou um erro.',
        'http_code' => $httpCode,
        'fonte' => 'Render',
        'servico' => 'Academy Trading',
        'somente_dados' => true,
        'operacao' => false
    ], 503);
}

/*
|--------------------------------------------------------------------------
| JSON DO RENDER
|--------------------------------------------------------------------------
*/

$dados = json_decode($saida, true);

if (!is_array($dados)) {

    resposta([
        'ok' => false,
        'erro' => 'O Render não retornou JSON válido.',
        'resposta' => $saida,
        'fonte' => 'Render',
        'somente_dados' => true,
        'operacao' => false
    ], 502);
}

/*
|--------------------------------------------------------------------------
| RESPOSTA FINAL
|--------------------------------------------------------------------------
*/

$dados['api'] = 'Academy Trading';
$dados['backend'] = 'PHP → Render → IQ Option';
$dados['somente_dados'] = true;
$dados['operacao'] = false;

echo json_encode(
    $dados,
    JSON_UNESCAPED_UNICODE |
    JSON_UNESCAPED_SLASHES
);

exit;
?>
