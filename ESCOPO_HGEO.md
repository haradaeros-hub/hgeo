# Inventário funcional do H-GEO

Revisão da implementação em `hgeo/legacy.py`, originada de `H_GEO_1.0_FINAL.py`. Este inventário descreve recursos presentes no código; não equivale a uma validação de todos os resultados com dados reais.

## 1. Copiloto Operacional

Quatro ações executadas pelo código:

- Adicionar mapa-base Google Satellite.
- Criar camada temporária de ponto, linha ou polígono, com seleção de SRC entre SIRGAS 2000 geográfico e UTM 21S/22S.
- Gerar buffer de área de influência/APP: metros ou quilômetros, segmentos de suavização, extremidade arredondada/plana/quadrada e dissolução opcional. A opção hectares está desabilitada.
- Remover seleções de feições nas camadas vetoriais do projeto.

Nove guias passo a passo, com avanço e cancelamento:

1. Tour da interface do QGIS.
2. Preparação de Shapefile para SISLA: formato, dissolução, atributos, campo CLASSE e arquivos de exportação.
3. Recorte de camadas (Clip).
4. Interseção de dados.
5. Diferença entre áreas.
6. União de atributos por localização.
7. Mesclagem de camadas vetoriais.
8. Delimitação de bacias.
9. Reprojeção de camadas em lote.

Os guias exibem instruções; sua existência não significa que essas nove operações sejam executadas automaticamente pelo plugin. O guia SISLA contém um roteiro de preparação, sem integração de envio ou validação junto ao sistema. O copiloto usa regras e palavras-chave locais para encaminhar opções do menu.

## 2. Laboratório Analítico

Sete categorias de análise: vegetação; água; solo e áreas expostas; queimada/estresse; áreas urbanas; composição visual; topografia/relevo.

Doze índices disponíveis nos seletores: NDVI, EVI, SAVI, NDWI, MNDWI, NDMI, NDSI, BAI, NBR, NBR2, NDBI e UI.

Outras funções:

- Explicações sobre os índices e indicação das bandas por sensor.
- Mapeamento de bandas Sentinel-2 e Landsat 8/9 já carregadas.
- Busca de datas e cenas, filtro de nuvens e download das bandas necessárias. O fluxo automático da interface fixa Sentinel-2.
- Composição em cor verdadeira ou falsa cor via VRT.
- Cálculo raster e aplicação de rampas de cores.
- Hipsometria sobre MDE.

## 3. Relevo e hidrologia

- MDE existente no projeto ou download pelo recorte do mapa.
- Copernicus DEM via AWS e alternativa OpenTopography quando há chave configurada.
- Painéis educativos e detalhes de resolução, SRC e elevação.
- Reprojeção para SIRGAS 2000/UTM 21S ou 22S, incluindo escolha automática conforme longitude.
- Direção de fluxo e rios por `r.watershed`, com limiar informado pelo usuário.
- Seleção de exutório por clique no mapa e transformação de coordenadas.
- Rotina de ajuste para o raster de drenagem.
- Bacia por `r.water.outlet`, conversão para polígono e geração de múltiplas bacias na mesma sessão.

## 4. Comparação temporal

- Área definida por camada poligonal, opcionalmente apenas feições selecionadas.
- Duas datas-alvo, janela de busca e limite de nuvens.
- Consulta Sentinel-2 L2A, agrupamento das cenas por data e tile.
- Prévias RGB com mesmo recorte/contraste, ampliação, navegação e destaque de pixels excluídos.
- Parâmetro SAVI L, resoluções de 10/20/30/60 m e mínimo de área válida comum.
- Uso dos metadados radiométricos, alinhamento espacial e classificação SCL para qualidade.
- Índices por data e diferença B − A.
- PDF com mapas, estatísticas, metodologia e referências.
- GeoTIFFs A/B/diferença, CSV de estatísticas, área GeoJSON, parâmetros e proveniência JSON.
- Cancelamento cooperativo e indicação de conclusão ou falha.

## 5. Integração como plugin

Menu e botão no QGIS, abertura sob demanda, acesso direto aos perfis e relatório, reaproveitamento da janela do mesmo perfil e seleção persistente da logo.

## Observações encontradas na leitura do código

Os motores do laboratório e do relatório não são idênticos. Há diferenças nas bandas/fórmulas associadas a NBR, BAI e UI. No laboratório, o teste textual de NDWI antecede MNDWI e pode capturar esse nome. Essas diferenças precisam de revisão científica e testes específicos antes de afirmar equivalência entre os módulos. Esta revisão documental não altera as fórmulas existentes.

A rotina de ajuste do exutório verifica a validade da amostra do raster, mas não testa explicitamente se o valor representa uma célula de rio. Esse comportamento também precisa ser validado.

Os limites e dependências do processamento devem ser verificados por operação. A grade do relatório impõe limite de dois milhões de pixels e condições para área UTM local. A publicação inicial é experimental; a validação anterior cobriu abertura e integração, além de um caso de NDVI/NoData, sem executar todos os fluxos reais.

## Autor

**Eros Gabriel Harada**

Universidade Federal de Mato Grosso do Sul — UFMS.

[LinkedIn](https://www.linkedin.com/in/eros-gabriel-harada-449836323/) · [GitHub](https://github.com/haradaeros-hub)
