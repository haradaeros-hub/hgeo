# H-GEO 1.1.2 — plugin experimental para QGIS

## Instalação

1. Abra QGIS 3.40.
2. Acesse **Complementos → Gerenciar e Instalar Complementos → Instalar a partir do ZIP**.
3. Selecione `HGEO_1.1.2.zip` e clique em **Instalar complemento**.
4. Use **Complementos → H-GEO → Abrir H-GEO**, ou o botão na barra de ferramentas.

Feche a versão executada como script antes de usar o plugin, para evitar janelas duplicadas.

## Funções preservadas

Modos Operacional e Analista; camada de rascunho; buffer; análise multiespectral; download de bandas e MDE; reprojeção; extração de exutório; delimitação de bacia; drenagem; prévias e relatório comparativo em PDF.

Os algoritmos existentes foram mantidos. O código original foi integrado em `legacy.py`; esta versão ainda não representa uma separação completa dos módulos internos.

## Melhorias

- Integração com o ciclo de carregamento e desativação do QGIS.
- Importação das ferramentas somente no primeiro uso, sem abrir janelas na inicialização do QGIS.
- Atalhos diretos para modos e relatório.
- Reutilização da janela ao abrir novamente o mesmo modo.
- Imports Qt pela camada `qgis.PyQt`.
- Remoção dos menus e ocultação das janelas ao desativar. Threads em execução não são encerradas à força; operações existentes podem continuar até concluir.

## Validação e limites

Testado com bibliotecas reais do QGIS 3.40.15 em execução sem interface visível e com uma interface hospedeira simulada: importação, carregamento sob demanda, quatro ações, abertura dos dois modos e do relatório, reutilização da janela, desativação e cálculo NDVI com zero no denominador/NoData.

Não foram executados downloads, geração completa do PDF ou algoritmos GRASS/GDAL com dados reais nesta validação. As operações dependem da conexão de rede, cobertura dos serviços e provedores Processing já exigidos pelo script original. Não há medição de ganho de tempo no processamento raster; as melhorias desta versão concentram-se no carregamento e nas janelas.

Compatibilidade declarada: QGIS 3.40 a 3.x; QGIS 4 não validado. Distribuição local experimental; publicação no repositório oficial exige definir licença, autoria, contato e repositório com o responsável.

Estrutura de plugin baseada na documentação oficial: https://docs.qgis.org/3.40/en/docs/pyqgis_developer_cookbook/plugins/plugins.html
