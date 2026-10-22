from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QPushButton, QLineEdit,
                             QTextEdit, QHBoxLayout, QWidget, QLabel, QMessageBox, QComboBox, QCheckBox, QApplication, QGraphicsDropShadowEffect, QGroupBox, QGridLayout, QProgressBar, QStackedWidget, QListWidget, QListWidgetItem)
from qgis.PyQt.QtGui import QColor, QPainter, QPen, QStandardItemModel, QStandardItem
from qgis.PyQt.QtCore import Qt, QPointF, QThread, pyqtSignal, QTimer
from qgis.core import (QgsProject, QgsRasterLayer, QgsVectorLayer,
                       QgsSingleBandPseudoColorRenderer, QgsColorRampShader, QgsRasterShader, QgsWkbTypes, QgsApplication, QgsRasterBandStats,
                       QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsTask)
from qgis.gui import QgsMapToolEmitPoint
import qgis.utils
import processing
import math
import os
import tempfile
import time
import urllib.request
import urllib.parse
import json
from datetime import date, timedelta, datetime

# ==========================================
# DEFINA O CAMINHO DA LOGO AQUI:
# ==========================================
from qgis.PyQt.QtCore import QSettings
CAMINHO_LOGO = QSettings().value("hgeo/logo", "", type=str)

# ==========================================
# CHAVE DE API DO OPENTOPOGRAPHY (OPCIONAL):
# Usada apenas como método alternativo de download do MDE, caso o
# Copernicus DEM via AWS falhe. Gratuita, cadastro em:
# https://portal.opentopography.org/myopentopo
# ==========================================
OPENTOPOGRAPHY_API_KEY = ""

# PALETA DE CORES CORPORATIVA
COR_PRIMARIA = "#1E507E"       # Azul Corporativo
COR_PRIMARIA_HOVER = "#153A5B" # Azul Escuro (Hover)
COR_DESTAQUE = "#69B04C"       # Verde Claro (Atenção/Sucesso)
COR_USUARIO = "#334155"        # Cinza Chumbo Escuro (Texto)

_assistente_chat = None
_hgeo_previous_chats = []
_assistente_welcome = None

_modo_atual = "operacional"

# ==========================================
# FUNÇÃO AUXILIAR DE ESTÉTICA
# ==========================================
def aplicar_sombra(widget, blur_radius=15, alpha=40, y_offset=3):
    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(blur_radius)
    shadow.setXOffset(0)
    shadow.setYOffset(y_offset)
    shadow.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(shadow)

# ==========================================
# DOWNLOAD/REPROJEÇÃO AUTOMÁTICA DE MDE (Copernicus DEM / OpenTopography)
#
# As funções "_arquivo" abaixo fazem apenas trabalho puro de GDAL/rede e
# devolvem um caminho de arquivo (string) — nunca criam QgsRasterLayer nem
# tocam o projeto QGIS, pois rodam dentro de uma QThread em segundo plano
# (ver TarefaGeoWorker). A construção do QgsRasterLayer e o addMapLayer
# sempre acontecem depois, de volta na thread principal.
# ==========================================
def obter_bbox_wgs84_do_canvas():
    """Extensão atual do mapa (canvas), reprojetada para WGS84 (EPSG:4326).
    Deve ser chamada na thread principal (usa o mapCanvas da interface)."""
    canvas = qgis.utils.iface.mapCanvas()
    extensao = canvas.extent()
    crs_atual = canvas.mapSettings().destinationCrs()
    crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if crs_atual != crs_wgs84:
        transformador = QgsCoordinateTransform(crs_atual, crs_wgs84, QgsProject.instance())
        extensao = transformador.transformBoundingBox(extensao)
    return extensao.xMinimum(), extensao.yMinimum(), extensao.xMaximum(), extensao.yMaximum()

def centro_wgs84_da_camada(layer):
    """Centro (lon, lat) da extensão de uma camada, em WGS84. Também deve ser
    chamada na thread principal (usa QgsCoordinateTransform/QgsProject)."""
    crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    extensao = layer.extent()
    if layer.crs() != crs_wgs84:
        transformador = QgsCoordinateTransform(layer.crs(), crs_wgs84, QgsProject.instance())
        extensao = transformador.transformBoundingBox(extensao)
    centro = extensao.center()
    return centro.x(), centro.y()

def epsg_sirgas_utm_por_longitude(longitude):
    """SIRGAS 2000 / UTM 21S (EPSG:31981) a oeste de -54°, 22S (EPSG:31982) a leste."""
    return "EPSG:31982" if longitude >= -54.0 else "EPSG:31981"

def _caminho_temp_unico(prefixo, sufixo=".tif"):
    fd, caminho = tempfile.mkstemp(prefix=f"{prefixo}_", suffix=sufixo)
    os.close(fd)
    return caminho

def _nome_tile_copernicus(lat_int, lon_int):
    ns = "N" if lat_int >= 0 else "S"
    ew = "E" if lon_int >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_int):02d}_00_{ew}{abs(lon_int):03d}_00_DEM"

def _baixar_mde_copernicus_aws_arquivo(bbox_wgs84, nome_camada="MDE_Copernicus_AutoBaixado"):
    """Monta um mosaico dos blocos de 1x1 grau do Copernicus DEM GLO-30 (bucket
    público da AWS, sem necessidade de API key) e recorta pela extensão informada.
    Usa diretamente as bindings Python do GDAL (osgeo.gdal) em vez do algoritmo
    'gdal:buildvirtualraster' do Processing, pois este último valida o parâmetro
    INPUT como lista de camadas e rejeita strings /vsicurl/ em algumas builds."""
    from osgeo import gdal
    gdal.UseExceptions()

    xmin, ymin, xmax, ymax = bbox_wgs84

    urls = []
    for lat_int in range(math.floor(ymin), math.floor(ymax) + 1):
        for lon_int in range(math.floor(xmin), math.floor(xmax) + 1):
            tile = _nome_tile_copernicus(lat_int, lon_int)
            urls.append(f"/vsicurl/https://copernicus-dem-30m.s3.amazonaws.com/{tile}/{tile}.tif")

    if not urls:
        raise Exception("Não foi possível determinar os blocos do MDE para a extensão atual do mapa.")

    caminho_vrt = f"/vsimem/{nome_camada}_{os.getpid()}_{id(urls)}.vrt"
    try:
        vrt_ds = gdal.BuildVRT(caminho_vrt, urls)
    except Exception as e:
        raise Exception(f"Falha ao montar o mosaico do MDE via GDAL ({type(e).__name__}): {str(e)}")

    if vrt_ds is None:
        raise Exception("Falha ao montar o mosaico do MDE (nenhum bloco Copernicus acessível — verifique internet/proxy ou se a área tem cobertura).")

    caminho_saida = _caminho_temp_unico(nome_camada)
    try:
        resultado_ds = gdal.Translate(
            caminho_saida, vrt_ds,
            projWin=[xmin, ymax, xmax, ymin], projWinSRS='EPSG:4326'
        )
    except Exception as e:
        raise Exception(f"Falha ao recortar o MDE baixado ({type(e).__name__}): {str(e)}")
    finally:
        # Limpeza de um arquivo /vsimem/ (memória, não disco) — nunca deve
        # mascarar um gdal.Translate que já tinha dado certo caso falhe.
        vrt_ds = None
        try:
            gdal.Unlink(caminho_vrt)
        except Exception:
            pass

    if resultado_ds is None:
        raise Exception("Falha ao gerar o arquivo final do MDE recortado.")
    resultado_ds = None
    return caminho_saida

def _baixar_mde_opentopography_arquivo(bbox_wgs84, nome_camada="MDE_OpenTopography_AutoBaixado", demtype="COP30"):
    """Baixa o MDE via API REST do OpenTopography (exige API key gratuita).
    Usa urllib puro (thread-safe) em vez de QgsBlockingNetworkRequest, já que
    esta função roda dentro de uma QThread em segundo plano."""
    if not OPENTOPOGRAPHY_API_KEY:
        raise Exception("Nenhuma API Key do OpenTopography configurada (variável OPENTOPOGRAPHY_API_KEY no início do script).")

    xmin, ymin, xmax, ymax = bbox_wgs84
    url_str = (
        "https://portal.opentopography.org/API/globaldem"
        f"?demtype={demtype}&south={ymin}&north={ymax}&west={xmin}&east={xmax}"
        f"&outputFormat=GTiff&API_Key={OPENTOPOGRAPHY_API_KEY}"
    )

    try:
        with urllib.request.urlopen(url_str, timeout=120) as resposta:
            dados = resposta.read()
    except Exception as e:
        raise Exception(f"Falha na requisição ao OpenTopography: {str(e)}")

    if dados[:4] not in (b'II*\x00', b'MM\x00*'):
        raise Exception("A resposta do OpenTopography não é um GeoTIFF válido (verifique a API Key e os limites da área).")

    caminho_saida = _caminho_temp_unico(nome_camada)
    with open(caminho_saida, "wb") as arquivo_temp:
        arquivo_temp.write(dados)
    return caminho_saida

def _baixar_mde_automatico_arquivo(bbox_wgs84, nome_camada="MDE_Auto"):
    """Tenta o Copernicus DEM via AWS primeiro (sem API key); se falhar,
    tenta o OpenTopography (se houver API key configurada). Devolve
    (caminho_arquivo, nome_sugerido_da_camada)."""
    erros = []
    try:
        caminho = _baixar_mde_copernicus_aws_arquivo(bbox_wgs84, nome_camada=f"{nome_camada}_Copernicus")
        return caminho, f"{nome_camada}_Copernicus"
    except Exception as e:
        erros.append(f"Copernicus/AWS: {str(e)}")

    try:
        caminho = _baixar_mde_opentopography_arquivo(bbox_wgs84, nome_camada=f"{nome_camada}_OpenTopography")
        return caminho, f"{nome_camada}_OpenTopography"
    except Exception as e:
        erros.append(f"OpenTopography: {str(e)}")

    raise Exception(" | ".join(erros))

def reprojetar_raster_arquivo(caminho_origem, epsg_destino, nome_saida):
    """Reprojeta um raster (por caminho de arquivo) para o CRS informado
    (ex: 'EPSG:31981') usando gdal.Warp. Devolve o caminho do novo arquivo."""
    from osgeo import gdal
    gdal.UseExceptions()
    caminho_saida = _caminho_temp_unico(nome_saida)
    try:
        resultado_ds = gdal.Warp(caminho_saida, caminho_origem, dstSRS=epsg_destino, resampleAlg='bilinear')
    except Exception as e:
        raise Exception(f"Falha ao reprojetar o MDE ({type(e).__name__}): {str(e)}")
    if resultado_ds is None:
        raise Exception("Falha ao reprojetar o MDE (GDAL retornou vazio — verifique se o CRS de destino é válido).")
    resultado_ds = None
    return caminho_saida

def baixar_e_preparar_mde_arquivo(bbox_wgs84, epsg_destino=None, nome_camada="MDE_Auto"):
    """Baixa o MDE da extensão informada e, se epsg_destino for passado,
    já devolve reprojetado para essa projeção. Roda inteiramente em
    segundo plano (chamada a partir de TarefaGeoWorker)."""
    caminho, nome = _baixar_mde_automatico_arquivo(bbox_wgs84, nome_camada=nome_camada)
    if epsg_destino:
        nome_reprojetado = f"{nome}_{epsg_destino.replace(':', '')}"
        caminho = reprojetar_raster_arquivo(caminho, epsg_destino, nome_reprojetado)
        nome = nome_reprojetado
    return caminho, nome

# ==========================================
# DOWNLOAD AUTOMÁTICO DE BANDAS DE SATÉLITE (Microsoft Planetary Computer)
# Catálogo STAC público — sem necessidade de conta/API key. As URLs dos
# arquivos (COG) ficam num Azure Blob Storage e precisam ser "assinadas"
# antes de serem lidas via /vsicurl/.
# ==========================================
# Sensor usado pelo download automático — não perguntamos ao usuário porque
# só baixamos dessa fonte por padrão. O seletor de sensor só é mostrado
# quando a pessoa diz que já tem as bandas (aí sim varia).
SATELITE_PADRAO_DOWNLOAD = "Sentinel-2 (10m/20m)"

STAC_COLECAO_POR_SATELITE = {
    "Sentinel-2 (10m/20m)": "sentinel-2-l2a",
    "Landsat 8/9 (30m)": "landsat-c2-l2"
}

STAC_ASSET_POR_BANDA = {
    "Sentinel-2 (10m/20m)": {"BLUE": "B02", "GREEN": "B03", "RED": "B04", "NIR": "B08", "SWIR1": "B11", "SWIR2": "B12"},
    "Landsat 8/9 (30m)":    {"BLUE": "blue", "GREEN": "green", "RED": "red", "NIR": "nir08", "SWIR1": "swir16", "SWIR2": "swir22"}
}

def _consultar_stac_planetary_computer(bbox_wgs84, colecao, data_inicio, data_fim, nuvem_max=None, limit=4):
    """Faz uma requisição de busca ao catálogo STAC do Planetary Computer.
    Se nuvem_max for None, não aplica filtro de nuvem (usado para diagnóstico
    de 'qual foi a menor nuvem disponível' quando a busca filtrada não acha nada)."""
    xmin, ymin, xmax, ymax = bbox_wgs84
    corpo = {
        "collections": [colecao],
        "bbox": [xmin, ymin, xmax, ymax],
        "datetime": f"{data_inicio}T00:00:00Z/{data_fim}T23:59:59Z",
        "sortby": [{"field": "eo:cloud_cover", "direction": "asc"}],
        "limit": limit
    }
    if nuvem_max is not None:
        corpo["query"] = {"eo:cloud_cover": {"lt": nuvem_max}}

    dados_json = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(
        "https://planetarycomputer.microsoft.com/api/stac/v1/search",
        data=dados_json,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resposta:
            resultado = json.loads(resposta.read().decode("utf-8"))
    except Exception as e:
        raise Exception(f"Falha ao consultar o Planetary Computer: {str(e)}")
    return resultado.get("features", [])

def buscar_cenas_planetary_computer(bbox_wgs84, colecao, data_inicio, data_fim, nuvem_max, limit=4):
    """Busca, no catálogo STAC público do Microsoft Planetary Computer, até
    'limit' cenas que tocam a área/período/limite de nuvem informados. Pega um
    lote maior de candidatas (ordenadas da com menos nuvem para a com mais) e
    devolve só a melhor cena de cada célula/tile — pegar as N com menos nuvem
    "no geral" pode repetir a mesma célula várias vezes e deixar outra célula
    de fora, resultando num mosaico com buracos (área sem cobertura nenhuma)."""
    features = _consultar_stac_planetary_computer(bbox_wgs84, colecao, data_inicio, data_fim, nuvem_max=nuvem_max, limit=max(limit * 6, 20))

    if not features:
        # Diagnóstico: sem o filtro de nuvem, qual foi a menor nuvem disponível nesse período/área?
        try:
            features_sem_filtro = _consultar_stac_planetary_computer(bbox_wgs84, colecao, data_inicio, data_fim, nuvem_max=None, limit=1)
        except Exception:
            features_sem_filtro = []

        if features_sem_filtro:
            melhor_nuvem = features_sem_filtro[0].get("properties", {}).get("eo:cloud_cover")
            if isinstance(melhor_nuvem, (int, float)):
                raise Exception(
                    f"Nenhuma cena com nuvem < {nuvem_max}% nesse período/área. "
                    f"A cena menos nublada encontrada tem {melhor_nuvem:.1f}% de nuvem — "
                    "aumente o limite de nuvem ou amplie o período."
                )
            raise Exception(
                f"Nenhuma cena com nuvem < {nuvem_max}% nesse período/área, mas existem cenas "
                "mais nubladas — aumente o limite de nuvem ou amplie o período."
            )
        raise Exception(
            "Nenhuma cena encontrada nesse período/área, mesmo sem considerar nuvem. "
            "Tente ampliar o período — pode não haver passagem do satélite sobre essa área nesses dias."
        )

    # 'features' já vem ordenada por nuvem crescente — ao percorrer em ordem e
    # guardar só a 1ª ocorrência de cada célula, ficamos com a cena menos
    # nublada de CADA célula que toca a área, em vez de repetir a mesma célula.
    melhor_por_celula = {}
    for feature in features:
        propriedades = feature.get("properties", {})
        chave_celula = (
            propriedades.get("s2:mgrs_tile")
            or propriedades.get("landsat:wrs_path_row")
            or (f"{propriedades.get('landsat:wrs_path')}_{propriedades.get('landsat:wrs_row')}"
                if propriedades.get("landsat:wrs_path") is not None else None)
            or feature.get("id")
        )
        if chave_celula not in melhor_por_celula:
            melhor_por_celula[chave_celula] = feature

    return list(melhor_por_celula.values())[:limit]

def assinar_url_planetary_computer(url):
    """Assina uma URL de asset do Planetary Computer — necessário para poder
    ler o COG hospedado no Azure Blob Storage via /vsicurl/."""
    url_assinatura = "https://planetarycomputer.microsoft.com/api/sas/v1/sign?href=" + urllib.parse.quote(url, safe="")
    try:
        with urllib.request.urlopen(url_assinatura, timeout=30) as resposta:
            resultado = json.loads(resposta.read().decode("utf-8"))
    except Exception as e:
        raise Exception(f"Falha ao assinar a URL da banda: {str(e)}")
    return resultado["href"]

def listar_datas_disponiveis(bbox_wgs84, satelite, data_inicio, data_fim, nuvem_max, limit_bruto=50):
    """Busca cenas no período/área/nuvem informados e agrupa por DATA de
    captura, para o usuário escolher a data específica em vez do sistema
    decidir sozinho qual imagem baixar. Devolve uma lista de dicts
    {"data": "AAAA-MM-DD", "n_cenas": N, "nuvem_media": X, "features": [...]},
    ordenada da com menos nuvem (média) para a com mais."""
    colecao = STAC_COLECAO_POR_SATELITE.get(satelite)
    if not colecao:
        raise Exception(f"Satélite não suportado: {satelite}")

    features = _consultar_stac_planetary_computer(bbox_wgs84, colecao, data_inicio, data_fim, nuvem_max=nuvem_max, limit=limit_bruto)

    if not features:
        try:
            features_sem_filtro = _consultar_stac_planetary_computer(bbox_wgs84, colecao, data_inicio, data_fim, nuvem_max=None, limit=1)
        except Exception:
            features_sem_filtro = []
        if features_sem_filtro:
            melhor_nuvem = features_sem_filtro[0].get("properties", {}).get("eo:cloud_cover")
            if isinstance(melhor_nuvem, (int, float)):
                raise Exception(
                    f"Nenhuma cena com nuvem < {nuvem_max}% nesse período/área. "
                    f"A cena menos nublada encontrada tem {melhor_nuvem:.1f}% de nuvem — "
                    "aumente o limite de nuvem ou amplie o período."
                )
        raise Exception(
            "Nenhuma cena encontrada nesse período/área. "
            "Tente ampliar o período — pode não haver passagem do satélite sobre essa área nesses dias."
        )

    grupos = {}
    for feature in features:
        data_hora = feature.get("properties", {}).get("datetime", "")
        data = data_hora[:10] if data_hora else feature.get("id", "desconhecida")
        grupos.setdefault(data, []).append(feature)

    resultado = []
    for data, features_do_dia in grupos.items():
        nuvens = [
            f.get("properties", {}).get("eo:cloud_cover") for f in features_do_dia
            if isinstance(f.get("properties", {}).get("eo:cloud_cover"), (int, float))
        ]
        nuvem_media = (sum(nuvens) / len(nuvens)) if nuvens else None
        resultado.append({"data": data, "n_cenas": len(features_do_dia), "nuvem_media": nuvem_media, "features": features_do_dia})

    resultado.sort(key=lambda g: (g["nuvem_media"] if g["nuvem_media"] is not None else 999.0))
    return resultado

def _baixar_bandas_de_features(bbox_wgs84, satelite, chaves_bandas, features):
    """Baixa/recorta (via /vsicurl/ + gdal.Translate) as bandas indicadas em
    chaves_bandas a partir de uma lista de cenas JÁ ESCOLHIDA (uma data
    específica, ou o resultado de buscar_cenas_planetary_computer). Se
    houver mais de uma cena, monta um mosaico (gdal.BuildVRT) por banda
    antes de recortar. Devolve (dict {chave: caminho_arquivo},
    id_composto_das_cenas, nuvem_media)."""
    from osgeo import gdal
    gdal.UseExceptions()

    mapa_assets = STAC_ASSET_POR_BANDA.get(satelite)
    if not mapa_assets:
        raise Exception(f"Satélite não suportado para download automático: {satelite}")
    if not features:
        raise Exception("Nenhuma cena informada para baixar.")

    # No gdal.BuildVRT, a última fonte da lista "pinta por cima" nas sobreposições —
    # então colocamos a cena com MENOS nuvem por último (lista em ordem decrescente de nuvem).
    features_para_mosaico = list(reversed(features))

    xmin, ymin, xmax, ymax = bbox_wgs84
    resultado = {}

    for chave in chaves_bandas:
        asset_key = mapa_assets.get(chave)
        if not asset_key:
            raise Exception(f"Banda lógica desconhecida: {chave}")

        urls_vsicurl = []
        urls_descartadas = 0
        for feature in features_para_mosaico:
            assets_disponiveis = feature.get("assets", {})
            if asset_key not in assets_disponiveis:
                continue
            try:
                href_assinado = assinar_url_planetary_computer(assets_disponiveis[asset_key]["href"])
                fonte_candidata = f"/vsicurl/{href_assinado}"
                # Testa se o GDAL realmente abre a fonte antes de incluí-la no mosaico —
                # evita que uma única cena com token/URL ruim derrube a banda inteira
                # com um erro genérico do GDAL ("unknown error occurred").
                ds_teste = gdal.Open(fonte_candidata)
                if ds_teste is None:
                    urls_descartadas += 1
                    continue
                ds_teste = None
                urls_vsicurl.append(fonte_candidata)
            except Exception:
                urls_descartadas += 1
                continue

        if not urls_vsicurl:
            detalhe = f" ({urls_descartadas} cena(s) descartada(s) por falha ao abrir)" if urls_descartadas else ""
            raise Exception(f"Nenhuma cena válida encontrada para a banda {chave} ({asset_key}){detalhe}.")

        caminho_saida = _caminho_temp_unico(f"{chave}_mosaico")
        ds = None
        ultimo_erro = None
        # Falhas de rede em /vsicurl/ costumam ser pontuais — tenta até 3 vezes
        # antes de desistir, em vez de derrubar o download inteiro na primeira soluço.
        for tentativa in range(3):
            caminho_vrt = None
            vrt_ds = None
            try:
                if len(urls_vsicurl) == 1:
                    fonte = urls_vsicurl[0]
                else:
                    caminho_vrt = f"/vsimem/{chave}_{os.getpid()}_{id(urls_vsicurl)}_{tentativa}.vrt"
                    vrt_ds = gdal.BuildVRT(caminho_vrt, urls_vsicurl)
                    if vrt_ds is None:
                        raise Exception(f"Falha ao mosaicar a banda {chave} (VRT vazio).")
                    fonte = vrt_ds

                ds = gdal.Translate(
                    caminho_saida, fonte,
                    projWin=[xmin, ymax, xmax, ymin], projWinSRS='EPSG:4326'
                )
                ultimo_erro = None
                break
            except Exception as e:
                ultimo_erro = e
                if tentativa < 2:
                    time.sleep(1.5)
            finally:
                # Solta a referência ao dataset do VRT antes de tentar apagar o
                # arquivo /vsimem/. O Unlink em si é só limpeza de um arquivo
                # virtual em memória (não é disco real) e às vezes falha de forma
                # intermitente no GDAL ("unknown error occurred") mesmo sem
                # referências pendentes — por isso NUNCA deixamos essa falha
                # mascarar um gdal.Translate que já tinha dado certo.
                vrt_ds = None
                if caminho_vrt:
                    try:
                        gdal.Unlink(caminho_vrt)
                    except Exception:
                        pass

        if ultimo_erro is not None:
            raise Exception(f"Falha ao baixar/mosaicar a banda {chave} ({type(ultimo_erro).__name__}) após 3 tentativas: {str(ultimo_erro)}")

        if ds is None:
            raise Exception(f"Falha ao recortar a banda {chave} (resultado vazio — confira se a área está dentro das cenas encontradas).")
        ds = None
        resultado[chave] = caminho_saida

    ids_cenas = [f.get("id", "cena") for f in features]
    id_composto = ids_cenas[0] if len(ids_cenas) == 1 else f"{ids_cenas[0]}+{len(ids_cenas) - 1}"
    valores_nuvem = [
        f.get("properties", {}).get("eo:cloud_cover") for f in features
        if isinstance(f.get("properties", {}).get("eo:cloud_cover"), (int, float))
    ]
    nuvem_media = (sum(valores_nuvem) / len(valores_nuvem)) if valores_nuvem else None
    return resultado, id_composto, nuvem_media

def baixar_bandas_satelite_arquivo(bbox_wgs84, satelite, chaves_bandas, data_inicio, data_fim, nuvem_max, max_cenas=6):
    """Modo automático (sem escolha manual de data): busca e já baixa a
    melhor cena de cada célula sozinho. Mantido para reuso — a tela de
    download hoje usa listar_datas_disponiveis() + _baixar_bandas_de_features()
    para deixar a pessoa escolher a data."""
    colecao = STAC_COLECAO_POR_SATELITE.get(satelite)
    if not colecao:
        raise Exception(f"Satélite não suportado para download automático: {satelite}")
    features = buscar_cenas_planetary_computer(bbox_wgs84, colecao, data_inicio, data_fim, nuvem_max, limit=max_cenas)
    return _baixar_bandas_de_features(bbox_wgs84, satelite, chaves_bandas, features)

class TarefaGeoWorker(QThread):
    """QThread genérica: executa 'funcao(*args, **kwargs)' em segundo plano
    e emite o resultado (ou o erro) de volta para a thread principal via
    sinal, mantendo a interface do QGIS responsiva durante downloads/GDAL."""
    concluido = pyqtSignal(object)
    falhou = pyqtSignal(str)

    def __init__(self, funcao, *args, **kwargs):
        super().__init__()
        self.funcao = funcao
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            resultado = self.funcao(*self.args, **self.kwargs)
            self.concluido.emit(resultado)
        except Exception as e:
            self.falhou.emit(str(e))

def formatar_detalhes_mde(layer):
    """Monta um resumo em HTML, com fins didáticos, com resolução (convertida
    p/ metros quando o MDE está em CRS geográfico), faixa de elevação,
    dimensões e extensão do MDE — cada característica com uma explicação
    curta do que ela significa na prática."""
    provider = layer.dataProvider()
    stats = provider.bandStatistics(1, QgsRasterBandStats.Min | QgsRasterBandStats.Max)
    largura, altura = layer.rasterUnitsPerPixelX(), layer.rasterUnitsPerPixelY()

    if layer.crs().isGeographic():
        lat_central = layer.extent().center().y()
        fator = math.cos(math.radians(lat_central))
        largura_m = largura * 111320.0 * fator
        altura_m = altura * 110540.0
        texto_resolucao = f"~{largura_m:.1f} x {altura_m:.1f} m (estimado a partir de graus)"
    else:
        texto_resolucao = f"{largura:.2f} x {altura:.2f} m"

    amplitude = stats.maximumValue - stats.minimumValue
    extensao = layer.extent()
    nota = "color:#94A3B8; font-size:10px;"

    return (
        f"<b>📊 Características técnicas — {layer.name()}</b><br><br>"
        f"<b>Resolução espacial:</b> {texto_resolucao}<br>"
        f"<span style='{nota}'>Tamanho de cada pixel no terreno. Quanto menor, mais detalhe (e maior o arquivo).</span><br><br>"
        f"<b>Sistema de referência (CRS):</b> {layer.crs().authid()} — {layer.crs().description()}<br><br>"
        f"<b>Amplitude altimétrica:</b> {stats.minimumValue:.1f} m a {stats.maximumValue:.1f} m "
        f"(desnível de {amplitude:.1f} m)<br>"
        f"<span style='{nota}'>Diferença entre o ponto mais baixo e o mais alto da área — indica o quão acidentado é o relevo.</span><br><br>"
        f"<b>Dimensões da grade:</b> {layer.width()} x {layer.height()} pixels<br><br>"
        f"<b>Extensão geográfica:</b><br>"
        f"({extensao.xMinimum():.4f}, {extensao.yMinimum():.4f}) — "
        f"({extensao.xMaximum():.4f}, {extensao.yMaximum():.4f})"
    )

def criar_painel_educativo_mde():
    """Painel fixo, didático, explicando o que é um MDE — usado no Passo 1."""
    painel = QWidget()
    painel.setStyleSheet("background-color: #EEF2FF; border: 1px solid #C7D2FE; border-radius: 6px; padding: 10px; margin-top: 10px;")
    lay = QVBoxLayout(painel)
    lay.setContentsMargins(10, 10, 10, 10)
    titulo = QLabel("📚 <b>O que é um MDE?</b>")
    titulo.setStyleSheet("color: #4338CA; font-size: 12px;")
    texto = QLabel(
        "<b>Modelo Digital de Elevação (MDE)</b> é uma imagem raster em que cada "
        "pixel armazena a <b>altitude (Z)</b> do terreno naquele ponto — não é uma "
        "foto, é uma grade de números. É a base de dados para praticamente toda "
        "análise de relevo: declividade, orientação de vertentes, sombreamento "
        "(hillshade), delimitação de bacias hidrográficas e direção de fluxo "
        "d'água, entre outras."
    )
    texto.setWordWrap(True)
    texto.setStyleSheet("color: #4A5568; font-size: 11px;")
    lay.addWidget(titulo)
    lay.addWidget(texto)
    return painel

def criar_painel_detalhes_mde(texto_inicial="Selecione ou baixe um MDE para ver as características."):
    """Painel recolhível ('Ver mais ▾') com as características técnicas do
    MDE — o texto é atualizado via 'painel.lbl_conteudo.setText(...)' quando
    um MDE é escolhido/baixado. Começa fechado para não ocupar espaço."""
    painel = QWidget()
    painel.setStyleSheet("background-color: #EBF4FA; border: 1px solid #BEE3F8; border-radius: 6px; padding: 4px 12px; margin-top: 10px;")
    lay = QVBoxLayout(painel)
    lay.setContentsMargins(4, 4, 4, 4)

    TEXTO_FECHADO = "📊 Ver características técnicas  ▾"
    TEXTO_ABERTO = "📊 Ocultar características técnicas  ▴"

    btn_toggle = QPushButton(TEXTO_FECHADO)
    btn_toggle.setCursor(Qt.PointingHandCursor)
    btn_toggle.setStyleSheet("QPushButton { background-color: transparent; border: none; color: #2B6CB0; font-weight: bold; font-size: 12px; text-align: left; padding: 6px 0; } QPushButton:hover { color: #1E507E; }")

    lbl = QLabel(texto_inicial)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: #4A5568; font-size: 11px; padding: 2px 0 8px 0;")
    lbl.hide()

    def _alternar():
        aberto = lbl.isVisible()
        lbl.setVisible(not aberto)
        btn_toggle.setText(TEXTO_FECHADO if aberto else TEXTO_ABERTO)

    btn_toggle.clicked.connect(_alternar)

    lay.addWidget(btn_toggle)
    lay.addWidget(lbl)

    painel.lbl_conteudo = lbl
    return painel

ESTILO_GROUPBOX = f"""
    QGroupBox {{ font-weight: bold; color: {COR_PRIMARIA}; border: 1px solid #CBD5E0; border-radius: 6px; margin-top: 10px; padding-top: 15px; font-size: 13px; }}
    QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; left: 10px; }}
"""
ESTILO_COMBO = "QComboBox { border: 1px solid #CBD5E0; border-radius: 4px; padding: 6px 10px; background-color: white; font-size: 12px; color: #2D3748; } QComboBox:hover { border: 1px solid #1E507E; }"
ESTILO_INPUT = "QLineEdit { border: 1px solid #CBD5E0; border-radius: 4px; padding: 6px 10px; background-color: white; font-size: 12px; color: #2D3748; } QLineEdit:hover { border: 1px solid #1E507E; }"

# ==========================================
# MÓDULOS DE AÇÃO (Telas de Configuração)
# ==========================================
class RascunhoDialog(QDialog):
    def __init__(self, parent=None):
        super(RascunhoDialog, self).__init__(parent)
        self.setWindowTitle("Nova Camada")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.resize(350, 200) 
        self.setStyleSheet("background-color: #ffffff; color: #333;")
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        
        layout.addWidget(QLabel("<b style='font-size: 13px;'>Tipo de Geometria:</b>"))
        self.combo_geom = QComboBox()
        self.combo_geom.addItems(["Polígono (Ex: Lote, Área de Supressão)", "Linha (Ex: Drenagem, Cerca)", "Ponto (Ex: Árvore Isolada)"])
        self.combo_geom.setStyleSheet(ESTILO_COMBO)
        layout.addWidget(self.combo_geom)
        
        layout.addWidget(QLabel("<b style='font-size: 13px;'>Sistema de Coordenadas (EPSG):</b>"))
        self.combo_epsg = QComboBox()
        self.combo_epsg.addItems([
            "EPSG:31981 - SIRGAS 2000 / UTM zone 21S",
            "EPSG:4674 - SIRGAS 2000 (Geográfico)",
            "EPSG:31982 - SIRGAS 2000 / UTM zone 22S"
        ])
        self.combo_epsg.setStyleSheet(ESTILO_COMBO)
        layout.addWidget(self.combo_epsg)
        
        layout.addStretch()
        btn_layout = QHBoxLayout()
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setStyleSheet("background-color: #e74c3c; color: white; font-weight: bold; padding: 8px; border-radius: 4px;")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton("Criar Geometria")
        self.btn_ok.setStyleSheet(f"background-color: {COR_DESTAQUE}; color: white; font-weight: bold; padding: 8px; border-radius: 4px;")
        self.btn_ok.clicked.connect(self.accept)
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_ok)
        layout.addLayout(btn_layout)
        self.setLayout(layout)
        
    def get_data(self):
        geom_map = {"Polígono (Ex: Lote, Área de Supressão)": "Polygon", "Linha (Ex: Drenagem, Cerca)": "LineString", "Ponto (Ex: Árvore Isolada)": "Point"}
        epsg_codigo = self.combo_epsg.currentText().split(':')[1].split(' ')[0]
        return geom_map[self.combo_geom.currentText()], epsg_codigo

class BufferDialog(QDialog):
    def __init__(self, parent=None):
        super(BufferDialog, self).__init__(parent)
        self.setWindowTitle("Configurar APP / Área de Influência")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.resize(380, 480) 
        self.setStyleSheet("background-color: #ffffff; color: #333;")
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        
        self.layers = {layer.name(): layer for layer in QgsProject.instance().mapLayers().values() if isinstance(layer, QgsVectorLayer)}
        
        layout.addWidget(QLabel("<b style='font-size: 13px;'>Camada Base (Ex: Drenagem):</b>"))
        self.combo_layer = QComboBox()
        if self.layers:
            self.combo_layer.addItems(self.layers.keys())
        else:
            self.combo_layer.addItem("Nenhuma camada vetorial encontrada")
            self.combo_layer.setEnabled(False)
        self.combo_layer.setStyleSheet(ESTILO_COMBO)
        layout.addWidget(self.combo_layer)
        
        dist_layout = QHBoxLayout()
        dist_box = QVBoxLayout()
        dist_box.addWidget(QLabel("<b style='font-size: 13px;'>Afastamento/Raio:</b>"))
        self.input_distance = QLineEdit("30.0")
        self.input_distance.setStyleSheet(ESTILO_INPUT)
        dist_box.addWidget(self.input_distance)
        
        unit_box = QVBoxLayout()
        unit_box.addWidget(QLabel("<b style='font-size: 13px;'>Unidade:</b>"))
        self.combo_unit = QComboBox()
        modelo_unidades = QStandardItemModel()
        for texto_unidade in ["metros", "quilômetros", "hectares (indisponível)"]:
            item_unidade = QStandardItem(texto_unidade)
            if "indisponível" in texto_unidade:
                item_unidade.setEnabled(False)
            modelo_unidades.appendRow(item_unidade)
        self.combo_unit.setModel(modelo_unidades)
        self.combo_unit.setStyleSheet(ESTILO_COMBO)
        unit_box.addWidget(self.combo_unit)
        
        dist_layout.addLayout(dist_box)
        dist_layout.addLayout(unit_box)
        layout.addLayout(dist_layout)
        
        layout.addWidget(QLabel("<b style='font-size: 13px;'>Segmentos de Suavização:</b>"))
        self.input_segments = QLineEdit("5")
        self.input_segments.setStyleSheet(ESTILO_INPUT)
        layout.addWidget(self.input_segments)
        
        layout.addWidget(QLabel("<b style='font-size: 13px;'>Estilo da Cobertura:</b>"))
        self.combo_cap = QComboBox()
        self.combo_cap.addItems(["Arredondado (Padrão APP)", "Plano", "Quadrado"])
        self.combo_cap.setStyleSheet(ESTILO_COMBO)
        layout.addWidget(self.combo_cap)
        
        self.check_dissolve = QCheckBox("Dissolver resultados (Importante para APPs contíguas)")
        self.check_dissolve.setChecked(True)
        self.check_dissolve.setStyleSheet("font-size: 13px; margin-top: 10px;")
        layout.addWidget(self.check_dissolve)
        
        layout.addStretch()
        btn_layout = QHBoxLayout()
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setStyleSheet("background-color: #e74c3c; color: white; font-weight: bold; padding: 8px; border-radius: 4px;")
        self.btn_cancel.clicked.connect(self.reject)
        
        self.btn_ok = QPushButton("Gerar Polígono")
        self.btn_ok.setStyleSheet(f"background-color: {COR_DESTAQUE}; color: white; font-weight: bold; padding: 8px; border-radius: 4px;")
        if not self.layers:
            self.btn_ok.setEnabled(False)
            self.btn_ok.setStyleSheet("background-color: #bdc3c7; color: white; font-weight: bold; padding: 8px; border-radius: 4px;")
        self.btn_ok.clicked.connect(self.accept)
        
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_ok)
        layout.addLayout(btn_layout)
        self.setLayout(layout)
        
    def get_data(self):
        return self.combo_layer.currentText(), self.input_distance.text().strip(), self.combo_unit.currentText(), self.input_segments.text().strip(), self.combo_cap.currentText().split(' ')[0], self.check_dissolve.isChecked()

class AnaliseMultiespectralDialog(QDialog):
    NOMES_BANDAS_PT = {
        "BLUE": "Azul", "GREEN": "Verde", "RED": "Vermelho",
        "NIR": "Infravermelho Próximo (NIR)", "SWIR1": "Infrav. Curto 1 (SWIR1)", "SWIR2": "Infrav. Curto 2 (SWIR2)"
    }

    def __init__(self, parent=None):
        super(AnaliseMultiespectralDialog, self).__init__(parent)
        self.setWindowTitle("Laboratório Analítico (GDAL e Topografia)")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.resize(480, 640)

        self.rasters = {layer.name(): layer for layer in QgsProject.instance().mapLayers().values() if isinstance(layer, QgsRasterLayer)}
        self.chaves_bandas_necessarias = []
        self._worker = None
        self._pagina_anterior = None

        self._spinner_frames = ["🛰️", "📡", "🌐", "📡"]
        self._spinner_index = 0
        self.timer_spinner = QTimer(self)
        self.timer_spinner.timeout.connect(self._animar_spinner)

        layout_raiz = QVBoxLayout(self)
        layout_raiz.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedWidget()
        layout_raiz.addWidget(self.stack)

        self.pagina_indice = self._criar_pagina_indice()
        self.pagina_bandas_pergunta = self._criar_pagina_bandas_pergunta()
        self.pagina_download = self._criar_pagina_download()
        self.pagina_config = self._criar_pagina_config()
        self.pagina_carregando = self._criar_pagina_carregando()

        self.stack.addWidget(self.pagina_indice)
        self.stack.addWidget(self.pagina_bandas_pergunta)
        self.stack.addWidget(self.pagina_download)
        self.stack.addWidget(self.pagina_config)
        self.stack.addWidget(self.pagina_carregando)
        self.stack.setCurrentWidget(self.pagina_indice)

        # Só agora todas as páginas existem (lbl_banda_1 etc. vivem na pagina_config,
        # criada por último) — é seguro chamar atualizar_interface() pela 1ª vez.
        self.atualizar_lista_indices()

    # ------------------------------------------------------------------
    # PÁGINA 1: o que analisar (sensor + categoria + índice)
    # ------------------------------------------------------------------
    def _criar_pagina_indice(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: #F8FAFC; font-family: 'Segoe UI', Arial;")
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(20, 22, 20, 20)
        layout.setSpacing(10)

        lbl_icone = QLabel("🛰️")
        lbl_icone.setAlignment(Qt.AlignCenter)
        lbl_icone.setStyleSheet("font-size: 40px;")
        layout.addWidget(lbl_icone)

        lbl_titulo = QLabel("Laboratório Analítico")
        lbl_titulo.setAlignment(Qt.AlignCenter)
        lbl_titulo.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {COR_PRIMARIA}; margin-bottom: 8px;")
        layout.addWidget(lbl_titulo)

        # O sensor não é perguntado aqui: quem baixa automaticamente com a
        # gente sempre recebe Sentinel-2 (fixado em SATELITE_PADRAO_DOWNLOAD);
        # o seletor de sensor só aparece depois, na tela de mapeamento, e só
        # para quem responder "já tenho essas bandas" (aí sim precisamos saber
        # de qual sensor são os rasters que a pessoa já carregou).
        self.combo_sat = QComboBox()
        self.combo_sat.addItems(["Sentinel-2 (10m/20m)", "Landsat 8/9 (30m)"])
        self.combo_sat.setCurrentText(SATELITE_PADRAO_DOWNLOAD)
        self.combo_sat.currentIndexChanged.connect(self.atualizar_interface)

        group_config = QGroupBox("O que você quer analisar?")
        group_config.setStyleSheet(ESTILO_GROUPBOX)
        layout_config = QGridLayout(group_config)
        layout_config.setContentsMargins(15, 20, 15, 15)

        lbl_cat = QLabel("Categoria de Análise:")
        lbl_cat.setStyleSheet("color: #4A5568;")
        self.combo_cat = QComboBox()
        self.combo_cat.addItems(["Vegetação", "Água", "Solo e Áreas Expostas", "Queimada / Estresse", "Áreas Urbanas", "Composição Visual (RGB)", "Topografia / Relevo"])
        self.combo_cat.setStyleSheet(ESTILO_COMBO)
        self.combo_cat.currentIndexChanged.connect(self.atualizar_lista_indices)

        lbl_alg = QLabel("Algoritmo / Índice:")
        lbl_alg.setStyleSheet("color: #4A5568;")
        self.combo_analise = QComboBox()
        self.combo_analise.setStyleSheet(ESTILO_COMBO)
        self.combo_analise.currentIndexChanged.connect(self.atualizar_interface)

        layout_config.addWidget(lbl_cat, 0, 0)
        layout_config.addWidget(self.combo_cat, 0, 1)
        layout_config.addWidget(lbl_alg, 1, 0)
        layout_config.addWidget(self.combo_analise, 1, 1)
        layout.addWidget(group_config)

        self.painel_info = QWidget()
        self.painel_info.setStyleSheet("background-color: #EBF4FA; border: 1px solid #BEE3F8; border-radius: 6px; padding: 10px;")
        info_layout = QVBoxLayout(self.painel_info)
        info_layout.setContentsMargins(10, 10, 10, 10)
        self.lbl_info_titulo = QLabel("ℹ️ <b>O que é este Índice?</b>")
        self.lbl_info_titulo.setStyleSheet("color: #2B6CB0; font-size: 12px;")
        self.lbl_info_texto = QLabel("Selecione um índice acima para ver a explicação técnica.")
        self.lbl_info_texto.setStyleSheet("color: #4A5568; font-size: 11px;")
        self.lbl_info_texto.setWordWrap(True)
        info_layout.addWidget(self.lbl_info_titulo)
        info_layout.addWidget(self.lbl_info_texto)
        layout.addWidget(self.painel_info)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_cancelar = QPushButton("Cancelar")
        btn_cancelar.setCursor(Qt.PointingHandCursor)
        btn_cancelar.setStyleSheet("QPushButton { background-color: transparent; color: #718096; font-weight: bold; border: none; } QPushButton:hover { color: #E53E3E; text-decoration: underline; }")
        btn_cancelar.clicked.connect(self.reject)

        btn_avancar = QPushButton("Avançar ➜")
        btn_avancar.setCursor(Qt.PointingHandCursor)
        btn_avancar.setStyleSheet(f"QPushButton {{ background-color: {COR_PRIMARIA}; color: white; font-weight: bold; padding: 8px 15px; border-radius: 4px; border: none; font-size: 12px; }} QPushButton:hover {{ background-color: {COR_PRIMARIA_HOVER}; }}")
        btn_avancar.clicked.connect(self._ir_apos_indice)

        btn_layout.addWidget(btn_cancelar)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_avancar)
        layout.addLayout(btn_layout)

        return pagina

    def _ir_apos_indice(self):
        if not self.combo_analise.currentText():
            self._reportar_erro("Selecione um índice antes de continuar.")
            return
        if self.combo_cat.currentText() == "Topografia / Relevo":
            self._ir_para_config()
        else:
            self._ir_para_bandas_pergunta()

    # ------------------------------------------------------------------
    # PÁGINA 2: pergunta — já tem as bandas necessárias para este índice?
    # ------------------------------------------------------------------
    def _criar_pagina_bandas_pergunta(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: #F8FAFC; font-family: 'Segoe UI', Arial;")
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(30, 30, 30, 20)
        layout.addStretch()

        lbl_icone = QLabel("🗂️")
        lbl_icone.setAlignment(Qt.AlignCenter)
        lbl_icone.setStyleSheet("font-size: 46px;")
        layout.addWidget(lbl_icone)

        self.lbl_bandas_necessarias = QLabel()
        self.lbl_bandas_necessarias.setAlignment(Qt.AlignCenter)
        self.lbl_bandas_necessarias.setWordWrap(True)
        self.lbl_bandas_necessarias.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {COR_PRIMARIA}; margin: 14px 0 6px 0;")
        layout.addWidget(self.lbl_bandas_necessarias)

        lbl_pergunta = QLabel("Você já tem essas bandas carregadas no projeto?")
        lbl_pergunta.setAlignment(Qt.AlignCenter)
        lbl_pergunta.setWordWrap(True)
        lbl_pergunta.setStyleSheet("color: #718096; font-size: 12px; margin-bottom: 20px;")
        layout.addWidget(lbl_pergunta)

        btn_tenho = QPushButton("✅  Sim, já tenho essas bandas")
        btn_tenho.setCursor(Qt.PointingHandCursor)
        btn_tenho.setStyleSheet(f"QPushButton {{ background-color: {COR_PRIMARIA}; color: white; font-weight: bold; padding: 13px; border-radius: 8px; border: none; font-size: 13px; }} QPushButton:hover {{ background-color: {COR_PRIMARIA_HOVER}; }}")
        btn_tenho.clicked.connect(lambda: self._ir_para_config(mostrar_sensor=True))
        layout.addWidget(btn_tenho)

        btn_nao_tenho = QPushButton("🛰️  Não, quero baixar automaticamente")
        btn_nao_tenho.setCursor(Qt.PointingHandCursor)
        btn_nao_tenho.setStyleSheet("QPushButton { background-color: #0D9488; color: white; font-weight: bold; padding: 13px; border-radius: 8px; border: none; font-size: 13px; margin-top: 10px; } QPushButton:hover { background-color: #0F766E; }")
        btn_nao_tenho.clicked.connect(self._ir_para_download_forcando_sentinel)
        layout.addWidget(btn_nao_tenho)

        layout.addStretch()

        btn_voltar = QPushButton("‹ Voltar")
        btn_voltar.setCursor(Qt.PointingHandCursor)
        btn_voltar.setStyleSheet("QPushButton { background-color: transparent; color: #718096; font-weight: bold; border: none; padding: 6px; } QPushButton:hover { color: #1E507E; text-decoration: underline; }")
        btn_voltar.clicked.connect(lambda: self.stack.setCurrentWidget(self.pagina_indice))
        layout.addWidget(btn_voltar, alignment=Qt.AlignCenter)

        return pagina

    def _ir_para_bandas_pergunta(self):
        texto = ", ".join(self.NOMES_BANDAS_PT.get(c, c) for c in self.chaves_bandas_necessarias)
        self.lbl_bandas_necessarias.setText(f"Este índice precisa das bandas:<br>{texto}")
        self.stack.setCurrentWidget(self.pagina_bandas_pergunta)

    def _ir_para_download_forcando_sentinel(self):
        # Só baixamos automaticamente do Sentinel-2 — fixa o sensor aqui pra
        # garantir que os rótulos de banda na tela de mapeamento batam com o
        # que realmente foi baixado, sem precisar perguntar isso ao usuário.
        self.combo_sat.blockSignals(True)
        self.combo_sat.setCurrentText(SATELITE_PADRAO_DOWNLOAD)
        self.combo_sat.blockSignals(False)
        self.atualizar_interface()
        self.stack.setCurrentWidget(self.pagina_download)

    # ------------------------------------------------------------------
    # PÁGINA 3: baixar só as bandas deste índice (compacta)
    # ------------------------------------------------------------------
    def _criar_pagina_download(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: #F8FAFC; font-family: 'Segoe UI', Arial;")
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(20, 20, 20, 20)

        group = QGroupBox("🔍 Buscar Cenas Disponíveis")
        group.setStyleSheet(ESTILO_GROUPBOX)
        g_layout = QVBoxLayout(group)
        g_layout.setContentsMargins(15, 18, 15, 15)
        g_layout.setSpacing(8)

        hoje = date.today()
        lbl_data_ini = QLabel("Data inicial:")
        self.input_data_inicio = QLineEdit((hoje - timedelta(days=90)).isoformat())
        self.input_data_inicio.setPlaceholderText("AAAA-MM-DD")
        self.input_data_inicio.setStyleSheet(ESTILO_INPUT)

        lbl_data_fim = QLabel("Data final:")
        self.input_data_fim = QLineEdit(hoje.isoformat())
        self.input_data_fim.setPlaceholderText("AAAA-MM-DD")
        self.input_data_fim.setStyleSheet(ESTILO_INPUT)

        lbl_nuvem = QLabel("Nuvem máxima (%):")
        self.input_nuvem_max = QLineEdit("20")
        self.input_nuvem_max.setStyleSheet(ESTILO_INPUT)

        self.btn_buscar_cenas = QPushButton("🔍 Buscar Cenas Disponíveis")
        self.btn_buscar_cenas.setCursor(Qt.PointingHandCursor)
        self.btn_buscar_cenas.setToolTip("Lista, no Microsoft Planetary Computer, as datas com imagem disponível nesse período/área/nuvem — você escolhe qual baixar")
        self.btn_buscar_cenas.setStyleSheet("QPushButton { background-color: #0D9488; color: white; font-weight: bold; border-radius: 4px; padding: 10px; font-size: 12px; } QPushButton:hover { background-color: #0F766E; }")
        self.btn_buscar_cenas.clicked.connect(self.buscar_cenas_disponiveis)

        g_layout.addWidget(lbl_data_ini)
        g_layout.addWidget(self.input_data_inicio)
        g_layout.addWidget(lbl_data_fim)
        g_layout.addWidget(self.input_data_fim)
        g_layout.addWidget(lbl_nuvem)
        g_layout.addWidget(self.input_nuvem_max)
        g_layout.addWidget(self.btn_buscar_cenas)
        layout.addWidget(group)

        self.group_lista_datas = QGroupBox("Datas encontradas (menos nuvem primeiro)")
        self.group_lista_datas.setStyleSheet(ESTILO_GROUPBOX)
        lay_lista = QVBoxLayout(self.group_lista_datas)
        lay_lista.setContentsMargins(15, 18, 15, 15)
        self.lista_datas = QListWidget()
        self.lista_datas.setStyleSheet(
            "QListWidget { border: 1px solid #CBD5E0; border-radius: 4px; background-color: white; font-size: 12px; } "
            "QListWidget::item { padding: 6px; } "
            "QListWidget::item:selected { background-color: #1E507E; color: white; }"
        )
        self.lista_datas.setMaximumHeight(150)
        self.lista_datas.itemSelectionChanged.connect(self._data_selecionada_mudou)
        lay_lista.addWidget(self.lista_datas)
        layout.addWidget(self.group_lista_datas)
        self.group_lista_datas.hide()

        painel_edu = QWidget()
        painel_edu.setStyleSheet("background-color: #EEF2FF; border: 1px solid #C7D2FE; border-radius: 6px; padding: 10px; margin-top: 10px;")
        lay_edu = QVBoxLayout(painel_edu)
        lay_edu.setContentsMargins(10, 10, 10, 10)
        titulo_edu = QLabel("📚 <b>O que é uma imagem multiespectral?</b>")
        titulo_edu.setStyleSheet("color: #4338CA; font-size: 12px;")
        texto_edu = QLabel(
            "Satélites como o <b>Sentinel-2</b> e o <b>Landsat</b> registram a luz refletida "
            "pela superfície em várias <b>bandas espectrais</b>, muito além do que o olho "
            "humano vê (RGB). Vegetação saudável reflete fortemente no <b>infravermelho "
            "próximo (NIR)</b> e absorve vermelho — é essa diferença que índices como o "
            "NDVI exploram para 'enxergar' o que uma foto comum não mostra."
        )
        texto_edu.setWordWrap(True)
        texto_edu.setStyleSheet("color: #4A5568; font-size: 11px;")
        lay_edu.addWidget(titulo_edu)
        lay_edu.addWidget(texto_edu)
        layout.addWidget(painel_edu)

        self.lbl_status_download = QLabel("Busque as cenas disponíveis para ver as datas com imagem nessa área.")
        self.lbl_status_download.setWordWrap(True)
        self.lbl_status_download.setStyleSheet("background-color: #F0FDF4; border: 1px solid #BBF7D0; border-radius: 6px; padding: 8px 10px; color: #15803D; font-size: 11px; margin-top: 10px;")
        layout.addWidget(self.lbl_status_download)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_voltar = QPushButton("‹ Voltar")
        btn_voltar.setCursor(Qt.PointingHandCursor)
        btn_voltar.setStyleSheet("QPushButton { background-color: transparent; color: #718096; font-weight: bold; border: none; } QPushButton:hover { color: #1E507E; text-decoration: underline; }")
        btn_voltar.clicked.connect(lambda: self.stack.setCurrentWidget(self.pagina_bandas_pergunta))

        self.btn_baixar_bandas = QPushButton("🛰️ Baixar Bandas desta Data")
        self.btn_baixar_bandas.setCursor(Qt.PointingHandCursor)
        self.btn_baixar_bandas.setStyleSheet("QPushButton { background-color: #0D9488; color: white; font-weight: bold; border-radius: 4px; padding: 8px 15px; font-size: 12px; } QPushButton:hover { background-color: #0F766E; } QPushButton:disabled { background-color: #A0AEC0; }")
        self.btn_baixar_bandas.setEnabled(False)
        self.btn_baixar_bandas.clicked.connect(self.baixar_bandas_da_data_selecionada)

        self.btn_avancar_download = QPushButton("Avançar ➜")
        self.btn_avancar_download.setCursor(Qt.PointingHandCursor)
        self.btn_avancar_download.setStyleSheet(f"QPushButton {{ background-color: {COR_PRIMARIA}; color: white; font-weight: bold; padding: 8px 15px; border-radius: 4px; border: none; font-size: 12px; }} QPushButton:hover {{ background-color: {COR_PRIMARIA_HOVER}; }}")
        self.btn_avancar_download.setEnabled(False)
        self.btn_avancar_download.clicked.connect(lambda: self._ir_para_config(mostrar_sensor=False))

        btn_layout.addWidget(btn_voltar)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_baixar_bandas)
        btn_layout.addWidget(self.btn_avancar_download)
        layout.addLayout(btn_layout)

        return pagina

    def buscar_cenas_disponiveis(self):
        if not self.chaves_bandas_necessarias:
            self._reportar_erro("Nenhuma banda necessária identificada — volte e escolha um índice.")
            return

        data_inicio = self.input_data_inicio.text().strip()
        data_fim = self.input_data_fim.text().strip()
        try:
            datetime.strptime(data_inicio, "%Y-%m-%d")
            datetime.strptime(data_fim, "%Y-%m-%d")
        except ValueError:
            self._reportar_erro("Datas inválidas. Use o formato AAAA-MM-DD.")
            return

        try:
            nuvem_max = float(self.input_nuvem_max.text().strip().replace(',', '.'))
        except ValueError:
            self._reportar_erro("Informe um valor numérico para a nuvem máxima (%).")
            return

        try:
            bbox = obter_bbox_wgs84_do_canvas()
        except Exception as e:
            self._reportar_erro(f"Falha ao obter a extensão do mapa: {str(e)}")
            return

        self._bbox_busca_atual = bbox
        satelite = self.combo_sat.currentText()
        self._iniciar_carregamento(f"Buscando cenas {satelite} disponíveis...\n(Planetary Computer)")

        self._worker = TarefaGeoWorker(listar_datas_disponiveis, bbox, satelite, data_inicio, data_fim, nuvem_max)
        self._worker.concluido.connect(self._busca_datas_concluida)
        self._worker.falhou.connect(self._busca_datas_falhou)
        self._worker.start()

    def _busca_datas_concluida(self, grupos):
        self._parar_carregamento()
        self._grupos_datas = grupos
        self.lista_datas.clear()
        self.btn_baixar_bandas.setEnabled(False)

        for grupo in grupos:
            nuvem_txt = f"{grupo['nuvem_media']:.1f}%" if isinstance(grupo['nuvem_media'], (int, float)) else "desconhecida"
            texto = f"📅 {grupo['data']}   —   {grupo['n_cenas']} cena(s)   —   nuvem média: {nuvem_txt}"
            self.lista_datas.addItem(QListWidgetItem(texto))

        self.group_lista_datas.show()
        if self.lista_datas.count() > 0:
            self.lista_datas.setCurrentRow(0)

        self.lbl_status_download.setText(f"✅ {len(grupos)} data(s) encontrada(s). Selecione uma e clique em 'Baixar Bandas desta Data'.")
        self._reportar_sucesso(f"{len(grupos)} data(s) disponível(is) encontrada(s).")

    def _busca_datas_falhou(self, mensagem):
        self._parar_carregamento()
        self.group_lista_datas.hide()
        self._reportar_erro(f"Falha ao buscar cenas disponíveis: {mensagem}")

    def _data_selecionada_mudou(self):
        self.btn_baixar_bandas.setEnabled(self.lista_datas.currentRow() >= 0)

    def baixar_bandas_da_data_selecionada(self):
        indice = self.lista_datas.currentRow()
        if indice < 0 or not getattr(self, "_grupos_datas", None) or indice >= len(self._grupos_datas):
            self._reportar_erro("Selecione uma data na lista antes de baixar.")
            return

        grupo = self._grupos_datas[indice]
        satelite = self.combo_sat.currentText()
        bandas_pt = ", ".join(self.chaves_bandas_necessarias)
        self._iniciar_carregamento(f"Baixando bandas de {grupo['data']}...\nBandas: {bandas_pt}")

        self._worker = TarefaGeoWorker(
            _baixar_bandas_de_features, self._bbox_busca_atual, satelite,
            list(self.chaves_bandas_necessarias), grupo["features"]
        )
        self._worker.concluido.connect(self._download_bandas_concluido)
        self._worker.falhou.connect(self._download_bandas_falhou)
        self._worker.start()

    def _download_bandas_concluido(self, resultado):
        dict_bandas, id_cena, nuvem_cena = resultado
        self._parar_carregamento()

        nomes_carregados = []
        for chave, caminho in dict_bandas.items():
            nome_camada = f"{chave}_{id_cena}"
            camada = QgsRasterLayer(caminho, nome_camada)
            if not camada.isValid():
                self._reportar_erro(f"A banda {chave} baixada não é válida.")
                continue
            QgsProject.instance().addMapLayer(camada)
            self.rasters[camada.name()] = camada
            nomes_carregados.append(camada.name())

        nuvem_txt = f"{nuvem_cena:.1f}%" if isinstance(nuvem_cena, (int, float)) else "desconhecida"

        if nomes_carregados:
            self.lbl_status_download.setText(
                f"✅ Bandas da cena '{id_cena}' (nuvem: {nuvem_txt}):<br>{', '.join(nomes_carregados)}"
            )
            self.btn_avancar_download.setEnabled(True)
            self._reportar_sucesso(f"{len(nomes_carregados)} banda(s) baixada(s) da cena '{id_cena}'.")
        else:
            self._reportar_erro("Nenhuma banda válida foi baixada.")

    def _download_bandas_falhou(self, mensagem):
        self._parar_carregamento()
        self._reportar_erro(f"Falha ao baixar as bandas automaticamente: {mensagem}")

    # ------------------------------------------------------------------
    # PÁGINA 4: mapeamento final de bandas + executar
    # ------------------------------------------------------------------
    def _criar_pagina_config(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: #F8FAFC; font-family: 'Segoe UI', Arial;")
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        self.lbl_resumo_indice = QLabel()
        self.lbl_resumo_indice.setWordWrap(True)
        self.lbl_resumo_indice.setStyleSheet("color: #718096; font-size: 11px; margin-bottom: 4px;")
        layout.addWidget(self.lbl_resumo_indice)

        # Só aparece quando a pessoa respondeu "já tenho essas bandas" — é
        # quando realmente precisamos saber de qual sensor são os rasters
        # que ela já carregou. No caminho de download automático fica
        # escondido (o sensor já está fixo em Sentinel-2, sem perguntar).
        self.group_sensor = QGroupBox("De qual sensor são essas bandas?")
        self.group_sensor.setStyleSheet(ESTILO_GROUPBOX)
        layout_sensor = QVBoxLayout(self.group_sensor)
        layout_sensor.setContentsMargins(15, 20, 15, 15)
        layout_sensor.addWidget(self.combo_sat)
        self.combo_sat.setStyleSheet(ESTILO_COMBO)
        layout.addWidget(self.group_sensor)
        self.group_sensor.hide()

        group_bandas = QGroupBox("Mapeamento de Matrizes (Rasters)")
        group_bandas.setStyleSheet(ESTILO_GROUPBOX)
        layout_bandas = QVBoxLayout(group_bandas)
        layout_bandas.setContentsMargins(15, 20, 15, 15)

        self.lbl_banda_1 = QLabel()
        self.lbl_banda_1.setStyleSheet("font-size: 11px;")
        self.combo_banda_1 = QComboBox()
        self.combo_banda_1.setStyleSheet(ESTILO_COMBO)

        self.btn_baixar_mde = QPushButton("🌎 Baixar MDE")
        self.btn_baixar_mde.setCursor(Qt.PointingHandCursor)
        self.btn_baixar_mde.setToolTip("Baixa automaticamente um MDE (Copernicus DEM 30m) cobrindo a extensão atual do mapa")
        self.btn_baixar_mde.setStyleSheet("QPushButton { background-color: #0D9488; color: white; font-weight: bold; border-radius: 4px; padding: 6px 10px; font-size: 11px; } QPushButton:hover { background-color: #0F766E; }")
        self.btn_baixar_mde.clicked.connect(self.baixar_mde_da_extensao)
        self.btn_baixar_mde.hide()

        box_banda_1 = QHBoxLayout()
        box_banda_1.addWidget(self.combo_banda_1)
        box_banda_1.addWidget(self.btn_baixar_mde)

        self.lbl_banda_2 = QLabel()
        self.lbl_banda_2.setStyleSheet("font-size: 11px; margin-top: 5px;")
        self.combo_banda_2 = QComboBox()
        self.combo_banda_2.setStyleSheet(ESTILO_COMBO)

        self.lbl_banda_3 = QLabel()
        self.lbl_banda_3.setStyleSheet("font-size: 11px; margin-top: 5px;")
        self.combo_banda_3 = QComboBox()
        self.combo_banda_3.setStyleSheet(ESTILO_COMBO)

        self.lbl_banda_4 = QLabel()
        self.lbl_banda_4.setStyleSheet("font-size: 11px; margin-top: 5px;")
        self.combo_banda_4 = QComboBox()
        self.combo_banda_4.setStyleSheet(ESTILO_COMBO)

        layout_bandas.addWidget(self.lbl_banda_1)
        layout_bandas.addLayout(box_banda_1)
        layout_bandas.addWidget(self.lbl_banda_2)
        layout_bandas.addWidget(self.combo_banda_2)
        layout_bandas.addWidget(self.lbl_banda_3)
        layout_bandas.addWidget(self.combo_banda_3)
        layout_bandas.addWidget(self.lbl_banda_4)
        layout_bandas.addWidget(self.combo_banda_4)

        layout.addWidget(group_bandas)

        if self.rasters:
            chaves = list(self.rasters.keys())
            self.combo_banda_1.addItems(chaves)
            self.combo_banda_2.addItems(chaves)
            self.combo_banda_3.addItems(chaves)
            self.combo_banda_4.addItems(chaves)
        else:
            erro_msg = "Nenhum Raster Encontrado"
            self.combo_banda_1.addItem(erro_msg)
            self.combo_banda_2.addItem(erro_msg)
            self.combo_banda_3.addItem(erro_msg)
            self.combo_banda_4.addItem(erro_msg)
            self.combo_banda_1.setEnabled(False)
            self.combo_banda_2.setEnabled(False)
            self.combo_banda_3.setEnabled(False)
            self.combo_banda_4.setEnabled(False)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_voltar = QPushButton("‹ Voltar")
        btn_voltar.setCursor(Qt.PointingHandCursor)
        btn_voltar.setStyleSheet("QPushButton { background-color: transparent; color: #718096; font-weight: bold; border: none; } QPushButton:hover { color: #1E507E; text-decoration: underline; }")
        btn_voltar.clicked.connect(lambda: self.stack.setCurrentWidget(self.pagina_indice))

        self.btn_ok = QPushButton("Executar Processo")
        self.btn_ok.setCursor(Qt.PointingHandCursor)
        self.btn_ok.setStyleSheet(f"QPushButton {{ background-color: {COR_PRIMARIA}; color: white; font-weight: bold; padding: 8px 20px; border-radius: 4px; border: none; font-size: 12px; }} QPushButton:hover {{ background-color: {COR_PRIMARIA_HOVER}; }}")
        if not self.rasters: self.btn_ok.setEnabled(False)
        self.btn_ok.clicked.connect(self.accept)

        btn_layout.addWidget(btn_voltar)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_ok)
        layout.addLayout(btn_layout)

        return pagina

    def _ir_para_config(self, mostrar_sensor=False):
        chaves = list(self.rasters.keys())
        for combo in (self.combo_banda_1, self.combo_banda_2, self.combo_banda_3, self.combo_banda_4):
            texto_atual = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            if chaves:
                combo.addItems(chaves)
                combo.setEnabled(True)
            else:
                combo.addItem("Nenhum Raster Encontrado")
                combo.setEnabled(False)
            if texto_atual in chaves:
                combo.setCurrentText(texto_atual)
            combo.blockSignals(False)
        self.btn_ok.setEnabled(bool(self.rasters))
        self.lbl_resumo_indice.setText(f"Categoria: <b>{self.combo_cat.currentText()}</b> — Índice: <b>{self.combo_analise.currentText()}</b>")
        self.group_sensor.setVisible(mostrar_sensor and self.combo_cat.currentText() != "Topografia / Relevo")
        self.stack.setCurrentWidget(self.pagina_config)
        self.atualizar_interface()

    def _criar_pagina_carregando(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: white;")
        layout = QVBoxLayout(pagina)
        layout.addStretch()

        self.lbl_spinner_grande = QLabel("🛰️")
        self.lbl_spinner_grande.setAlignment(Qt.AlignCenter)
        self.lbl_spinner_grande.setStyleSheet("font-size: 72px;")
        layout.addWidget(self.lbl_spinner_grande)

        self.lbl_status_carregando = QLabel("Processando...")
        self.lbl_status_carregando.setAlignment(Qt.AlignCenter)
        self.lbl_status_carregando.setWordWrap(True)
        self.lbl_status_carregando.setStyleSheet(f"color: {COR_PRIMARIA}; font-size: 14px; font-weight: bold; margin: 16px 30px 0 30px;")
        layout.addWidget(self.lbl_status_carregando)

        self.barra_progresso = QProgressBar()
        self.barra_progresso.setRange(0, 0)
        self.barra_progresso.setTextVisible(False)
        self.barra_progresso.setFixedHeight(6)
        self.barra_progresso.setFixedWidth(220)
        self.barra_progresso.setStyleSheet(f"QProgressBar {{ border: none; background-color: #E2E8F0; border-radius: 3px; }} QProgressBar::chunk {{ background-color: {COR_DESTAQUE}; border-radius: 3px; }}")
        layout.addWidget(self.barra_progresso, alignment=Qt.AlignCenter)

        layout.addStretch()
        return pagina

    def _iniciar_carregamento(self, texto):
        self.lbl_status_carregando.setText(texto)
        self._pagina_anterior = self.stack.currentWidget()
        self.stack.setCurrentWidget(self.pagina_carregando)
        self._spinner_index = 0
        self.lbl_spinner_grande.setText(self._spinner_frames[0])
        self.timer_spinner.start(350)

    def _parar_carregamento(self):
        self.timer_spinner.stop()
        if self._pagina_anterior is not None:
            self.stack.setCurrentWidget(self._pagina_anterior)

    def _animar_spinner(self):
        self._spinner_index = (self._spinner_index + 1) % len(self._spinner_frames)
        self.lbl_spinner_grande.setText(self._spinner_frames[self._spinner_index])

    def atualizar_lista_indices(self):
        cat = self.combo_cat.currentText()
        self.combo_analise.blockSignals(True)
        self.combo_analise.clear()
        
        if cat == "Vegetação":
            self.combo_analise.addItems(["NDVI (Saúde da Vegetação)", "EVI (Vegetação Melhorado)", "SAVI (Vegetação Ajustado ao Solo)"])
        elif cat == "Água":
            self.combo_analise.addItems(["NDWI (Corpos D'Água)", "MNDWI (Água Modificado)", "NDMI (Umidade da Vegetação)"])
        elif cat == "Solo e Áreas Expostas":
            self.combo_analise.addItems(["NDSI (Neve / Solo)", "BAI (Solo Exposto / Queimado)"])
        elif cat == "Queimada / Estresse":
            self.combo_analise.addItems(["NBR (Cicatrizes de Queimadas)", "NBR2 (Queimada Severa)"])
        elif cat == "Áreas Urbanas":
            self.combo_analise.addItems(["NDBI (Áreas Construídas)", "UI (Índice Urbano)"])
        elif cat == "Composição Visual (RGB)":
            self.combo_analise.addItems(["True Color (Cor Verdadeira)", "False Color (Infravermelho)"])
        elif cat == "Topografia / Relevo":
            self.combo_analise.addItems(["Hipsometria (Elevação do Terreno)"])
            
        self.combo_analise.blockSignals(False)
        self.atualizar_interface()

    def atualizar_interface(self):
        satelite = self.combo_sat.currentText()
        analise = self.combo_analise.currentText()
        cat = self.combo_cat.currentText()

        bandas = {
            "Sentinel-2 (10m/20m)": {"BLUE": "Banda 2", "GREEN": "Banda 3", "RED": "Banda 4", "NIR": "Banda 8", "SWIR1": "Banda 11", "SWIR2": "Banda 12"},
            "Landsat 8/9 (30m)":    {"BLUE": "Banda 2", "GREEN": "Banda 3", "RED": "Banda 4", "NIR": "Banda 5", "SWIR1": "Banda 6", "SWIR2": "Banda 7"}
        }

        self.lbl_banda_1.hide(); self.combo_banda_1.hide()
        self.lbl_banda_2.hide(); self.combo_banda_2.hide()
        self.lbl_banda_3.hide(); self.combo_banda_3.hide()
        self.lbl_banda_4.hide(); self.combo_banda_4.hide()
        self.btn_baixar_mde.hide()
        self.chaves_bandas_necessarias = []

        if not analise: return

        if cat == "Composição Visual (RGB)":
            self.lbl_banda_1.show(); self.combo_banda_1.show()
            self.lbl_banda_2.show(); self.combo_banda_2.show()
            self.lbl_banda_3.show(); self.combo_banda_3.show()

            if "True Color" in analise:
                chave1, chave2, chave3 = "RED", "GREEN", "BLUE"
            elif "False Color" in analise:
                chave1, chave2, chave3 = "NIR", "RED", "GREEN"
            dica_1, dica_2, dica_3 = bandas[satelite][chave1], bandas[satelite][chave2], bandas[satelite][chave3]
            self.chaves_bandas_necessarias = [chave1, chave2, chave3]

            self.lbl_banda_1.setText(f"<span style='color: #c0392b;'>Canal Red (Requer: {dica_1}):</span>")
            self.lbl_banda_2.setText(f"<span style='color: #27ae60;'>Canal Green (Requer: {dica_2}):</span>")
            self.lbl_banda_3.setText(f"<span style='color: #2980b9;'>Canal Blue (Requer: {dica_3}):</span>")

        elif cat == "Topografia / Relevo":
            self.lbl_banda_1.show(); self.combo_banda_1.show()
            self.btn_baixar_mde.show()
            self.lbl_banda_1.setText("<span style='color: #8e44ad;'>Matriz <b>MDE (Elevação)</b> (Requer: Raster Topográfico):</span>")

        else:
            b1_nome, b2_nome, b3_nome, b4_nome = "", "", "", ""
            cor1, cor2, cor3, cor4 = "#2C3E50", "#2C3E50", "#2C3E50", "#2C3E50"
            dica_1, dica_2, dica_3, dica_4 = "", "", "", ""
            chave1, chave2, chave3 = None, None, None

            if "NDVI" in analise or "SAVI" in analise:
                b1_nome, b2_nome = "NIR (Infravermelho)", "RED (Vermelho)"
                chave1, chave2 = "NIR", "RED"
                dica_1, dica_2 = bandas[satelite]["NIR"], bandas[satelite]["RED"]
                cor1, cor2 = "#8e44ad", "#c0392b"
                self.lbl_banda_1.show(); self.combo_banda_1.show()
                self.lbl_banda_2.show(); self.combo_banda_2.show()
            elif "EVI" in analise:
                b1_nome, b2_nome, b3_nome = "NIR (Infravermelho)", "RED (Vermelho)", "BLUE (Azul)"
                chave1, chave2, chave3 = "NIR", "RED", "BLUE"
                dica_1, dica_2, dica_3 = bandas[satelite]["NIR"], bandas[satelite]["RED"], bandas[satelite]["BLUE"]
                cor1, cor2, cor3 = "#8e44ad", "#c0392b", "#2980b9"
                self.lbl_banda_1.show(); self.combo_banda_1.show()
                self.lbl_banda_2.show(); self.combo_banda_2.show()
                self.lbl_banda_3.show(); self.combo_banda_3.show()
            elif "NDWI" in analise:
                b1_nome, b2_nome = "GREEN (Verde)", "NIR (Infravermelho)"
                chave1, chave2 = "GREEN", "NIR"
                dica_1, dica_2 = bandas[satelite]["GREEN"], bandas[satelite]["NIR"]
                cor1, cor2 = "#27ae60", "#8e44ad"
                self.lbl_banda_1.show(); self.combo_banda_1.show()
                self.lbl_banda_2.show(); self.combo_banda_2.show()
            elif "MNDWI" in analise or "NDSI" in analise:
                b1_nome, b2_nome = "GREEN (Verde)", "SWIR 1 (Infrav. Curto)"
                chave1, chave2 = "GREEN", "SWIR1"
                dica_1, dica_2 = bandas[satelite]["GREEN"], bandas[satelite]["SWIR1"]
                cor1, cor2 = "#27ae60", "#d35400"
                self.lbl_banda_1.show(); self.combo_banda_1.show()
                self.lbl_banda_2.show(); self.combo_banda_2.show()
            elif "NDMI" in analise or ("NBR" in analise and "NBR2" not in analise):
                b1_nome, b2_nome = "NIR (Infravermelho)", "SWIR 1 (Infrav. Curto)"
                chave1, chave2 = "NIR", "SWIR1"
                dica_1, dica_2 = bandas[satelite]["NIR"], bandas[satelite]["SWIR1"]
                cor1, cor2 = "#8e44ad", "#d35400"
                self.lbl_banda_1.show(); self.combo_banda_1.show()
                self.lbl_banda_2.show(); self.combo_banda_2.show()
            elif "BAI" in analise:
                b1_nome, b2_nome, b3_nome = "RED (Vermelho)", "SWIR 1 (Infrav. Curto)", "NIR (Infravermelho)"
                chave1, chave2, chave3 = "RED", "SWIR1", "NIR"
                dica_1, dica_2, dica_3 = bandas[satelite]["RED"], bandas[satelite]["SWIR1"], bandas[satelite]["NIR"]
                cor1, cor2, cor3 = "#c0392b", "#d35400", "#8e44ad"
                self.lbl_banda_1.show(); self.combo_banda_1.show()
                self.lbl_banda_2.show(); self.combo_banda_2.show()
                self.lbl_banda_3.show(); self.combo_banda_3.show()
            elif "NBR2" in analise:
                b1_nome, b2_nome = "SWIR 1 (Infrav. Curto 1)", "SWIR 2 (Infrav. Curto 2)"
                chave1, chave2 = "SWIR1", "SWIR2"
                dica_1, dica_2 = bandas[satelite]["SWIR1"], bandas[satelite]["SWIR2"]
                cor1, cor2 = "#d35400", "#e67e22"
                self.lbl_banda_1.show(); self.combo_banda_1.show()
                self.lbl_banda_2.show(); self.combo_banda_2.show()
            elif "NDBI" in analise or "UI" in analise:
                b1_nome, b2_nome = "SWIR 1 (Infrav. Curto)", "NIR (Infravermelho)"
                chave1, chave2 = "SWIR1", "NIR"
                dica_1, dica_2 = bandas[satelite]["SWIR1"], bandas[satelite]["NIR"]
                cor1, cor2 = "#d35400", "#8e44ad"
                self.lbl_banda_1.show(); self.combo_banda_1.show()
                self.lbl_banda_2.show(); self.combo_banda_2.show()

            self.chaves_bandas_necessarias = [c for c in (chave1, chave2, chave3) if c]

            self.lbl_banda_1.setText(f"<span style='color: {cor1};'>Matriz <b>{b1_nome}</b> (Requer: {dica_1}):</span>")
            if b2_nome: self.lbl_banda_2.setText(f"<span style='color: {cor2};'>Matriz <b>{b2_nome}</b> (Requer: {dica_2}):</span>")
            if b3_nome: self.lbl_banda_3.setText(f"<span style='color: {cor3};'>Matriz <b>{b3_nome}</b> (Requer: {dica_3}):</span>")
            if b4_nome: self.lbl_banda_4.setText(f"<span style='color: {cor4};'>Matriz <b>{b4_nome}</b> (Requer: {dica_4}):</span>")

        # Pré-seleciona automaticamente bandas já baixadas (nome "CHAVE_<cena>")
        combos_visiveis = [self.combo_banda_1, self.combo_banda_2, self.combo_banda_3, self.combo_banda_4]
        for i, chave in enumerate(self.chaves_bandas_necessarias):
            if i >= len(combos_visiveis) or not combos_visiveis[i].isVisible():
                continue
            combo = combos_visiveis[i]
            candidato = next((nome for nome in self.rasters if nome.startswith(f"{chave}_")), None)
            if candidato and candidato in [combo.itemText(j) for j in range(combo.count())]:
                combo.setCurrentText(candidato)

        # ATUALIZAÇÃO DO PAINEL DE INFORMAÇÕES
        dicionario_dicas = {
            "NDVI": ("Índice de Vegetação da Diferença Normalizada", "Mede a saúde e densidade da vegetação verde. Valores altos indicam mata densa e saudável, enquanto valores próximos a zero ou negativos indicam solo exposto, água ou nuvens."),
            "EVI": ("Índice de Vegetação Melhorado", "Similar ao NDVI, mas corrige distorções atmosféricas e a influência do solo. É excelente para áreas de floresta muito densa, onde o NDVI costuma saturar."),
            "SAVI": ("Índice de Vegetação Ajustado ao Solo", "Minimiza a influência do brilho do solo de fundo. É o índice ideal para regiões áridas, cerrado ralo ou áreas com vegetação esparsa."),
            "NDWI": ("Índice da Diferença Normalizada da Água", "Realça a presença de corpos d'água (lagos, rios) e saturação de umidade na vegetação, suprimindo as cores do solo e das plantas."),
            "MNDWI": ("NDWI Modificado", "Uma versão superior ao NDWI para separar água líquida de áreas construídas, reduzindo bastante os falsos positivos dentro de centros urbanos."),
            "NDMI": ("Índice de Umidade da Diferença Normalizada", "Mede o estresse hídrico (nível de água nas folhas do dossel). Muito utilizado para monitorar secas agrícolas e risco de incêndio."),
            "NDSI": ("Índice de Neve da Diferença Normalizada", "Desenvolvido especificamente para diferenciar a cobertura de neve e gelo de nuvens espessas."),
            "BAI": ("Índice de Área Queimada", "Focado em identificar o resíduo de carvão e cinzas no solo logo após a passagem de um incêndio."),
            "NBR": ("Razão Normalizada de Queimada", "O padrão ouro para mapear cicatrizes de incêndios florestais. Áreas queimadas aparecem com valores bem baixos/negativos."),
            "NBR2": ("Razão Normalizada de Queimada 2", "Variação do NBR que foca na reflectância de materiais secos (celulose, palha morta) para avaliar a severidade do fogo no solo."),
            "NDBI": ("Índice de Área Edificada", "Realça áreas urbanas, asfalto, concreto e telhados, ajudando a mapear o crescimento das cidades em contraste com a natureza."),
            "UI": ("Índice Urbano", "Alternativa ao NDBI para separar de forma rápida e clara zonas altamente urbanizadas de áreas naturais ou agrícolas."),
            "True Color": ("Composição RGB (Cor Verdadeira)", "Combina as bandas Vermelha, Verde e Azul para gerar uma imagem exatamente como o olho humano veria do espaço."),
            "False Color": ("Composição Falsa Cor (Infravermelho)", "Injeta o canal infravermelho, fazendo com que a vegetação saudável 'brilhe' em tons de vermelho intenso. Ótimo para ver o vigor das plantas."),
            "Hipsometria": ("Mapa de Elevação Topográfica", "Aplica uma rampa de cores sobre o Modelo Digital de Elevação (MDE), variando do nível do mar (verde) até os topos de morro (vermelho/branco).")
        }

        chave_encontrada = ""
        for key in dicionario_dicas.keys():
            if key in analise:
                chave_encontrada = key
                break
                
        if chave_encontrada:
            titulo, texto = dicionario_dicas[chave_encontrada]
            self.lbl_info_titulo.setText(f"ℹ️ <b>{chave_encontrada}</b> - {titulo}")
            self.lbl_info_texto.setText(f"{texto}")
        else:
            self.lbl_info_titulo.setText(f"ℹ️ <b>Informação do Processo</b>")
            self.lbl_info_texto.setText("Selecione um índice no menu acima para ver o detalhamento técnico e para que serve.")

    def _reportar_erro(self, mensagem):
        parent_chat = self.parent()
        if parent_chat is not None and hasattr(parent_chat, "chat_history"):
            parent_chat.chat_history.append(f"<span style='color: #e74c3c;'>❌ {mensagem}</span>")
            parent_chat.rolar_chat()
        else:
            QMessageBox.critical(self, "Erro", mensagem)

    def _reportar_sucesso(self, mensagem):
        parent_chat = self.parent()
        if parent_chat is not None and hasattr(parent_chat, "chat_history"):
            parent_chat.chat_history.append(f"<span style='color: {COR_DESTAQUE};'>✅ {mensagem}</span>")
            parent_chat.rolar_chat()
        else:
            QMessageBox.information(self, "H-Geo", mensagem)

    def baixar_mde_da_extensao(self):
        try:
            bbox = obter_bbox_wgs84_do_canvas()
        except Exception as e:
            self._reportar_erro(f"Falha ao obter a extensão do mapa: {str(e)}")
            return

        self._iniciar_carregamento("Baixando MDE (Copernicus DEM 30m)...")

        self._worker = TarefaGeoWorker(_baixar_mde_automatico_arquivo, bbox, nome_camada="MDE_Auto")
        self._worker.concluido.connect(self._download_mde_concluido)
        self._worker.falhou.connect(self._download_mde_falhou)
        self._worker.start()

    def _download_mde_concluido(self, resultado):
        caminho, nome = resultado
        self._parar_carregamento()

        camada_mde = QgsRasterLayer(caminho, nome)
        if not camada_mde.isValid():
            self._reportar_erro("O MDE baixado não é válido.")
            return

        QgsProject.instance().addMapLayer(camada_mde)
        self.rasters[camada_mde.name()] = camada_mde

        chaves = list(self.rasters.keys())
        for combo in (self.combo_banda_1, self.combo_banda_2, self.combo_banda_3, self.combo_banda_4):
            texto_atual = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(chaves)
            combo.setEnabled(True)
            if texto_atual in chaves:
                combo.setCurrentText(texto_atual)
            combo.blockSignals(False)
        self.combo_banda_1.setCurrentText(camada_mde.name())
        self.btn_ok.setEnabled(True)

        self._reportar_sucesso(f"MDE baixado e carregado: {camada_mde.name()}")

    def _download_mde_falhou(self, mensagem):
        self._parar_carregamento()
        self._reportar_erro(f"Falha ao baixar o MDE automaticamente: {mensagem}")

    def get_data(self):
        analise = self.combo_analise.currentText()
        cat = self.combo_cat.currentText()
        b1 = self.combo_banda_1.currentText()
        b2 = self.combo_banda_2.currentText()
        b3 = self.combo_banda_3.currentText() if self.combo_banda_3.isVisible() else None
        b4 = self.combo_banda_4.currentText() if self.combo_banda_4.isVisible() else None
        return cat, analise, b1, b2, b3, b4

# ==========================================
# MÓDULOS DE HIDROLOGIA (GRASS)
# ==========================================
class PointTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, callback):
        super().__init__(canvas)
        self.callback = callback
        self.setCursor(Qt.CrossCursor)

    def canvasReleaseEvent(self, event):
        point = self.toMapCoordinates(event.pos())
        self.callback(point)

def _tarefa_delimitar_bacia(task, dir_layer, coords_final):
    """Roda no worker thread de uma QgsTask: delimita a bacia (r.water.outlet)
    e converte o resultado raster em polígono (gdal:polygonize)."""
    alg_outlet = "grass7:r.water.outlet" if QgsApplication.processingRegistry().algorithmById("grass7:r.water.outlet") else "grass:r.water.outlet"
    params_outlet = {
        'input': dir_layer,
        'coordinates': coords_final,
        'output': 'TEMPORARY_OUTPUT'
    }
    res_outlet = processing.run(alg_outlet, params_outlet)

    params_poly = {
        'INPUT': res_outlet['output'],
        'BAND': 1,
        'FIELD': 'DN',
        'OUTPUT': 'TEMPORARY_OUTPUT'
    }
    res_poly = processing.run("gdal:polygonize", params_poly)
    return res_poly['OUTPUT']

class ExtratorExutorioDialog(QDialog):
    def __init__(self, parent=None):
        super(ExtratorExutorioDialog, self).__init__(parent)
        self.setWindowTitle("Passo 3: Delimitar Bacia pelo Exutório")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.resize(440, 480)

        self.canvas = qgis.utils.iface.mapCanvas()
        self.ferramenta_anterior = None
        self.point_tool = None
        self._task = None
        self._contador_bacias = 0
        self._nomes_bacias_geradas = []

        self._spinner_frames = ["🎯", "🌊", "💧", "🌊"]
        self._spinner_index = 0
        self.timer_spinner = QTimer(self)
        self.timer_spinner.timeout.connect(self._animar_spinner)

        layout_raiz = QVBoxLayout(self)
        layout_raiz.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedWidget()
        layout_raiz.addWidget(self.stack)

        self.pagina_principal = self._criar_pagina_principal()
        self.pagina_carregando = self._criar_pagina_carregando()
        self.stack.addWidget(self.pagina_principal)
        self.stack.addWidget(self.pagina_carregando)
        self.stack.setCurrentWidget(self.pagina_principal)

    def _criar_pagina_principal(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: #F8FAFC; font-family: 'Segoe UI', Arial;")
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(20, 20, 20, 20)

        painel_educativo = QWidget()
        painel_educativo.setStyleSheet("background-color: #FFF7ED; border: 1px solid #FED7AA; border-radius: 6px; padding: 10px;")
        lay_edu = QVBoxLayout(painel_educativo)
        lay_edu.setContentsMargins(10, 10, 10, 10)
        titulo_edu = QLabel("📚 <b>O que é o Ponto de Exutório?</b>")
        titulo_edu.setStyleSheet("color: #C2410C; font-size: 12px;")
        texto_edu = QLabel(
            "É o <b>ponto de saída</b> da água de uma bacia — normalmente onde um "
            "rio cruza uma seção de interesse (ex: um barramento, uma foz, o limite "
            "de uma propriedade). O algoritmo <b>r.water.outlet</b> percorre a "
            "Direção de Fluxo 'rio acima' a partir desse ponto e delimita toda a "
            "área de contribuição — a <b>bacia (ou microbacia)</b> daquele exutório."
        )
        texto_edu.setWordWrap(True)
        texto_edu.setStyleSheet("color: #4A5568; font-size: 11px;")
        lay_edu.addWidget(titulo_edu)
        lay_edu.addWidget(texto_edu)
        layout.addWidget(painel_educativo)

        group2 = QGroupBox("Parâmetros do Ponto de Saída")
        group2.setStyleSheet(ESTILO_GROUPBOX)
        g2_layout = QGridLayout(group2)
        g2_layout.setContentsMargins(15, 20, 15, 15)

        lbl_dir = QLabel("Raster de Direção de Fluxo:")
        self.combo_dir = QComboBox()
        self.combo_dir.setStyleSheet(ESTILO_COMBO)
        self.atualizar_lista_direcao()

        lbl_stream = QLabel("Raster de Rios/Drenagem:")
        self.combo_stream = QComboBox()
        self.combo_stream.setStyleSheet(ESTILO_COMBO)
        self.atualizar_lista_stream()

        lbl_coord = QLabel("Coordenada (X, Y):")
        box_coord = QHBoxLayout()
        self.input_coord = QLineEdit("")
        self.input_coord.setPlaceholderText("Clique na mira ➡")
        self.input_coord.setStyleSheet(ESTILO_INPUT)

        self.btn_capturar = QPushButton("🎯 Mapear")
        self.btn_capturar.setCursor(Qt.PointingHandCursor)
        self.btn_capturar.setStyleSheet("QPushButton { background-color: #e67e22; color: white; font-weight: bold; border-radius: 4px; padding: 6px; font-size: 13px; } QPushButton:hover { background-color: #d35400; }")
        self.btn_capturar.clicked.connect(self.ativar_mira)

        box_coord.addWidget(self.input_coord)
        box_coord.addWidget(self.btn_capturar)

        g2_layout.addWidget(lbl_dir, 0, 0)
        g2_layout.addWidget(self.combo_dir, 0, 1)
        g2_layout.addWidget(lbl_stream, 1, 0)
        g2_layout.addWidget(self.combo_stream, 1, 1)
        g2_layout.addWidget(lbl_coord, 2, 0)
        g2_layout.addLayout(box_coord, 2, 1)

        lbl_ajuda_snap = QLabel("O ponto clicado é ajustado automaticamente para a célula de rio mais próxima (até ~12 pixels de distância).")
        lbl_ajuda_snap.setWordWrap(True)
        lbl_ajuda_snap.setStyleSheet("color: #718096; font-size: 10px; margin-top: 4px;")
        g2_layout.addWidget(lbl_ajuda_snap, 3, 0, 1, 2)

        layout.addWidget(group2)

        self.lbl_contador = QLabel("Nenhuma bacia gerada ainda nesta sessão.")
        self.lbl_contador.setWordWrap(True)
        self.lbl_contador.setStyleSheet(f"background-color: #F0FDF4; border: 1px solid #BBF7D0; border-radius: 6px; padding: 8px 10px; color: #15803D; font-size: 11px; margin-top: 10px;")
        layout.addWidget(self.lbl_contador)

        lbl_dica_loop = QLabel("Você pode gerar quantas bacias quiser: clique em '🎯 Mapear' num novo ponto e depois em 'Gerar Polígono da Bacia' de novo — a tela fica aberta.")
        lbl_dica_loop.setWordWrap(True)
        lbl_dica_loop.setStyleSheet("color: #94A3B8; font-size: 10px; margin-top: 4px;")
        layout.addWidget(lbl_dica_loop)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        self.btn_fechar = QPushButton("Fechar")
        self.btn_fechar.setCursor(Qt.PointingHandCursor)
        self.btn_fechar.setStyleSheet("QPushButton { background-color: transparent; color: #718096; font-weight: bold; border: none; } QPushButton:hover { color: #E53E3E; text-decoration: underline; }")
        self.btn_fechar.clicked.connect(self.reject)

        self.btn_passo2 = QPushButton("🌊 Gerar Polígono da Bacia")
        self.btn_passo2.setCursor(Qt.PointingHandCursor)
        self.btn_passo2.setStyleSheet(f"QPushButton {{ background-color: {COR_DESTAQUE}; color: white; font-weight: bold; padding: 8px 15px; border-radius: 4px; border: none; font-size: 13px; }} QPushButton:hover {{ background-color: #58963E; }}")
        self.btn_passo2.clicked.connect(self.executar_passo2)

        btn_layout.addWidget(self.btn_fechar)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_passo2)
        layout.addLayout(btn_layout)

        return pagina

    def _criar_pagina_carregando(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: white;")
        layout = QVBoxLayout(pagina)
        layout.addStretch()

        self.lbl_spinner_grande = QLabel("🎯")
        self.lbl_spinner_grande.setAlignment(Qt.AlignCenter)
        self.lbl_spinner_grande.setStyleSheet("font-size: 72px;")
        layout.addWidget(self.lbl_spinner_grande)

        self.lbl_status_carregando = QLabel("Processando...")
        self.lbl_status_carregando.setAlignment(Qt.AlignCenter)
        self.lbl_status_carregando.setWordWrap(True)
        self.lbl_status_carregando.setStyleSheet(f"color: {COR_PRIMARIA}; font-size: 14px; font-weight: bold; margin: 16px 30px 0 30px;")
        layout.addWidget(self.lbl_status_carregando)

        self.barra_progresso = QProgressBar()
        self.barra_progresso.setRange(0, 0)
        self.barra_progresso.setTextVisible(False)
        self.barra_progresso.setFixedHeight(6)
        self.barra_progresso.setFixedWidth(220)
        self.barra_progresso.setStyleSheet(f"QProgressBar {{ border: none; background-color: #E2E8F0; border-radius: 3px; }} QProgressBar::chunk {{ background-color: {COR_DESTAQUE}; border-radius: 3px; }}")
        layout.addWidget(self.barra_progresso, alignment=Qt.AlignCenter)

        layout.addStretch()
        return pagina

    def _iniciar_carregamento(self, texto):
        self.lbl_status_carregando.setText(texto)
        self.stack.setCurrentWidget(self.pagina_carregando)
        self._spinner_index = 0
        self.lbl_spinner_grande.setText(self._spinner_frames[0])
        self.timer_spinner.start(350)

    def _parar_carregamento(self):
        self.timer_spinner.stop()
        self.stack.setCurrentWidget(self.pagina_principal)

    def _animar_spinner(self):
        self._spinner_index = (self._spinner_index + 1) % len(self._spinner_frames)
        self.lbl_spinner_grande.setText(self._spinner_frames[self._spinner_index])

    def atualizar_lista_direcao(self):
        self.combo_dir.clear()
        camadas_atuais = {layer.name(): layer for layer in QgsProject.instance().mapLayers().values() if isinstance(layer, QgsRasterLayer)}
        direcoes = [nome for nome in camadas_atuais.keys() if "Direcao_Fluxo" in nome or "drainage" in nome.lower()]
        if direcoes:
            self.combo_dir.addItems(direcoes)
        else:
            self.combo_dir.addItems(list(camadas_atuais.keys()))

    def atualizar_lista_stream(self):
        self.combo_stream.clear()
        camadas_atuais = {layer.name(): layer for layer in QgsProject.instance().mapLayers().values() if isinstance(layer, QgsRasterLayer)}
        streams = [nome for nome in camadas_atuais.keys() if "Rios_Drenagem" in nome or "stream" in nome.lower()]
        if streams:
            self.combo_stream.addItems(streams)
        else:
            self.combo_stream.addItem("(nenhum — sem ajuste automático)")
            self.combo_stream.setEnabled(False)

    def ativar_mira(self):
        self.hide()
        self.ferramenta_anterior = self.canvas.mapTool()
        self.point_tool = QgsMapToolEmitPoint(self.canvas)
        self.point_tool.canvasClicked.connect(self.coordenada_capturada)
        self.canvas.setMapTool(self.point_tool)

    def coordenada_capturada(self, point, button):
        self.point_tool.canvasClicked.disconnect()
        self.canvas.setMapTool(self.ferramenta_anterior)
        self.input_coord.setText(f"{point.x():.5f},{point.y():.5f}")
        self.show()

    def _transformar_para_crs(self, ponto, crs_destino):
        """O ponto capturado pelo clique no mapa vem no CRS do canvas/projeto.
        Como o MDE (e, portanto, o raster de rios) pode estar em outro CRS
        (ex: SIRGAS 2000 UTM), é preciso reprojetar antes de comparar com a
        grade do raster — senão a busca por célula de rio nunca bate com nada."""
        crs_canvas = self.canvas.mapSettings().destinationCrs()
        if crs_canvas == crs_destino:
            return ponto
        transformador = QgsCoordinateTransform(crs_canvas, crs_destino, QgsProject.instance())
        return transformador.transform(ponto)

    def _snap_para_stream(self, ponto, stream_layer, raio_celulas=12):
        """Procura, num raio de N células ao redor do ponto clicado, a célula
        de rio (valor não-nulo no raster de drenagem) mais próxima e devolve
        suas coordenadas (x, y). Devolve None se nada for encontrado no raio."""
        provider = stream_layer.dataProvider()
        px_x = stream_layer.rasterUnitsPerPixelX()
        px_y = stream_layer.rasterUnitsPerPixelY()
        extensao_raster = stream_layer.extent()

        melhor_ponto = None
        melhor_dist2 = None

        for dy in range(-raio_celulas, raio_celulas + 1):
            for dx in range(-raio_celulas, raio_celulas + 1):
                x = ponto.x() + dx * px_x
                y = ponto.y() + dy * px_y
                candidato = QgsPointXY(x, y)
                if not extensao_raster.contains(candidato):
                    continue
                valor, ok = provider.sample(candidato, 1)
                if not ok or valor is None:
                    continue
                dist2 = dx * dx + dy * dy
                if melhor_dist2 is None or dist2 < melhor_dist2:
                    melhor_dist2 = dist2
                    melhor_ponto = (x, y)

        return melhor_ponto

    def executar_passo2(self):
        dir_nome = self.combo_dir.currentText()
        stream_nome = self.combo_stream.currentText()
        coords_texto = self.input_coord.text().strip()

        if not dir_nome:
            self._reportar_erro("Selecione o raster de Direção de Fluxo.")
            return
        if not coords_texto:
            self._reportar_erro("Clique em '🎯 Mapear' e marque o ponto de exutório no mapa antes de continuar.")
            return

        try:
            camadas_atuais = {layer.name(): layer for layer in QgsProject.instance().mapLayers().values()}
            if dir_nome not in camadas_atuais:
                raise Exception(f"A camada '{dir_nome}' não foi encontrada no projeto (pode ter sido removida).")
            dir_layer = camadas_atuais[dir_nome]

            x_str, y_str = coords_texto.split(',')
            ponto_clicado_canvas = QgsPointXY(float(x_str), float(y_str))

            ponto_clicado_dir = self._transformar_para_crs(ponto_clicado_canvas, dir_layer.crs())
            coords_final = f"{ponto_clicado_dir.x():.5f},{ponto_clicado_dir.y():.5f} [{dir_layer.crs().authid()}]"

            if stream_nome and stream_nome in camadas_atuais:
                stream_layer = camadas_atuais[stream_nome]
                ponto_para_snap = self._transformar_para_crs(ponto_clicado_canvas, stream_layer.crs())
                ponto_ajustado = self._snap_para_stream(ponto_para_snap, stream_layer)
                if ponto_ajustado is None:
                    raise Exception(
                        "Nenhuma célula de rio encontrada perto do ponto clicado. "
                        "Clique mais próximo de um trecho da camada 'Rios_Drenagem' "
                        "ou volte ao Passo 2 e reduza o limiar de drenagem para gerar mais rios."
                    )
                coords_final = f"{ponto_ajustado[0]:.5f},{ponto_ajustado[1]:.5f} [{stream_layer.crs().authid()}]"
        except Exception as e:
            self._reportar_erro(f"Falha ao preparar o ponto de exutório: {str(e)}")
            return

        self._iniciar_carregamento("Delimitando bacia (r.water.outlet)...\nCalculando área de contribuição")

        self._task = QgsTask.fromFunction(
            "Delimitar bacia",
            lambda task, layer=dir_layer, coords=coords_final: _tarefa_delimitar_bacia(task, layer, coords),
            on_finished=self._delimitacao_concluida
        )
        QgsApplication.taskManager().addTask(self._task)

    def _delimitacao_concluida(self, exception, caminho_vetor=None):
        self._parar_carregamento()

        if exception is not None:
            self._reportar_erro(f"Falha ao delimitar a bacia: {str(exception)}")
            return

        try:
            self._contador_bacias += 1
            nome_bacia = f"Bacia_Exutorio_{self._contador_bacias}"
            bacia_vector = QgsVectorLayer(caminho_vetor, nome_bacia, "ogr")

            if not bacia_vector.isValid():
                self._contador_bacias -= 1
                self._reportar_erro("Falha ao gerar o polígono da bacia (vetor inválido).")
            elif bacia_vector.featureCount() == 0:
                self._contador_bacias -= 1
                self._reportar_erro(
                    "O processamento rodou, mas não gerou nenhuma feição (bacia vazia). "
                    "O ponto de exutório provavelmente não caiu sobre a rede de drenagem — "
                    "tente clicar mais em cima do rio, ou reduza o limiar no Passo 2."
                )
            else:
                QgsProject.instance().addMapLayer(bacia_vector)
                self._nomes_bacias_geradas.append(nome_bacia)
                self._reportar_sucesso(f"'{nome_bacia}' delimitada com sucesso ({bacia_vector.featureCount()} feição(ões)).")
                self.input_coord.clear()
                self.lbl_contador.setText(
                    f"✅ {len(self._nomes_bacias_geradas)} bacia(s) gerada(s): " + ", ".join(self._nomes_bacias_geradas)
                )
        except Exception as e:
            self._reportar_erro(f"Falha ao processar o resultado da bacia: {str(e)}")

    def _reportar_erro(self, mensagem):
        parent_chat = self.parent()
        if parent_chat is not None and hasattr(parent_chat, "chat_history"):
            parent_chat.chat_history.append(f"<span style='color: #e74c3c;'>❌ {mensagem}</span>")
            parent_chat.rolar_chat()
        else:
            QMessageBox.critical(self, "Erro", mensagem)

    def _reportar_sucesso(self, mensagem):
        parent_chat = self.parent()
        if parent_chat is not None and hasattr(parent_chat, "chat_history"):
            parent_chat.chat_history.append(f"<span style='color: {COR_DESTAQUE};'>✅ {mensagem}</span>")
            parent_chat.rolar_chat()
        else:
            QMessageBox.information(self, "H-Geo", mensagem)

class SelecaoMDEDialog(QDialog):
    ITENS_EPSG = [
        "Detectar automaticamente (21S ou 22S)",
        "EPSG:31981 - SIRGAS 2000 / UTM 21S",
        "EPSG:31982 - SIRGAS 2000 / UTM 22S",
        "Manter CRS original (não reprojetar)"
    ]

    def __init__(self, parent=None):
        super(SelecaoMDEDialog, self).__init__(parent)
        self.setWindowTitle("Passo 1: Obter MDE")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.resize(440, 520)

        self._worker = None
        self._mde_baixado = None
        self._pagina_anterior = None
        # Exclui rasters gerados pela própria ferramenta (direção de fluxo, rios) —
        # nunca são um MDE válido de entrada, só confundiriam a seleção.
        self.rasters = {
            layer.name(): layer for layer in QgsProject.instance().mapLayers().values()
            if isinstance(layer, QgsRasterLayer)
            and "Direcao_Fluxo" not in layer.name()
            and "Rios_Drenagem" not in layer.name()
        }

        self._spinner_frames = ["🛰️", "📡", "🌐", "📡"]
        self._spinner_index = 0
        self.timer_spinner = QTimer(self)
        self.timer_spinner.timeout.connect(self._animar_spinner)

        layout_raiz = QVBoxLayout(self)
        layout_raiz.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedWidget()
        layout_raiz.addWidget(self.stack)

        self.pagina_pergunta = self._criar_pagina_pergunta()
        self.pagina_selecao = self._criar_pagina_selecao()
        self.pagina_download = self._criar_pagina_download()
        self.pagina_carregando = self._criar_pagina_carregando()

        self.stack.addWidget(self.pagina_pergunta)
        self.stack.addWidget(self.pagina_selecao)
        self.stack.addWidget(self.pagina_download)
        self.stack.addWidget(self.pagina_carregando)

        self.stack.setCurrentWidget(self.pagina_pergunta)

    # ------------------------------------------------------------------
    # PÁGINA 1: pergunta inicial
    # ------------------------------------------------------------------
    def _criar_pagina_pergunta(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: #F8FAFC; font-family: 'Segoe UI', Arial;")
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(30, 30, 30, 20)
        layout.addStretch()

        lbl_icone = QLabel("🗺️")
        lbl_icone.setAlignment(Qt.AlignCenter)
        lbl_icone.setStyleSheet("font-size: 52px;")
        layout.addWidget(lbl_icone)

        lbl_pergunta = QLabel("Você já tem um MDE carregado<br>neste projeto do QGIS?")
        lbl_pergunta.setAlignment(Qt.AlignCenter)
        lbl_pergunta.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {COR_PRIMARIA}; margin: 16px 0 4px 0;")
        layout.addWidget(lbl_pergunta)

        lbl_sub = QLabel("MDE = Modelo Digital de Elevação (raster de altitude do terreno)")
        lbl_sub.setAlignment(Qt.AlignCenter)
        lbl_sub.setWordWrap(True)
        lbl_sub.setStyleSheet("color: #718096; font-size: 11px; margin-bottom: 24px;")
        layout.addWidget(lbl_sub)

        self.btn_tenho_mde = QPushButton("✅  Sim, já tenho um MDE")
        self.btn_tenho_mde.setCursor(Qt.PointingHandCursor)
        self.btn_tenho_mde.setStyleSheet(f"QPushButton {{ background-color: {COR_PRIMARIA}; color: white; font-weight: bold; padding: 14px; border-radius: 8px; border: none; font-size: 13px; }} QPushButton:hover {{ background-color: {COR_PRIMARIA_HOVER}; }} QPushButton:disabled {{ background-color: #CBD5E0; }}")
        self.btn_tenho_mde.clicked.connect(self._ir_para_selecao)
        if not self.rasters:
            self.btn_tenho_mde.setEnabled(False)
            self.btn_tenho_mde.setToolTip("Nenhum raster carregado neste projeto")
        layout.addWidget(self.btn_tenho_mde)

        btn_nao_tenho = QPushButton("🌎  Não, quero baixar automaticamente")
        btn_nao_tenho.setCursor(Qt.PointingHandCursor)
        btn_nao_tenho.setStyleSheet("QPushButton { background-color: #0D9488; color: white; font-weight: bold; padding: 14px; border-radius: 8px; border: none; font-size: 13px; margin-top: 10px; } QPushButton:hover { background-color: #0F766E; }")
        btn_nao_tenho.clicked.connect(self._ir_para_download)
        layout.addWidget(btn_nao_tenho)

        layout.addStretch()

        btn_cancelar = QPushButton("Cancelar")
        btn_cancelar.setCursor(Qt.PointingHandCursor)
        btn_cancelar.setStyleSheet("QPushButton { background-color: transparent; color: #718096; font-weight: bold; border: none; padding: 8px; } QPushButton:hover { color: #E53E3E; text-decoration: underline; }")
        btn_cancelar.clicked.connect(self.reject)
        layout.addWidget(btn_cancelar, alignment=Qt.AlignCenter)

        return pagina

    def _criar_combo_epsg(self):
        combo = QComboBox()
        combo.setStyleSheet(ESTILO_COMBO)
        combo.addItems(self.ITENS_EPSG)
        combo.setToolTip("O MDE será reprojetado para esta projeção antes de seguir para o Passo 2 (necessário para o r.watershed gerar a malha corretamente)")
        return combo

    def _parse_epsg_combo(self, combo, centro_lon=None):
        texto = combo.currentText()
        if texto.startswith("EPSG:"):
            codigo = texto.split(':')[1].split(' ')[0]
            return f"EPSG:{codigo}"
        if texto.startswith("Detectar"):
            return epsg_sirgas_utm_por_longitude(centro_lon if centro_lon is not None else -55.0)
        return None

    # ------------------------------------------------------------------
    # PÁGINA 2: já tenho um MDE (seleção)
    # ------------------------------------------------------------------
    def _criar_pagina_selecao(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: #F8FAFC; font-family: 'Segoe UI', Arial;")
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(20, 20, 20, 20)

        group_mde = QGroupBox("MDE já carregado no projeto")
        group_mde.setStyleSheet(ESTILO_GROUPBOX)
        g_layout = QGridLayout(group_mde)
        g_layout.setContentsMargins(15, 20, 15, 15)

        lbl_mde = QLabel("Selecione o MDE:")
        self.combo_mde = QComboBox()
        self.combo_mde.setStyleSheet(ESTILO_COMBO)
        self.combo_mde.currentIndexChanged.connect(self.atualizar_detalhes)

        lbl_epsg = QLabel("Projeção de saída:")
        self.combo_epsg_selecao = self._criar_combo_epsg()

        g_layout.addWidget(lbl_mde, 0, 0)
        g_layout.addWidget(self.combo_mde, 0, 1)
        g_layout.addWidget(lbl_epsg, 1, 0)
        g_layout.addWidget(self.combo_epsg_selecao, 1, 1)
        layout.addWidget(group_mde)

        layout.addWidget(criar_painel_educativo_mde())

        self.painel_detalhes = criar_painel_detalhes_mde()
        layout.addWidget(self.painel_detalhes)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_voltar = QPushButton("‹ Voltar")
        btn_voltar.setCursor(Qt.PointingHandCursor)
        btn_voltar.setStyleSheet("QPushButton { background-color: transparent; color: #718096; font-weight: bold; border: none; } QPushButton:hover { color: #1E507E; text-decoration: underline; }")
        btn_voltar.clicked.connect(lambda: self.stack.setCurrentWidget(self.pagina_pergunta))

        self.btn_proximo = QPushButton("Avançar para Malha Hídrica ➜")
        self.btn_proximo.setCursor(Qt.PointingHandCursor)
        self.btn_proximo.setStyleSheet(f"QPushButton {{ background-color: {COR_PRIMARIA}; color: white; font-weight: bold; padding: 8px 15px; border-radius: 4px; border: none; font-size: 12px; }} QPushButton:hover {{ background-color: {COR_PRIMARIA_HOVER}; }}")
        self.btn_proximo.clicked.connect(self.avancar)

        btn_layout.addWidget(btn_voltar)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_proximo)
        layout.addLayout(btn_layout)

        return pagina

    def _ir_para_selecao(self):
        self.combo_mde.blockSignals(True)
        self.combo_mde.clear()
        if self.rasters:
            self.combo_mde.addItems(self.rasters.keys())
            self.combo_mde.setEnabled(True)
        else:
            self.combo_mde.addItem("Nenhum MDE carregado")
            self.combo_mde.setEnabled(False)
        self.combo_mde.blockSignals(False)
        self.stack.setCurrentWidget(self.pagina_selecao)
        self.atualizar_detalhes()

    def atualizar_detalhes(self):
        nome = self.combo_mde.currentText()
        if not nome or nome not in self.rasters:
            self.painel_detalhes.lbl_conteudo.setText("Selecione um MDE para ver as características.")
            self.btn_proximo.setEnabled(False)
            return
        try:
            self.painel_detalhes.lbl_conteudo.setText(formatar_detalhes_mde(self.rasters[nome]))
            self.btn_proximo.setEnabled(True)
        except Exception as e:
            self.painel_detalhes.lbl_conteudo.setText(f"Não foi possível calcular as características deste MDE: {str(e)}")
            self.btn_proximo.setEnabled(True)

    def avancar(self):
        nome = self.combo_mde.currentText()
        if not nome or nome not in self.rasters:
            return
        mde_layer = self.rasters[nome]

        try:
            centro_lon, _ = centro_wgs84_da_camada(mde_layer)
        except Exception:
            centro_lon = None
        epsg_destino = self._parse_epsg_combo(self.combo_epsg_selecao, centro_lon=centro_lon)

        if not epsg_destino or mde_layer.crs().authid().upper() == epsg_destino.upper():
            self._avancar_com_camada(mde_layer)
            return

        self._iniciar_carregamento(f"Reprojetando MDE para {epsg_destino}...")

        nome_saida = f"{mde_layer.name()}_{epsg_destino.replace(':', '')}"
        self._worker = TarefaGeoWorker(reprojetar_raster_arquivo, mde_layer.source(), epsg_destino, nome_saida)
        self._worker.concluido.connect(self._reprojecao_concluida)
        self._worker.falhou.connect(self._reprojecao_falhou)
        self._worker.start()

    def _reprojecao_concluida(self, caminho):
        self._parar_carregamento()
        nome_final = os.path.splitext(os.path.basename(caminho))[0]
        camada_reprojetada = QgsRasterLayer(caminho, nome_final)
        if not camada_reprojetada.isValid():
            self._reportar_erro("O MDE reprojetado não é válido.")
            return
        QgsProject.instance().addMapLayer(camada_reprojetada)
        self._avancar_com_camada(camada_reprojetada)

    def _reprojecao_falhou(self, mensagem):
        self._parar_carregamento()
        self._reportar_erro(f"Falha ao reprojetar o MDE: {mensagem}")

    # ------------------------------------------------------------------
    # PÁGINA 3: baixar automaticamente (compacta)
    # ------------------------------------------------------------------
    def _criar_pagina_download(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: #F8FAFC; font-family: 'Segoe UI', Arial;")
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(20, 20, 20, 20)

        group = QGroupBox("Baixar MDE automaticamente")
        group.setStyleSheet(ESTILO_GROUPBOX)
        g_layout = QVBoxLayout(group)
        g_layout.setContentsMargins(15, 18, 15, 15)
        g_layout.setSpacing(8)

        lbl_epsg = QLabel("Projeção de saída:")
        self.combo_epsg_download = self._criar_combo_epsg()

        self.btn_baixar_mde = QPushButton("🌎 Baixar MDE (extensão do mapa)")
        self.btn_baixar_mde.setCursor(Qt.PointingHandCursor)
        self.btn_baixar_mde.setToolTip("Baixa automaticamente um MDE (Copernicus DEM 30m) cobrindo a extensão atual do mapa, já reprojetado conforme selecionado acima")
        self.btn_baixar_mde.setStyleSheet("QPushButton { background-color: #0D9488; color: white; font-weight: bold; border-radius: 4px; padding: 10px; font-size: 12px; } QPushButton:hover { background-color: #0F766E; }")
        self.btn_baixar_mde.clicked.connect(self.baixar_mde_da_extensao)

        g_layout.addWidget(lbl_epsg)
        g_layout.addWidget(self.combo_epsg_download)
        g_layout.addWidget(self.btn_baixar_mde)
        layout.addWidget(group)

        layout.addWidget(criar_painel_educativo_mde())

        self.painel_detalhes_download = criar_painel_detalhes_mde("O MDE baixado vai aparecer aqui com suas características.")
        self.painel_detalhes_download.hide()
        layout.addWidget(self.painel_detalhes_download)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_voltar = QPushButton("‹ Voltar")
        btn_voltar.setCursor(Qt.PointingHandCursor)
        btn_voltar.setStyleSheet("QPushButton { background-color: transparent; color: #718096; font-weight: bold; border: none; } QPushButton:hover { color: #1E507E; text-decoration: underline; }")
        btn_voltar.clicked.connect(lambda: self.stack.setCurrentWidget(self.pagina_pergunta))

        self.btn_proximo_download = QPushButton("Avançar para Malha Hídrica ➜")
        self.btn_proximo_download.setCursor(Qt.PointingHandCursor)
        self.btn_proximo_download.setStyleSheet(f"QPushButton {{ background-color: {COR_PRIMARIA}; color: white; font-weight: bold; padding: 8px 15px; border-radius: 4px; border: none; font-size: 12px; }} QPushButton:hover {{ background-color: {COR_PRIMARIA_HOVER}; }}")
        self.btn_proximo_download.setEnabled(False)
        self.btn_proximo_download.clicked.connect(self._avancar_download)

        btn_layout.addWidget(btn_voltar)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_proximo_download)
        layout.addLayout(btn_layout)

        return pagina

    def _ir_para_download(self):
        self.stack.setCurrentWidget(self.pagina_download)

    def baixar_mde_da_extensao(self):
        try:
            bbox = obter_bbox_wgs84_do_canvas()
        except Exception as e:
            self._reportar_erro(f"Falha ao obter a extensão do mapa: {str(e)}")
            return

        centro_lon = (bbox[0] + bbox[2]) / 2.0
        epsg_destino = self._parse_epsg_combo(self.combo_epsg_download, centro_lon=centro_lon)

        self._iniciar_carregamento("Baixando MDE (Copernicus DEM 30m)...")

        self._worker = TarefaGeoWorker(baixar_e_preparar_mde_arquivo, bbox, epsg_destino, nome_camada="MDE_Auto")
        self._worker.concluido.connect(self._download_concluido)
        self._worker.falhou.connect(self._download_falhou)
        self._worker.start()

    def _download_concluido(self, resultado):
        caminho, nome = resultado
        self._parar_carregamento()

        camada_mde = QgsRasterLayer(caminho, nome)
        if not camada_mde.isValid():
            self._reportar_erro("O MDE baixado/reprojetado não é válido.")
            return

        QgsProject.instance().addMapLayer(camada_mde)
        self.rasters[camada_mde.name()] = camada_mde
        self._mde_baixado = camada_mde

        self.painel_detalhes_download.lbl_conteudo.setText(formatar_detalhes_mde(camada_mde))
        self.painel_detalhes_download.show()
        self.btn_proximo_download.setEnabled(True)

        self._reportar_sucesso(f"MDE baixado e carregado: {camada_mde.name()}")

    def _download_falhou(self, mensagem):
        self._parar_carregamento()
        self._reportar_erro(f"Falha ao baixar o MDE automaticamente: {mensagem}")

    def _avancar_download(self):
        if self._mde_baixado is None:
            return
        self._avancar_com_camada(self._mde_baixado)

    # ------------------------------------------------------------------
    # PÁGINA 4: carregando (tela cheia, branca, só o satélite)
    # ------------------------------------------------------------------
    def _criar_pagina_carregando(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: white;")
        layout = QVBoxLayout(pagina)
        layout.addStretch()

        self.lbl_spinner_grande = QLabel("🛰️")
        self.lbl_spinner_grande.setAlignment(Qt.AlignCenter)
        self.lbl_spinner_grande.setStyleSheet("font-size: 72px;")
        layout.addWidget(self.lbl_spinner_grande)

        self.lbl_status_carregando = QLabel("Processando...")
        self.lbl_status_carregando.setAlignment(Qt.AlignCenter)
        self.lbl_status_carregando.setWordWrap(True)
        self.lbl_status_carregando.setStyleSheet(f"color: {COR_PRIMARIA}; font-size: 14px; font-weight: bold; margin: 16px 30px 0 30px;")
        layout.addWidget(self.lbl_status_carregando)

        self.barra_progresso = QProgressBar()
        self.barra_progresso.setRange(0, 0)
        self.barra_progresso.setTextVisible(False)
        self.barra_progresso.setFixedHeight(6)
        self.barra_progresso.setFixedWidth(220)
        self.barra_progresso.setStyleSheet(f"QProgressBar {{ border: none; background-color: #E2E8F0; border-radius: 3px; }} QProgressBar::chunk {{ background-color: {COR_DESTAQUE}; border-radius: 3px; }}")
        layout.addWidget(self.barra_progresso, alignment=Qt.AlignCenter)

        layout.addStretch()
        return pagina

    def _iniciar_carregamento(self, texto):
        self.lbl_status_carregando.setText(texto)
        self._pagina_anterior = self.stack.currentWidget()
        self.stack.setCurrentWidget(self.pagina_carregando)
        self._spinner_index = 0
        self.lbl_spinner_grande.setText(self._spinner_frames[0])
        self.timer_spinner.start(350)

    def _parar_carregamento(self):
        self.timer_spinner.stop()
        if self._pagina_anterior is not None:
            self.stack.setCurrentWidget(self._pagina_anterior)

    def _animar_spinner(self):
        self._spinner_index = (self._spinner_index + 1) % len(self._spinner_frames)
        self.lbl_spinner_grande.setText(self._spinner_frames[self._spinner_index])

    # ------------------------------------------------------------------
    def _avancar_com_camada(self, mde_layer):
        self.hide()
        parent_chat = self.parent()
        parent_chat.dialog_bacias = GeradorDrenagemDialog(parent_chat, mde_layer=mde_layer)
        parent_chat.dialog_bacias.show()

    def _reportar_erro(self, mensagem):
        parent_chat = self.parent()
        if parent_chat is not None and hasattr(parent_chat, "chat_history"):
            parent_chat.chat_history.append(f"<span style='color: #e74c3c;'>❌ {mensagem}</span>")
            parent_chat.rolar_chat()
        else:
            QMessageBox.critical(self, "Erro", mensagem)

    def _reportar_sucesso(self, mensagem):
        parent_chat = self.parent()
        if parent_chat is not None and hasattr(parent_chat, "chat_history"):
            parent_chat.chat_history.append(f"<span style='color: {COR_DESTAQUE};'>✅ {mensagem}</span>")
            parent_chat.rolar_chat()
        else:
            QMessageBox.information(self, "H-Geo", mensagem)

def _tarefa_watershed(task, mde_layer, thr):
    """Roda no worker thread de uma QgsTask (mecanismo oficial do QGIS para
    algoritmos de Processing em segundo plano — mais seguro que uma QThread
    crua para chamadas que envolvem o GRASS via 'processing.run')."""
    alg = "grass7:r.watershed" if QgsApplication.processingRegistry().algorithmById("grass7:r.watershed") else "grass:r.watershed"
    params = {
        'elevation': mde_layer,
        'threshold': int(thr),
        'drainage': 'TEMPORARY_OUTPUT',
        'stream': 'TEMPORARY_OUTPUT'
    }
    res = processing.run(alg, params)
    return {'drainage': res['drainage'], 'stream': res['stream']}

class GeradorDrenagemDialog(QDialog):
    def __init__(self, parent=None, mde_layer=None):
        super(GeradorDrenagemDialog, self).__init__(parent)
        self.setWindowTitle("Passo 2: Gerar Malha Hídrica")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.resize(440, 460)

        self.mde_layer = mde_layer
        self._task = None

        self._spinner_frames = ["💧", "🌊", "💦", "🌊"]
        self._spinner_index = 0
        self.timer_spinner = QTimer(self)
        self.timer_spinner.timeout.connect(self._animar_spinner)

        layout_raiz = QVBoxLayout(self)
        layout_raiz.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedWidget()
        layout_raiz.addWidget(self.stack)

        self.pagina_principal = self._criar_pagina_principal()
        self.pagina_carregando = self._criar_pagina_carregando()
        self.stack.addWidget(self.pagina_principal)
        self.stack.addWidget(self.pagina_carregando)
        self.stack.setCurrentWidget(self.pagina_principal)

    def _criar_pagina_principal(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: #F8FAFC; font-family: 'Segoe UI', Arial;")
        layout = QVBoxLayout(pagina)
        layout.setContentsMargins(20, 20, 20, 20)

        if self.mde_layer is None:
            lbl_mde = QLabel("<span style='color:#e74c3c;'>⚠️ Nenhum MDE recebido do Passo 1.</span>")
            lbl_mde.setWordWrap(True)
            lbl_mde.setStyleSheet("font-size: 11px; margin-bottom: 6px;")
            layout.addWidget(lbl_mde)

        painel_educativo = QWidget()
        painel_educativo.setStyleSheet("background-color: #EEF2FF; border: 1px solid #C7D2FE; border-radius: 6px; padding: 10px;")
        lay_edu = QVBoxLayout(painel_educativo)
        lay_edu.setContentsMargins(10, 10, 10, 10)
        titulo_edu = QLabel("📚 <b>O que é a Malha Hídrica?</b>")
        titulo_edu.setStyleSheet("color: #4338CA; font-size: 12px;")
        texto_edu = QLabel(
            "A partir do MDE, o algoritmo <b>r.watershed</b> (GRASS) calcula duas camadas:<br>"
            "• <b>Direção de Fluxo</b>: para cada pixel, indica para qual dos 8 vizinhos a água escoaria.<br>"
            "• <b>Rede de Drenagem (Rios)</b>: soma o fluxo acumulado de todos os pixels a montante e "
            "marca como 'rio' toda célula que ultrapassa o Limiar definido abaixo."
        )
        texto_edu.setWordWrap(True)
        texto_edu.setStyleSheet("color: #4A5568; font-size: 11px;")
        lay_edu.addWidget(titulo_edu)
        lay_edu.addWidget(texto_edu)
        layout.addWidget(painel_educativo)

        group1 = QGroupBox("Parâmetro de Geração")
        group1.setStyleSheet(ESTILO_GROUPBOX)
        g1_layout = QGridLayout(group1)
        g1_layout.setContentsMargins(15, 20, 15, 15)

        lbl_thr = QLabel("Limiar de Drenagem (Pixels):")
        self.input_thr = QLineEdit("1000")
        self.input_thr.setStyleSheet(ESTILO_INPUT)

        painel_info = QWidget()
        painel_info.setStyleSheet("background-color: #EBF4FA; border: 1px solid #BEE3F8; border-radius: 6px; padding: 10px; margin-top: 10px;")
        info_layout = QVBoxLayout(painel_info)
        info_layout.setContentsMargins(10, 10, 10, 10)
        lbl_info_titulo = QLabel("ℹ️ <b>O que é o Limiar (Threshold)?</b>")
        lbl_info_titulo.setStyleSheet("color: #2B6CB0; font-size: 12px;")
        lbl_info_texto = QLabel(
            "É o tamanho mínimo de contribuição para formar um rio.<br>"
            "• <b>Menor que 500:</b> Gera micro-córregos (rede densa).<br>"
            "• <b>1000 a 5000:</b> Ideal para bacias médias.<br>"
            "• <b>Maior que 10000:</b> Apenas grandes rios regionais."
        )
        lbl_info_texto.setStyleSheet("color: #4A5568; font-size: 11px;")
        info_layout.addWidget(lbl_info_titulo)
        info_layout.addWidget(lbl_info_texto)

        g1_layout.addWidget(lbl_thr, 0, 0)
        g1_layout.addWidget(self.input_thr, 0, 1)
        g1_layout.addWidget(painel_info, 1, 0, 1, 2)

        layout.addWidget(group1)
        layout.addStretch()

        btn_layout = QHBoxLayout()
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.setStyleSheet("QPushButton { background-color: transparent; color: #718096; font-weight: bold; border: none; } QPushButton:hover { color: #E53E3E; text-decoration: underline; }")
        self.btn_cancel.clicked.connect(self.reject)

        self.btn_passo1 = QPushButton("Gerar Calhas de Rios")
        self.btn_passo1.setCursor(Qt.PointingHandCursor)
        self.btn_passo1.setStyleSheet(f"QPushButton {{ background-color: {COR_PRIMARIA}; color: white; font-weight: bold; padding: 8px 15px; border-radius: 4px; border: none; font-size: 12px; }} QPushButton:hover {{ background-color: {COR_PRIMARIA_HOVER}; }}")
        self.btn_passo1.setEnabled(self.mde_layer is not None)
        self.btn_passo1.clicked.connect(self.executar_passo1)

        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_passo1)
        layout.addLayout(btn_layout)

        return pagina

    def _criar_pagina_carregando(self):
        pagina = QWidget()
        pagina.setStyleSheet("background-color: white;")
        layout = QVBoxLayout(pagina)
        layout.addStretch()

        self.lbl_spinner_grande = QLabel("💧")
        self.lbl_spinner_grande.setAlignment(Qt.AlignCenter)
        self.lbl_spinner_grande.setStyleSheet("font-size: 72px;")
        layout.addWidget(self.lbl_spinner_grande)

        self.lbl_status_carregando = QLabel("Processando...")
        self.lbl_status_carregando.setAlignment(Qt.AlignCenter)
        self.lbl_status_carregando.setWordWrap(True)
        self.lbl_status_carregando.setStyleSheet(f"color: {COR_PRIMARIA}; font-size: 14px; font-weight: bold; margin: 16px 30px 0 30px;")
        layout.addWidget(self.lbl_status_carregando)

        self.barra_progresso = QProgressBar()
        self.barra_progresso.setRange(0, 0)
        self.barra_progresso.setTextVisible(False)
        self.barra_progresso.setFixedHeight(6)
        self.barra_progresso.setFixedWidth(220)
        self.barra_progresso.setStyleSheet(f"QProgressBar {{ border: none; background-color: #E2E8F0; border-radius: 3px; }} QProgressBar::chunk {{ background-color: {COR_DESTAQUE}; border-radius: 3px; }}")
        layout.addWidget(self.barra_progresso, alignment=Qt.AlignCenter)

        layout.addStretch()
        return pagina

    def _iniciar_carregamento(self, texto):
        self.lbl_status_carregando.setText(texto)
        self.stack.setCurrentWidget(self.pagina_carregando)
        self._spinner_index = 0
        self.lbl_spinner_grande.setText(self._spinner_frames[0])
        self.timer_spinner.start(350)

    def _parar_carregamento(self):
        self.timer_spinner.stop()
        self.stack.setCurrentWidget(self.pagina_principal)

    def _animar_spinner(self):
        self._spinner_index = (self._spinner_index + 1) % len(self._spinner_frames)
        self.lbl_spinner_grande.setText(self._spinner_frames[self._spinner_index])

    def executar_passo1(self):
        thr = self.input_thr.text()
        if self.mde_layer is None:
            self._reportar_erro("Nenhum MDE recebido do Passo 1.")
            return
        if not thr.isdigit():
            self._reportar_erro("Informe um limiar de drenagem válido (número inteiro).")
            return

        self._iniciar_carregamento("Gerando malha hídrica (r.watershed)...\nDireção de fluxo e rede de drenagem")

        mde_layer = self.mde_layer
        self._task = QgsTask.fromFunction(
            "Gerar malha hídrica",
            lambda task, layer=mde_layer, limiar=thr: _tarefa_watershed(task, layer, limiar),
            on_finished=self._watershed_concluido
        )
        QgsApplication.taskManager().addTask(self._task)

    def _watershed_concluido(self, exception, resultado=None):
        self._parar_carregamento()

        if exception is not None:
            self._reportar_erro(f"Falha ao gerar a malha hídrica: {str(exception)}")
            return

        try:
            thr = self.input_thr.text()
            dir_layer = QgsRasterLayer(resultado['drainage'], f"Direcao_Fluxo (Limiar {thr})")
            stream_layer = QgsRasterLayer(resultado['stream'], f"Rios_Drenagem (Limiar {thr})")

            if dir_layer.isValid() and stream_layer.isValid():
                QgsProject.instance().addMapLayer(dir_layer)
                QgsProject.instance().addMapLayer(stream_layer)
                self._reportar_sucesso("Malha hídrica gerada com sucesso.")
                self.accept()
                self.abrir_passo_3()
            else:
                self._reportar_erro("Falha ao gerar as camadas de direção de fluxo/drenagem (raster inválido).")
        except Exception as e:
            self._reportar_erro(f"Falha ao processar o resultado da malha hídrica: {str(e)}")

    def _reportar_erro(self, mensagem):
        parent_chat = self.parent()
        if parent_chat is not None and hasattr(parent_chat, "chat_history"):
            parent_chat.chat_history.append(f"<span style='color: #e74c3c;'>❌ {mensagem}</span>")
            parent_chat.rolar_chat()
        else:
            QMessageBox.critical(self, "Erro", mensagem)

    def _reportar_sucesso(self, mensagem):
        parent_chat = self.parent()
        if parent_chat is not None and hasattr(parent_chat, "chat_history"):
            parent_chat.chat_history.append(f"<span style='color: {COR_DESTAQUE};'>✅ {mensagem}</span>")
            parent_chat.rolar_chat()
        else:
            QMessageBox.information(self, "H-Geo", mensagem)

    def abrir_passo_3(self):
        self.parent().janela_passo3 = ExtratorExutorioDialog(self.parent())
        self.parent().janela_passo3.show()

# ==========================================
# MÓDULO PRINCIPAL DO COPILOTO (A MÁGICA DA UI)
# ==========================================
# === H-GEO: RELATORIO COMPARATIVO v1 ===
# Self-contained addition: no companion module or credential file is required.
import csv as _hgr_csv
import hashlib as _hgr_hashlib
import json as _hgr_json
import math as _hgr_math
import os as _hgr_os
import re as _hgr_re
import time as _hgr_time
import uuid as _hgr_uuid
import tempfile as _hgr_tempfile
import xml.etree.ElementTree as _hgr_xml
from pathlib import Path as _HgrPath
from datetime import date as _HgrDate, timedelta as _HgrDelta, datetime as _HgrDateTime
from qgis.PyQt.QtCore import QDate, QThread, pyqtSignal, QUrl, QTimer, Qt, QSize
from qgis.PyQt.QtGui import QDesktopServices, QPixmap, QColor
from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox,
    QFileDialog, QLineEdit, QCheckBox, QGroupBox, QGridLayout, QTextEdit,
    QScrollArea, QWidget, QGraphicsView, QGraphicsScene, QFrame)
from qgis.core import (QgsProject, QgsGeometry, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform, QgsMapLayerProxyModel)
from qgis.gui import QgsMapLayerComboBox

HGR_INDEX_DETAILS = {'NDVI': ('Índice de Vegetação por Diferença Normalizada', 'Normalized Difference Vegetation Index', 'Realça a resposta da vegetação verde. Valores maiores costumam indicar maior presença ou vigor de vegetação; não medem saúde isoladamente.'), 'EVI': ('Índice de Vegetação Realçado', 'Enhanced Vegetation Index', 'Realça a vegetação com ajustes para efeitos do solo e da atmosfera. Valores maiores costumam acompanhar vegetação mais densa ou ativa.'), 'SAVI': ('Índice de Vegetação Ajustado ao Solo', 'Soil-Adjusted Vegetation Index', 'Realça a vegetação reduzindo a influência do solo exposto. A interpretação depende do parâmetro L e da cobertura vegetal.'), 'NDWI': ('Índice de Água por Diferença Normalizada', 'Normalized Difference Water Index', 'Nesta variante (McFeeters), realça água aberta. Valores maiores podem indicar água, mas sombras e construções também influenciam.'), 'MNDWI': ('Índice de Água por Diferença Normalizada Modificado', 'Modified Normalized Difference Water Index', 'Realça água aberta usando verde e infravermelho de ondas curtas. Valores maiores favorecem água; não delimitam corpos hídricos sozinhos.'), 'NDMI': ('Índice de Umidade por Diferença Normalizada', 'Normalized Difference Moisture Index', 'É sensível à água na vegetação. Valores maiores podem acompanhar vegetação mais úmida; não são uma medida direta da umidade do solo.'), 'NDSI': ('Índice de Neve por Diferença Normalizada', 'Normalized Difference Snow Index', 'Foi desenvolvido para realçar neve. Água e outras superfícies podem produzir resposta semelhante; não deve ser interpretado como neve neste campus.'), 'NBR': ('Razão de Queimada Normalizada', 'Normalized Burn Ratio', 'Compara infravermelho próximo e ondas curtas. Uma redução pode ser compatível com queimadas, mas também com outras mudanças da vegetação.'), 'NBR2': ('Razão de Queimada Normalizada 2', 'Normalized Burn Ratio 2', 'Compara duas bandas de ondas curtas, sensíveis à umidade e a alterações da superfície. O resultado isolado não comprova fogo ou sua severidade.'), 'NDBI': ('Índice de Áreas Construídas por Diferença Normalizada', 'Normalized Difference Built-up Index', 'Realça respostas associadas a superfícies construídas. Solo exposto também pode apresentar valores altos; não é uma classificação urbana.'), 'BAI': ('Índice de Área Queimada', 'Burn Area Index', 'Realça respostas espectrais próximas às de carvão e áreas queimadas. Valores altos exigem conferência visual e contexto; não comprovam queima.'), 'UI': ('Índice Urbano', 'Urban Index', 'Nesta variante normalizada, compara SWIR2 e infravermelho próximo. Valores altos podem ocorrer em construções e solo exposto.')}

HGR_INDICES = {
    'NDVI': ('Vegetação verde', ('NIR', 'RED'), '(NIR-RED)/(NIR+RED)'),
    'EVI': ('Vegetação com ajustes', ('NIR', 'RED', 'BLUE'),
            '2,5(NIR-RED)/(NIR+6RED-7,5BLUE+1)'),
    'SAVI': ('Vegetação / ajuste do solo', ('NIR', 'RED'),
             '(1+L)(NIR-RED)/(NIR+RED+L)'),
    'NDWI': ('Água aberta (McFeeters)', ('GREEN', 'NIR'),
             '(GREEN-NIR)/(GREEN+NIR)'),
    'MNDWI': ('Água aberta / SWIR', ('GREEN', 'SWIR1'),
              '(GREEN-SWIR1)/(GREEN+SWIR1)'),
    'NDMI': ('Umidade da vegetação', ('NIR', 'SWIR1'),
             '(NIR-SWIR1)/(NIR+SWIR1)'),
    'NDSI': ('Neve', ('GREEN', 'SWIR1'), '(GREEN-SWIR1)/(GREEN+SWIR1)'),
    'NBR': ('Resposta espectral / queimadas', ('NIR', 'SWIR2'),
            '(NIR-SWIR2)/(NIR+SWIR2)'),
    'NBR2': ('Resposta SWIR / pós-fogo', ('SWIR1', 'SWIR2'),
             '(SWIR1-SWIR2)/(SWIR1+SWIR2)'),
    'NDBI': ('Superfícies construídas / solo', ('SWIR1', 'NIR'),
             '(SWIR1-NIR)/(SWIR1+NIR)'),
    'BAI': ('Realce de áreas queimadas', ('RED', 'NIR'),
            '1/((0,1-RED)²+(0,06-NIR)²)'),
    'UI': ('Índice urbano normalizado', ('SWIR2', 'NIR'),
           '(SWIR2-NIR)/(SWIR2+NIR)'),
}
HGR_ASSETS = dict(BLUE='B02', GREEN='B03', RED='B04', NIR='B08',
                  SWIR1='B11', SWIR2='B12')
HGR_BAND_IDS = dict(BLUE=1, GREEN=2, RED=3, NIR=7, SWIR1=11, SWIR2=12)
HGR_SOURCES = [
    ('PAB / Embrapa: NDVI, EVI, SAVI',
     'https://ainfo.cnptia.embrapa.br/digital/bitstream/item/85330/1/Indices-de-vegetacao.pdf'),
    ('Geoscience Australia: NDWI, MNDWI, NDBI, BAI',
     'https://knowledge.dea.ga.gov.au/_modules/dea_tools/bandindices/'),
    ('USGS: índices de reflectância Landsat',
     'https://www.usgs.gov/landsat-missions/landsat-surface-reflectance-derived-spectral-indices'),
    ('UI normalizado SWIR2/NIR: equação 4',
     'https://pmc.ncbi.nlm.nih.gov/articles/PMC10485128/'),
    ('Imagens Sentinel-2 L2A',
     'https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a'),
]


def _hgr_check(cancel):
    if cancel and cancel():
        raise InterruptedError('Operação cancelada. Nenhum PDF incompleto será publicado.')


class _HgrQgisLogStream:
    """Output for orphaned console handlers in a GUI-only QGIS process."""
    encoding = 'utf-8'
    closed = False

    def write(self, message):
        from qgis.core import QgsMessageLog, Qgis
        text = str(message).strip()
        if text:
            # Network debug records can contain temporary asset credentials.
            text = _hgr_re.sub(r'([?&](?:sig|token|access_token)=)[^&\s\"\']+',
                               r'\1[removido]', text, flags=_hgr_re.IGNORECASE)
            QgsMessageLog.logMessage(text, 'H-GEO / bibliotecas', Qgis.Info)
        return len(str(message))

    def flush(self):
        pass


def _hgr_ensure_gui_logging():
    """Repair only broken console outputs, leaving levels and file logs intact."""
    import logging
    loggers = [logging.getLogger()]
    loggers.extend(value for value in list(logging.Logger.manager.loggerDict.values())
                   if isinstance(value, logging.Logger))
    seen = set()
    repaired = 0
    for logger in loggers:
        for handler in list(logger.handlers):
            if id(handler) in seen:
                continue
            seen.add(id(handler))
            # FileHandler.stream=None is legitimate for delayed file opening.
            if not isinstance(handler, logging.StreamHandler) or isinstance(handler, logging.FileHandler):
                continue
            handler.acquire()
            try:
                stream = handler.stream
                if stream is None or getattr(stream, 'closed', False):
                    handler.stream = _HgrQgisLogStream()
                    repaired += 1
            finally:
                handler.release()
    return repaired


# Re-executing the script also repairs handlers left over from the prior session.
_hgr_ensure_gui_logging()


def _hgr_http(url, cancel=None, payload=None):
    import requests
    _hgr_ensure_gui_logging()
    for attempt in range(3):
        _hgr_check(cancel)
        try:
            response = requests.request('POST' if payload is not None else 'GET',
                                        url, json=payload, timeout=(15, 45))
        except requests.RequestException:
            if attempt == 2:
                raise RuntimeError('Falha de conexão com o serviço de imagens. Tente novamente.')
            continue
        if response.status_code in (429, 502, 503, 504) and attempt < 2:
            try:
                delay = min(60, max(2, int(response.headers.get('Retry-After', '5'))))
            except ValueError:
                delay = 5
            until = _hgr_time.monotonic() + delay
            while _hgr_time.monotonic() < until:
                _hgr_check(cancel)
                _hgr_time.sleep(.15)
            continue
        if response.status_code >= 400:
            # Never include signed URLs or access tokens in error messages.
            raise RuntimeError('Serviço de imagens respondeu HTTP %s.' % response.status_code)
        return response
    raise RuntimeError('Serviço temporariamente indisponível.')


def _hgr_catalog(config, cancel=None, progress=lambda s: None):
    from osgeo import ogr
    geometry = ogr.CreateGeometryFromJson(_hgr_json.dumps(config['geometry']))
    xmin, xmax, ymin, ymax = geometry.GetEnvelope()
    bbox = [xmin, ymin, xmax, ymax]
    options = []
    for target in config['targets']:
        _hgr_check(cancel)
        day = _HgrDate.fromisoformat(target)
        delta = _HgrDelta(days=config['window_days'])
        end = min(day + delta, _HgrDate.today())
        start = day - delta
        progress('Consultando cenas próximas a %s...' % target)
        body = {'collections': ['sentinel-2-l2a'], 'bbox': bbox,
                'datetime': '%sT00:00:00Z/%sT23:59:59Z' % (start, end),
                'query': {'eo:cloud_cover': {'lte': config['scene_cloud_pct']}},
                'limit': 100}
        data = _hgr_http('https://planetarycomputer.microsoft.com/api/stac/v1/search',
                         cancel, body).json()
        features = data.get('features', [])
        # Follow STAC pagination, retaining a bounded local query.
        for _ in range(4):
            nxt = next((x for x in data.get('links', []) if x.get('rel') == 'next'), None)
            if not nxt:
                break
            progress('Consultando próxima página do catálogo...')
            method = nxt.get('method', 'GET').upper()
            next_body = nxt.get('body', body)
            if nxt.get('merge') and method == 'POST':
                next_body = dict(body, **next_body)
            data = _hgr_http(nxt['href'], cancel,
                             next_body if method == 'POST' else None).json()
            features.extend(data.get('features', []))
        if any(x.get('rel') == 'next' for x in data.get('links', [])):
            raise ValueError('Muitas cenas. Reduza a área ou a janela de busca para consultar todas as opções.')
        grouped = {}
        for feature in features:
            props = feature.get('properties', {})
            capture = props.get('datetime', '')[:10]
            if not capture or not (start.isoformat() <= capture <= end.isoformat()):
                continue
            tile = props.get('s2:mgrs_tile') or _hgr_re.search(r'_T([A-Z0-9]+)_', feature['id'])
            tile = tile.group(1) if hasattr(tile, 'group') else tile
            tile = tile or feature['id']
            # Prefer the least cloudy observation per tile on a given date.
            by_tile = grouped.setdefault(capture, {})
            old = by_tile.get(tile)
            cloud = props.get('eo:cloud_cover', 100)
            if old is None or cloud < old['properties'].get('eo:cloud_cover', 100):
                by_tile[tile] = feature
        groups = []
        for capture, tiles in grouped.items():
            fs = list(tiles.values())
            groups.append({'date': capture, 'features': fs,
                           'cloud': sum(x['properties'].get('eo:cloud_cover', 100) for x in fs) / len(fs)})
        groups.sort(key=lambda x: (abs((_HgrDate.fromisoformat(x['date']) - day).days), x['cloud']))
        if not groups:
            raise ValueError('Nenhuma cena para %s. Amplie a janela ou o limite de nuvens.' % target)
        options.append(groups)
    return options


def _hgr_grid(geometry, resolution):
    from osgeo import ogr, osr
    area = ogr.CreateGeometryFromJson(_hgr_json.dumps(geometry))
    if area is None or area.IsEmpty() or not area.IsValid():
        raise ValueError('A área deve conter polígonos válidos.')
    xmin, xmax, ymin, ymax = area.GetEnvelope()
    lon, lat = (xmin + xmax) / 2, (ymin + ymax) / 2
    if not (-80 <= ymin <= ymax <= 84) or xmax - xmin > 6:
        raise ValueError('Esta versão usa UTM local. Divida áreas polares ou muito extensas.')
    zone = max(1, min(60, int((lon + 180) // 6) + 1))
    epsg = (32600 if lat >= 0 else 32700) + zone
    wgs = osr.SpatialReference(); wgs.ImportFromEPSG(4326)
    utm = osr.SpatialReference(); utm.ImportFromEPSG(epsg)
    for srs in (wgs, utm):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    projected = area.Clone()
    projected.Transform(osr.CoordinateTransformation(wgs, utm))
    x0, x1, y0, y1 = projected.GetEnvelope()
    r = int(resolution)
    bounds = [_hgr_math.floor(x0/r)*r-2*r, _hgr_math.floor(y0/r)*r-2*r,
              _hgr_math.ceil(x1/r)*r+2*r, _hgr_math.ceil(y1/r)*r+2*r]
    width, height = round((bounds[2]-bounds[0])/r), round((bounds[3]-bounds[1])/r)
    if width * height > 2_000_000:
        raise ValueError('A área excede 2 milhões de pixels. Aumente o pixel ou divida a área.')
    return {'epsg': epsg, 'wkt': utm.ExportToWkt(), 'bounds': bounds,
            'width': width, 'height': height, 'resolution': r,
            'bbox_wgs84': [xmin-.001, ymin-.001, xmax+.001, ymax+.001]}


def _hgr_compute_index(name, bands, savi_l=.5):
    import numpy as np
    required = HGR_INDICES[name][1]
    shape = bands[required[0]].shape
    valid = np.ones(shape, dtype=bool)
    for key in required:
        if bands[key].shape != shape:
            raise ValueError('Bandas sem alinhamento para %s.' % name)
        valid &= np.isfinite(bands[key])
    a, b = (bands[k] for k in required[:2])
    if name == 'SAVI':
        numerator, denominator = (1+savi_l)*(a-b), a+b+savi_l
    elif name == 'EVI':
        numerator, denominator = 2.5*(a-b), a+6*b-7.5*bands['BLUE']+1
    elif name == 'BAI':
        numerator, denominator = np.ones(shape), (.1-bands['RED'])**2+(.06-bands['NIR'])**2
    else:
        numerator, denominator = a-b, a+b
    valid &= np.abs(denominator) > 1e-8
    result = np.full(shape, np.nan, dtype='float64')
    np.divide(numerator, denominator, out=result, where=valid)
    result[~np.isfinite(result)] = np.nan
    return result


def _hgr_write_raster(path, array, grid):
    import numpy as np
    from osgeo import gdal
    ds = gdal.GetDriverByName('GTiff').Create(str(path), grid['width'], grid['height'],
                                             1, gdal.GDT_Float32, options=['COMPRESS=DEFLATE'])
    r = grid['resolution']; x0, y0, x1, y1 = grid['bounds']
    ds.SetGeoTransform((x0, r, 0, y1, 0, -r)); ds.SetProjection(grid['wkt'])
    ds.GetRasterBand(1).SetNoDataValue(-9999)
    ds.GetRasterBand(1).WriteArray(np.where(np.isfinite(array), array, -9999).astype('float32'))
    ds.FlushCache()
    ds = None


def _hgr_is_local(href):
    return not href.startswith(('https://', 'http://')) and _HgrPath(href).is_file()


def _hgr_signed_url(href, tokens, cancel):
    from urllib.parse import urlparse
    if _hgr_is_local(href):  # Offline inputs are useful for reproducible tests.
        return href
    parsed = urlparse(href)
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.blob.core.windows.net'):
        raise ValueError('Asset de imagem fora do armazenamento esperado do catálogo.')
    account = parsed.hostname.split('.')[0]
    container = parsed.path.strip('/').split('/')[0]
    key = account + '/' + container
    if key not in tokens:
        tokens[key] = _hgr_http('https://planetarycomputer.microsoft.com/api/sas/v1/token/' + key,
                                cancel).json()['token']
    return href.split('?')[0] + '?' + tokens[key]


def _hgr_prepare_date(group, config, grid, folder, tokens, cancel, progress, local_assets=None):
    import numpy as np
    from osgeo import gdal
    needed = set(k for name in config['indices'] for k in HGR_INDICES[name][1])
    needed.update(('BLUE', 'GREEN', 'RED'))
    sources = {k: [] for k in sorted(needed) + ['SCL']}
    provenance = []
    box = grid['bbox_wgs84']
    for feature in sorted(group['features'], key=lambda f: -f['properties'].get('eo:cloud_cover', 100)):
        _hgr_check(cancel)
        fid = feature['id']
        overrides = (local_assets or {}).get(fid, {})
        def href(key):
            return overrides.get(key) or feature['assets'][key]['href']
        meta_url = _hgr_signed_url(href('product-metadata'), tokens, cancel)
        if _hgr_is_local(meta_url):
            content = _HgrPath(meta_url).read_bytes()
        else:
            content = _hgr_http(meta_url, cancel).content
        tree = _hgr_xml.fromstring(content)
        quant = next((float(e.text) for e in tree.iter()
                      if e.tag.split('}')[-1] == 'BOA_QUANTIFICATION_VALUE'), None)
        offsets = {int(e.attrib['band_id']): float(e.text) for e in tree.iter()
                   if e.tag.split('}')[-1] == 'BOA_ADD_OFFSET'}
        if not quant or quant <= 0:
            raise ValueError('Metadado de reflectância ausente na cena %s.' % fid)
        baseline = feature['properties'].get('s2:processing_baseline', '0')
        if float(baseline) >= 4 and not offsets:
            raise ValueError('Offset radiométrico ausente no metadado de %s.' % fid)
        scene_folder = folder / ('cena_' + _hgr_hashlib.sha256(fid.encode('utf-8')).hexdigest()[:12])
        scene_folder.mkdir()
        (scene_folder/'metadado.xml').write_bytes(content)
        provenance.append({'id': fid, 'datetime': feature['properties'].get('datetime'),
                           'cloud_pct': feature['properties'].get('eo:cloud_cover'),
                           'quantification': quant, 'offsets': offsets})
        for key in sources:
            _hgr_check(cancel)
            progress('%s: preparando %s...' % (group['date'], key))
            asset = 'SCL' if key == 'SCL' else HGR_ASSETS[key]
            url = _hgr_signed_url(href(asset), tokens, cancel)
            remote = url if _hgr_is_local(url) else '/vsicurl/' + url
            gdal.PushErrorHandler('CPLQuietErrorHandler')
            try:
                ds = gdal.Translate('', remote, format='MEM',
                    projWin=[box[0], box[3], box[2], box[1]], projWinSRS='EPSG:4326')
            except Exception:
                # GDAL exceptions can embed signed URLs. Keep the GUI/log message clean.
                raise RuntimeError('Não foi possível ler %s em %s. Tente outra cena.' % (key, group['date']))
            finally:
                gdal.PopErrorHandler()
            if ds is None:
                raise RuntimeError('Leitura vazia da banda %s.' % key)
            arr = ds.ReadAsArray().astype('float64')
            band = ds.GetRasterBand(1)
            nodata = band.GetNoDataValue()
            valid = np.isfinite(arr) & (arr != 0)
            if nodata is not None:
                valid &= arr != nodata
            if key != 'SCL':
                arr = (arr + offsets.get(HGR_BAND_IDS[key], 0)) / quant
            path = scene_folder/(key+'.tif')
            target = gdal.GetDriverByName('GTiff').Create(str(path), ds.RasterXSize,
                ds.RasterYSize, 1, gdal.GDT_Float32, options=['COMPRESS=DEFLATE'])
            target.SetGeoTransform(ds.GetGeoTransform()); target.SetProjection(ds.GetProjection())
            target.GetRasterBand(1).SetNoDataValue(-9999)
            target.GetRasterBand(1).WriteArray(np.where(valid, arr, -9999).astype('float32'))
            target.FlushCache(); target = None; ds = None
            sources[key].append(str(path))
    arrays = {}
    for key, paths in sources.items():
        _hgr_check(cancel)
        ds = gdal.Warp('', paths, format='MEM', dstSRS=grid['wkt'],
            outputBounds=grid['bounds'], width=grid['width'], height=grid['height'],
            resampleAlg='near' if key == 'SCL' else 'bilinear', srcNodata=-9999, dstNodata=-9999)
        if ds is None:
            raise RuntimeError('Não foi possível alinhar %s.' % key)
        arr = ds.ReadAsArray().astype('float64'); arr[arr == -9999] = np.nan
        arrays[key] = arr; ds = None
    arrays['quality'] = np.isin(arrays['SCL'], [2, 4, 5, 6, 7])
    return arrays, provenance


def _hgr_pdf(path, config, grid, arrays, results, mask, rows, cancel):
    import numpy as np
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.ticker import FuncFormatter
    from osgeo import ogr, osr
    extent = [grid['bounds'][0], grid['bounds'][2], grid['bounds'][1], grid['bounds'][3]]
    geo = ogr.CreateGeometryFromJson(_hgr_json.dumps(config['geometry']))
    src = osr.SpatialReference(); src.ImportFromEPSG(4326)
    dst = osr.SpatialReference(); dst.ImportFromEPSG(grid['epsg'])
    for s in (src, dst):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    geo.Transform(osr.CoordinateTransformation(src, dst))
    rings = []
    def collect(g):
        if g.GetGeometryName() == 'LINEARRING':
            rings.append(np.array(g.GetPoints())[:, :2])
        else:
            for j in range(g.GetGeometryCount()):
                collect(g.GetGeometryRef(j))
    collect(geo)
    navy, muted = '#17434b', '#536a70'
    total = len(config['indices']) + 3
    dates = [x['date'] for x in config['groups']]
    def page(title, number, subtitle):
        fig = Figure(figsize=(11.69, 8.27), facecolor='#fbfaf6'); FigureCanvasAgg(fig)
        fig.text(.055,.95,'H-GEO / RELATÓRIO COMPARATIVO',size=9,weight='bold',color=navy)
        fig.text(.055,.9,title,size=18 if len(title)>48 else 22,weight='bold',color=navy)
        fig.text(.055,.86,subtitle,size=9,color=muted)
        fig.text(.055,.025,'Sentinel-2 L2A / Copernicus / Microsoft Planetary Computer',size=7,color=muted)
        fig.text(.945,.025,'%02d / %02d' % (number,total),ha='right',size=7,color=muted)
        return fig
    def map_axes(fig, pos, data, title, cmap=None, limits=None):
        ax = fig.add_axes(pos)
        if cmap:
            im = ax.imshow(np.ma.masked_invalid(data), extent=extent, cmap=cmap,
                           vmin=limits[0], vmax=limits[1], interpolation='nearest')
        else:
            im = ax.imshow(data, extent=extent, interpolation='nearest')
        for ring in rings:
            ax.plot(ring[:,0],ring[:,1],color='white',lw=2)
            ax.plot(ring[:,0],ring[:,1],color=navy,lw=.6)
        ax.set_title(title,size=11,color=navy)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v,p:'%.1f' % (v/1000)))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v,p:'%.1f' % (v/1000)))
        ax.tick_params(labelsize=6);ax.set_xlabel('E (km)',size=7);ax.set_ylabel('N (km)',size=7)
        ax.set_xlim(extent[:2]);ax.set_ylim(extent[2:]);ax.set_aspect('equal')
        ax.annotate('N',xy=(.92,.95),xytext=(.92,.84),xycoords='axes fraction',ha='center',
                    size=8,color=navy,arrowprops={'arrowstyle':'-|>','color':navy})
        length = grid['resolution'] * max(1, round(grid['width']/5))
        x = extent[0]+(extent[1]-extent[0])*.07
        y = extent[2]+(extent[3]-extent[2])*.06
        ax.plot([x,x+length],[y,y],color='white',lw=4)
        ax.plot([x,x+length],[y,y],color=navy,lw=1.5)
        ax.text(x+length/2,y+(extent[3]-extent[2])*.025,'%g m' % length,ha='center',size=6,
                bbox={'facecolor':'white','alpha':.8,'edgecolor':'none','pad':1})
        return im
    rgb = [np.clip(np.stack([a[k] for k in ('RED','GREEN','BLUE')],axis=-1)/.32,0,1)**(1/1.8)
           for a in arrays]
    with PdfPages(str(path)) as pdf:
        fig = page(config['area_name'],1,'%s × %s | Grade: %s m | EPSG:%s' %
                   (dates[0],dates[1],grid['resolution'],grid['epsg']))
        for j in range(2):
            map_axes(fig,[.06+j*.46,.24,.40,.55],rgb[j],dates[j])
        coverage = 100 * np.count_nonzero(mask) / config['area_pixels']
        fig.text(.055,.16,'Cor natural (B4/B3/B2), mesmo realce. Área válida em comum: %.1f%%.' % coverage,
                 size=10,color=navy)
        fig.text(.055,.105,'As datas acima são as aquisições escolhidas, não estimativas.\n'
                 'Os índices descrevem resposta espectral; mudanças não comprovam causa ou ganho/perda de cobertura.',
                 size=9,color=muted,va='top',linespacing=1.7)
        pdf.savefig(fig); fig.clear()
        _hgr_check(cancel)
        fig = page('Resultados em números',2,config['area_name']+' | Resumo dos índices selecionados')
        fig.text(.055,.805,'Comparação das médias na mesma área válida nas duas datas',size=12,weight='bold',color=navy)
        fmt = lambda value: ('%.4f' % value).replace('.',',')
        cells = []
        for name,r in zip(config['indices'],rows):
            delta = r['delta_mean']
            cells.append([name,fmt(r['mean_a']),fmt(r['mean_b']),
                          ('+' if delta>0 else '')+fmt(delta),fmt(r['delta_median']),
                          'Aumento' if delta>0 else 'Redução' if delta<0 else 'Sem variação'])
        ax = fig.add_axes([.055,.265,.89,.49]);ax.axis('off')
        table = ax.table(cellText=cells,colLabels=['Índice','Média\n'+dates[0],'Média\n'+dates[1],
                        'Diferença\nmédia (B − A)','Mediana da\ndiferença','Direção da\nmédia'],
                        colWidths=[.12,.18,.18,.18,.18,.16],cellLoc='center',bbox=[0,0,1,1])
        table.auto_set_font_size(False);table.set_fontsize(10)
        for (row,col),cell in table.get_celld().items():
            cell.set_edgecolor('#d4dfdc');cell.set_linewidth(.6)
            if row==0:
                cell.set_facecolor(navy);cell.get_text().set_color('white');cell.get_text().set_weight('bold')
            else:
                cell.set_facecolor('#edf2ee' if row%2 else '#ffffff');cell.get_text().set_color(navy)
                if col==0:cell.get_text().set_weight('bold')
        hectares=rows[0]['pixels']*grid['resolution']**2/10000
        fig.text(.055,.218,('Área válida comum: %.2f ha | %s pixels | Grade: %s m' %
                 (hectares,rows[0]['pixels'],grid['resolution'])).replace('%.2f',''),size=10,color=navy)
        fig.text(.055,.173,'Como ler a tabela',size=10,weight='bold',color=navy)
        fig.text(.055,.145,'A = primeira data; B = segunda. Diferença positiva indica aumento; negativa indica redução.\n'
                 'Os valores são adimensionais. A diferença é absoluta, não uma porcentagem de mudança.\n'
                 'A mediana resume a diferença pixel a pixel. A direção não indica, sozinha, melhora ou piora.',
                 size=9,color=muted,va='top',linespacing=1.6)
        fig.text(.055,.055,'Cada índice tem escala e significado próprios: compare suas datas, não a magnitude entre índices.',size=9,color=muted)
        pdf.savefig(fig);fig.clear()
        for i,name in enumerate(config['indices']):
            _hgr_check(cancel)
            r = rows[i]; a,b,diff = results[name]
            pt,en,meaning = HGR_INDEX_DETAILS[name]
            fig = page(name+' — '+pt,i+3,en+' | Índice adimensional')
            fig.text(.055,.825,'Comparação: %s e %s • Mesma área e escala nas duas datas' % tuple(dates),size=9,color=muted)
            cmap = 'RdYlGn' if name in ('NDVI','EVI','SAVI') else 'BrBG'
            lo,hi = -1,1
            if name == 'BAI':
                lo,hi = 0,max(1,float(np.nanpercentile(np.concatenate([a[np.isfinite(a)],b[np.isfinite(b)]]),98)))
                cmap = 'magma'
            delta = max(.1,float(np.nanpercentile(np.abs(diff[np.isfinite(diff)]),98)))
            for j,z in enumerate((a,b,diff)):
                im = map_axes(fig,[.055+j*.31,.415,.26,.355],z,
                              dates[j] if j<2 else 'Diferença',cmap if j<2 else 'BrBG',
                              (lo,hi) if j<2 else (-delta,delta))
                cb = fig.colorbar(im,cax=fig.add_axes([.07+j*.31,.335,.23,.014]),orientation='horizontal',extend='both')
                cb.ax.tick_params(labelsize=6)
            import textwrap
            from matplotlib.patches import FancyBboxPatch
            def legend_box(x,title,text,width=.43):
                fig.add_artist(FancyBboxPatch((x,.162),width,.137,boxstyle='round,pad=0.009',
                    transform=fig.transFigure,facecolor='#edf2ee',edgecolor='none',zorder=0))
                fig.text(x+.008,.278,title,size=8,weight='bold',color=navy)
                fig.text(x+.008,.256,textwrap.fill(text,68),size=9,color=navy,va='top',linespacing=1.4)
            legend_box(.055,'O QUE ESTE ÍNDICE MOSTRA',meaning)
            colors = ('Nas datas: verde = valores maiores; vermelho = menores. ' if name in ('NDVI','EVI','SAVI') else
                      'Nas datas: tons claros = valores maiores; escuros = menores. ' if name=='BAI' else
                      'Nas datas: verde-azulado = valores maiores; marrom = menores. ')
            legend_box(.515,'COMO LER OS MAPAS',colors+'Na diferença: verde = aumento; marrom = redução. Branco = fora da área ou sem dado comum. Aumento não significa necessariamente melhora.')
            fmt=lambda value: ('%.4f' % value).replace('.',',')
            change = 'um aumento' if r['delta_mean']>0 else 'uma redução' if r['delta_mean']<0 else 'uma variação nula'
            fig.text(.055,.125,'Nesta comparação',size=10,weight='bold',color=navy)
            fig.text(.055,.101,'A média passou de %s para %s: %s de %s unidade do índice.' %
                     (fmt(r['mean_a']),fmt(r['mean_b']),change,fmt(abs(r['delta_mean']))),size=10,color=navy)
            hectares=r['pixels']*grid['resolution']**2/10000
            fig.text(.055,.077,'Resumo da área válida nas duas datas: %.2f ha (%s pixels). Diferença = segunda data menos primeira.' %
                     (hectares,r['pixels']),size=8,color=muted)
            fig.text(.055,.05,'Fórmula: '+HGR_INDICES[name][2]+'   |   Barras: faixa de visualização; GeoTIFFs preservam os valores.',size=8,color=muted)
            pdf.savefig(fig);fig.clear()
        fig = page('Método, parâmetros e fontes',total,'Relatório exploratório; sem validação de campo ou atribuição causal')
        import textwrap
        notes = [
            'Área: %s. Geometria fornecida pelo usuário; não certifica limite cadastral.' % config['area_name'],
            'Datas-alvo: %s. Aquisições efetivas: %s.' % (' / '.join(config['targets']),' / '.join(dates)),
            'Bandas convertidas para reflectância com quantificação/offset do XML de cada cena. '
            'Grade comum UTM de %s m; bilinear nas bandas e vizinho mais próximo na SCL.' % grid['resolution'],
            'SCL mantida: 2,4,5,6,7. Excluídos NoData, defeitos, sombra de nuvem, nuvem e neve. '
            'Máscara espacial/numérica comum a A/B e a todos os índices selecionados.',
            'EVI: G=2,5; C1=6; C2=7,5; L=1. SAVI: L=%s. Zero válido é preservado; '
            'denominador com módulo <=1e-8 recebe NoData=-9999.' % config['savi_l'],
            'MNDWI=GREEN/SWIR1; NBR=NIR/SWIR2; UI=SWIR2/NIR normalizado. '
            'BAI usa RED/NIR e pontos 0,1/0,06. O gerador usa o motor corrigido, separado do laboratório legado.',
            'Chuva, fenologia, manejo e geometria de observação podem alterar a resposta. Não há ajuste BRDF '
            'ou teste de significância. Reamostrar SWIR a 10 m não cria detalhe de 10 m.',
            'NDBI=-NDMI nas definições adotadas: não são evidências independentes. NDSI é neve. '
            'NBR/BAI não comprovam ocorrência de fogo. Zero/SAVI/EVI não são classes universais de saúde.',
        ]
        y = .79
        for note in notes:
            text = '\n'.join(textwrap.wrap(note,125))
            fig.text(.055,y,text,size=8,color=muted,va='top',linespacing=1.4)
            y -= .027*(text.count('\n')+1)+.014
        y -= .012
        for label,url in HGR_SOURCES:
            fig.text(.055,y,label,size=7,color=navy,weight='bold')
            fig.text(.055,y-.019,url,size=6,color=muted,url=url)
            y -= .047
        pdf.savefig(fig);fig.clear()


def _hgr_run_report(config, progress=lambda s: None, cancel=None, local_assets=None):
    import numpy as np
    from osgeo import gdal, ogr
    _hgr_check(cancel)
    if not config.get('indices') or any(x not in HGR_INDICES for x in config['indices']):
        raise ValueError('Selecione pelo menos um índice válido.')
    if len(config.get('groups', [])) != 2:
        raise ValueError('Selecione as duas aquisições antes de gerar.')
    dates = [x['date'] for x in config['groups']]
    if dates[0] >= dates[1]:
        raise ValueError('A segunda aquisição deve ser posterior à primeira.')
    grid = _hgr_grid(config['geometry'], config['resolution'])
    config = dict(config)
    name = _hgr_re.sub(r'[^\w-]+','_',config['area_name']).strip('_')[:40] or 'area'
    folder = _HgrPath(config['output']) / ('HGEO_%s_%s_%s' %
             (name,_HgrDateTime.now().strftime('%Y%m%d_%H%M%S'),_hgr_uuid.uuid4().hex[:6]))
    folder.mkdir(parents=True,exist_ok=False)
    (folder/'pedido.json').write_text(_hgr_json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    temp_pdf = folder/'relatorio.em_processamento.pdf'
    try:
        progress('Preparando área e grade de análise...')
        mask_ds = gdal.GetDriverByName('MEM').Create('',grid['width'],grid['height'],1,gdal.GDT_Byte)
        r = grid['resolution']; x0,y0,x1,y1 = grid['bounds']
        mask_ds.SetGeoTransform((x0,r,0,y1,0,-r)); mask_ds.SetProjection(grid['wkt'])
        boundary = folder/'area.geojson'
        boundary.write_text(_hgr_json.dumps({'type':'FeatureCollection','features':[
            {'type':'Feature','properties':{},'geometry':config['geometry']}]}),encoding='utf-8')
        vec = ogr.Open(str(boundary)); gdal.RasterizeLayer(mask_ds,[1],vec.GetLayer(),burn_values=[1])
        area_mask = mask_ds.ReadAsArray().astype(bool)
        if not area_mask.any():
            raise ValueError('Área menor que a grade escolhida. Reduza o tamanho do pixel.')
        config['area_pixels'] = int(area_mask.sum())
        tokens = {}; arrays = []; provenance = []
        # Per-thread HTTP configuration avoids changing the user's GDAL settings.
        previous = {}
        for key,value in [('GDAL_HTTP_TIMEOUT','40'),('GDAL_HTTP_CONNECTTIMEOUT','15'),
                          ('GDAL_DISABLE_READDIR_ON_OPEN','EMPTY_DIR')]:
            previous[key] = gdal.GetThreadLocalConfigOption(key)
            gdal.SetThreadLocalConfigOption(key,value)
        try:
            for i,group in enumerate(config['groups']):
                date_folder = folder/('dados_'+group['date']); date_folder.mkdir()
                data,meta = _hgr_prepare_date(group,config,grid,date_folder,tokens,cancel,progress,local_assets)
                arrays.append(data); provenance.append(meta)
        finally:
            for key,value in previous.items():
                gdal.SetThreadLocalConfigOption(key,value)
        common = area_mask & arrays[0]['quality'] & arrays[1]['quality']
        computed = {}
        for name in config['indices']:
            _hgr_check(cancel); progress('Calculando %s...' % name)
            pair = [_hgr_compute_index(name,data,config['savi_l']) for data in arrays]
            computed[name] = pair
            common &= np.isfinite(pair[0]) & np.isfinite(pair[1])
        # Also require valid visible bands used by the overview.
        for data in arrays:
            for key in ('RED','GREEN','BLUE'):
                common &= np.isfinite(data[key])
        fraction = 100 * common.sum()/area_mask.sum()
        if not common.any() or fraction < config['min_common_pct']:
            raise ValueError('Área válida em comum: %.1f%%; mínimo solicitado: %s%%. '
                             'Escolha cenas melhores ou revise o mínimo.' % (fraction,config['min_common_pct']))
        results = {}; rows = []
        for name,pair in computed.items():
            a,b = [np.where(common,z,np.nan) for z in pair]
            diff = b-a; results[name] = (a,b,diff)
            for suffix,data in [('A',a),('B',b),('DIFERENCA',diff)]:
                _hgr_write_raster(folder/('%s_%s.tif' % (name,suffix)),data,grid)
            rows.append({'index':name,'date_a':dates[0],'date_b':dates[1],
                'mean_a':float(np.nanmean(a)),'mean_b':float(np.nanmean(b)),
                'delta_mean':float(np.nanmean(diff)),'delta_median':float(np.nanmedian(diff)),
                'pixels':int(common.sum()),'valid_common_pct':float(fraction)})
        with (folder/'estatisticas.csv').open('w',newline='',encoding='utf-8-sig') as f:
            writer = _hgr_csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
        (folder/'proveniencia.json').write_text(_hgr_json.dumps({'scenes':provenance,'grid':grid,
            'statistics':rows,'sources':HGR_SOURCES,'engine_version':'1.0'},ensure_ascii=False,indent=2),encoding='utf-8')
        progress('Montando mapas, diferenças e metodologia no PDF...')
        _hgr_pdf(temp_pdf,config,grid,arrays,results,common,rows,cancel)
        _hgr_check(cancel)
        pdf = folder/'Relatorio_Comparativo_HGEO.pdf'
        _hgr_os.replace(str(temp_pdf),str(pdf))
        (folder/'CONCLUIDO.txt').write_text('Relatório concluído. Consulte o PDF e a proveniência.\n',encoding='utf-8')
        return str(pdf)
    except Exception as exc:
        if temp_pdf.exists():
            temp_pdf.unlink()
        (folder/'NAO_CONCLUIDO.txt').write_text(str(exc),encoding='utf-8')
        raise


def _hgr_scene_preview(config, cancel=None, progress=lambda s: None,
                       local_assets=None, cache_root=None):
    """RGB + SCL preview of the AOI, with stable contrast and a bounded grid."""
    import numpy as np
    from osgeo import gdal, ogr, osr
    from PIL import Image, ImageDraw
    _hgr_check(cancel)
    group = config['preview_group']
    key_data = {'version': 1, 'geometry': config['geometry'], 'date': group['date'],
                'ids': sorted(f['id'] for f in group['features'])}
    key = _hgr_hashlib.sha256(_hgr_json.dumps(key_data,sort_keys=True).encode('utf-8')).hexdigest()
    cache = _HgrPath(cache_root or (_HgrPath(_hgr_tempfile.gettempdir())/'hgeo_previews_v1'))
    cache.mkdir(parents=True,exist_ok=True)
    cache_file = cache/(key+'.json')
    if cache_file.exists():
        try:
            result = _hgr_json.loads(cache_file.read_text(encoding='utf-8'))
            if all(_HgrPath(result[k]).is_file() for k in ('image','quality_image')):
                _hgr_check(cancel)
                result['cached'] = True
                return result
        except (OSError, ValueError, KeyError):
            pass
    grid = _hgr_grid(config['geometry'],20)
    span = max(grid['bounds'][2]-grid['bounds'][0],grid['bounds'][3]-grid['bounds'][1])
    resolution = max(20,int(_hgr_math.ceil(span/640/10))*10)
    grid = _hgr_grid(config['geometry'],resolution)
    folder = _HgrPath(_hgr_tempfile.mkdtemp(prefix=key[:12]+'_',dir=str(cache)))
    previous = {}
    for setting,value in [('GDAL_HTTP_TIMEOUT','40'),('GDAL_HTTP_CONNECTTIMEOUT','15'),
                          ('GDAL_DISABLE_READDIR_ON_OPEN','EMPTY_DIR')]:
        previous[setting]=gdal.GetThreadLocalConfigOption(setting)
        gdal.SetThreadLocalConfigOption(setting,value)
    try:
        # RGB alone is enough to choose an image; full report checks all index bands.
        data,provenance=_hgr_prepare_date(group,dict(config,indices=[]),grid,folder,{},
                                          cancel,progress,local_assets)
    finally:
        for setting,value in previous.items():gdal.SetThreadLocalConfigOption(setting,value)
    _hgr_check(cancel)
    feature_collection={'type':'FeatureCollection','features':[
        {'type':'Feature','properties':{},'geometry':config['geometry']}]}
    boundary=folder/'area.geojson'
    boundary.write_text(_hgr_json.dumps(feature_collection),encoding='utf-8')
    vector=ogr.Open(str(boundary))
    mask=gdal.GetDriverByName('MEM').Create('',grid['width'],grid['height'],1,gdal.GDT_Byte)
    x0,y0,x1,y1=grid['bounds'];r=grid['resolution']
    mask.SetGeoTransform((x0,r,0,y1,0,-r));mask.SetProjection(grid['wkt'])
    gdal.RasterizeLayer(mask,[1],vector.GetLayer(),burn_values=[1])
    inside=mask.ReadAsArray().astype(bool)
    if not inside.any():raise ValueError('Área pequena demais para a prévia.')
    rgb=np.stack([data[k] for k in ('RED','GREEN','BLUE')],axis=-1)
    finite=np.all(np.isfinite(rgb),axis=-1)
    valid=inside & finite & data['quality']
    cloud_shadow=inside & np.isin(data['SCL'],[3,8,9,10])
    nodata=inside & (~finite | ~np.isfinite(data['SCL']))
    image=(np.clip(np.nan_to_num(rgb,nan=0)/.32,0,1)**(1/1.8)*255).astype('uint8')
    image[~finite]=[235,239,241]
    highlighted=image.copy()
    excluded=inside & ~valid
    highlighted[excluded]=(highlighted[excluded]*.4+np.array([236,63,130])*.6).astype('uint8')
    geom=ogr.CreateGeometryFromJson(_hgr_json.dumps(config['geometry']))
    src=osr.SpatialReference();src.ImportFromEPSG(4326)
    dst=osr.SpatialReference();dst.ImportFromEPSG(grid['epsg'])
    for s in (src,dst):s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    geom.Transform(osr.CoordinateTransformation(src,dst))
    rings=[]
    def collect(g):
        if g.GetGeometryName()=='LINEARRING':
            rings.append([((p[0]-x0)/r,(y1-p[1])/r) for p in g.GetPoints()])
        else:
            for i in range(g.GetGeometryCount()):collect(g.GetGeometryRef(i))
    collect(geom)
    for name,arr in [('rgb.png',image),('qualidade.png',highlighted)]:
        im=Image.fromarray(arr);draw=ImageDraw.Draw(im)
        for ring in rings:
            draw.line(ring,fill=(255,255,255),width=3)
            draw.line(ring,fill=(17,74,84),width=1)
        im.save(folder/name)
    result={'key':key,'date':group['date'],'image':str(folder/'rgb.png'),
        'quality_image':str(folder/'qualidade.png'),'valid_pct':100*valid.sum()/inside.sum(),
        'cloud_shadow_pct':100*cloud_shadow.sum()/inside.sum(),
        'nodata_pct':100*nodata.sum()/inside.sum(),'resolution':resolution,
        'bounds':grid['bounds'],'epsg':grid['epsg'],'pixels':int(inside.sum()),
        'scene_cloud_pct':group['cloud'],'scene_ids':[x['id'] for x in provenance],
        'width':grid['width'],'height':grid['height'],'cached':False}
    # Convert NumPy scalar metadata before serializing.
    for field in ('valid_pct','cloud_shadow_pct','nodata_pct'):result[field]=float(result[field])
    _hgr_check(cancel)
    staged=cache/(key+'.'+_hgr_uuid.uuid4().hex+'.tmp')
    staged.write_text(_hgr_json.dumps(result,ensure_ascii=False),encoding='utf-8')
    _hgr_os.replace(str(staged),str(cache_file))
    return result


class HGeoPreviewWorker(QThread):
    progresso=pyqtSignal(str)
    pronto=pyqtSignal(object)
    falhou=pyqtSignal(str)

    def __init__(self,config,parent=None):
        super().__init__(parent);self.config=config

    def run(self):
        try:
            _hgr_ensure_gui_logging()
            self.pronto.emit(_hgr_scene_preview(self.config,self.isInterruptionRequested,self.progresso.emit))
        except Exception as exc:self.falhou.emit(str(exc))


class HGeoPreviewLabel(QLabel):
    clicked=pyqtSignal()

    def __init__(self,parent=None):
        super().__init__(parent);self._original=None
        self.setAlignment(Qt.AlignCenter);self.setMinimumSize(250,200)
        self.setMaximumHeight(280);self.setWordWrap(True)
        self.setStyleSheet('background:#E9F0F2;border:1px solid #C6D6DA;border-radius:5px;color:#526B70;')
        self.setCursor(Qt.PointingHandCursor)
        self.clearImage('Busque as imagens para ver a prévia da sua área.')

    def sizeHint(self):return QSize(300,240)

    def clearImage(self,message):
        self._original=None;self.setPixmap(QPixmap());self.setText(message)

    def setImage(self,path):
        pixmap=QPixmap(path)
        if pixmap.isNull():raise ValueError('Não foi possível abrir a prévia da imagem.')
        self._original=pixmap;self.setText('');self._fit()

    def _fit(self):
        if self._original is not None:
            self.setPixmap(self._original.scaled(self.size()-QSize(8,8),Qt.KeepAspectRatio,Qt.SmoothTransformation))

    def resizeEvent(self,event):
        super().resizeEvent(event);self._fit()

    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton and self._original is not None:self.clicked.emit()
        super().mousePressEvent(event)


class HGeoPreviewZoomView(QGraphicsView):
    def __init__(self,scene,parent=None):
        super().__init__(scene,parent);self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QColor('#E9F0F2'))

    def wheelEvent(self,event):
        factor=1.2 if event.angleDelta().y()>0 else 1/1.2
        scale=self.transform().m11()
        if .1 <= scale*factor <= 40:self.scale(factor,factor)
        event.accept()


class HGeoPreviewZoomDialog(QDialog):
    def __init__(self,path,title,parent=None):
        super().__init__(parent);self.setWindowTitle(title);self.resize(1000,780)
        layout=QVBoxLayout(self);scene=QGraphicsScene(self);pixmap=QPixmap(path)
        if pixmap.isNull():raise ValueError('Prévia indisponível.')
        scene.addPixmap(pixmap);self.view=HGeoPreviewZoomView(scene,self)
        layout.addWidget(QLabel('Role para ampliar/reduzir; arraste para mover. Contorno: área escolhida. Norte para cima.'))
        layout.addWidget(self.view,1);buttons=QHBoxLayout()
        for text,callback in [('−',lambda:self.view.scale(1/1.25,1/1.25)),
                              ('+',lambda:self.view.scale(1.25,1.25)),
                              ('Ajustar à janela',self.fit)]:
            button=QPushButton(text);button.clicked.connect(callback);buttons.addWidget(button)
        close=QPushButton('Fechar');close.clicked.connect(self.accept);buttons.addWidget(close)
        layout.addLayout(buttons);QTimer.singleShot(0,self.fit)

    def fit(self):self.view.fitInView(self.view.sceneRect(),Qt.KeepAspectRatio)


class HGeoRelatorioWorker(QThread):
    progresso = pyqtSignal(str)
    pronto = pyqtSignal(object)
    falhou = pyqtSignal(str)

    def __init__(self, operation, config, parent=None):
        super().__init__(parent)
        self.operation = operation; self.config = config

    def run(self):
        try:
            _hgr_ensure_gui_logging()
            if self.operation == 'catalog':
                result = _hgr_catalog(self.config,self.isInterruptionRequested,self.progresso.emit)
            else:
                result = _hgr_run_report(self.config,self.progresso.emit,self.isInterruptionRequested)
            self.pronto.emit(result)
        except Exception as exc:
            self.falhou.emit(str(exc))


class HGeoRelatorioDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('H-GEO | Relatório comparativo')
        self.resize(940,900)
        self.setStyleSheet('QDialog {background:#F8FAFC;} QLabel,QCheckBox {color:#244653;} '
            'QPushButton {padding:9px;} QGroupBox {font-weight:bold; margin-top:10px; padding-top:15px;}')
        self.worker = None; self._catalog_config = None; self._groups = None; self._pdf = None
        self._main_busy=False;self._closing=False
        self._preview_workers={};self._preview_pending=[False,False]
        self._preview_versions=[0,0];self._preview_info=[None,None]
        self._preview_cancelled=[False,False]
        layout = QVBoxLayout(self)
        intro = QLabel('Compare duas aquisições Sentinel-2: área, datas e índices.\n'
            'As imagens disponíveis aparecem antes da geração. PDF + GeoTIFFs + estatísticas.')
        intro.setWordWrap(True);layout.addWidget(intro)
        scroll = QScrollArea();scroll.setWidgetResizable(True);self._scroll=scroll
        contents = QWidget();content_layout = QVBoxLayout(contents)
        scroll.setWidget(contents);layout.addWidget(scroll,1)
        inputs = QGroupBox('1. Área e busca de imagens');form = QFormLayout(inputs)
        self.area = QgsMapLayerComboBox();self.area.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        form.addRow('Camada de polígonos:',self.area)
        self.selected_only = QCheckBox('Usar somente as feições selecionadas')
        form.addRow('',self.selected_only)
        def date_edit(day):
            control = QDateEdit(day);control.setCalendarPopup(True);control.setDisplayFormat('dd/MM/yyyy')
            control.setMaximumDate(QDate.currentDate());return control
        self.date_a = date_edit(QDate.currentDate().addYears(-1))
        self.date_b = date_edit(QDate.currentDate())
        form.addRow('Data-alvo inicial:',self.date_a);form.addRow('Data-alvo final:',self.date_b)
        self.window = QSpinBox();self.window.setRange(0,30);self.window.setValue(7);self.window.setSuffix(' dias')
        form.addRow('Janela de busca (±):',self.window)
        self.cloud = QSpinBox();self.cloud.setRange(0,100);self.cloud.setValue(20);self.cloud.setSuffix('%')
        form.addRow('Nuvens na cena, até:',self.cloud)
        self.search = QPushButton('Buscar imagens disponíveis');form.addRow('',self.search)
        content_layout.addWidget(inputs)
        self.scene_a = QComboBox();self.scene_b = QComboBox()
        self.preview_group=QGroupBox('2. Compare as imagens antes de escolher')
        previews=QVBoxLayout(self.preview_group)
        caption=QLabel('Prévias em cor natural, com o mesmo recorte e contraste. Troque a data e clique na imagem para ampliar.')
        caption.setWordWrap(True);previews.addWidget(caption)
        cards=QHBoxLayout();self.preview_labels=[];self.preview_meta=[]
        self.preview_zoom=[];self.preview_retry=[];self.preview_navigation=[]
        for side,combo in enumerate((self.scene_a,self.scene_b)):
            card=QGroupBox('Imagem A - inicial' if side==0 else 'Imagem B - final')
            column=QVBoxLayout(card);column.addWidget(combo)
            navigation=QHBoxLayout();previous=QPushButton('‹ Anterior');following=QPushButton('Próxima ›')
            previous.clicked.connect(lambda checked=False,s=side:self._step_scene(s,-1))
            following.clicked.connect(lambda checked=False,s=side:self._step_scene(s,1))
            navigation.addWidget(previous);navigation.addWidget(following);column.addLayout(navigation)
            label=HGeoPreviewLabel();column.addWidget(label,1)
            meta=QLabel('Nuvens da cena e qualidade na área aparecerão aqui.');meta.setWordWrap(True)
            meta.setToolTip('Qualidade local estimada por RGB + classificação SCL automática. '
                            'O relatório verifica também as demais bandas dos índices.')
            column.addWidget(meta);actions=QHBoxLayout()
            zoom=QPushButton('Ampliar');zoom.setEnabled(False)
            retry=QPushButton('Atualizar prévia');retry.setEnabled(False)
            zoom.clicked.connect(lambda checked=False,s=side:self._zoom_preview(s))
            label.clicked.connect(lambda s=side:self._zoom_preview(s))
            retry.clicked.connect(lambda checked=False,s=side:self._queue_preview(s))
            actions.addWidget(zoom);actions.addWidget(retry);column.addLayout(actions)
            cards.addWidget(card,1);self.preview_labels.append(label);self.preview_meta.append(meta)
            self.preview_zoom.append(zoom);self.preview_retry.append(retry)
            self.preview_navigation.append((previous,following))
        previews.addLayout(cards)
        self.highlight_quality=QCheckBox('Destacar em rosa os pixels excluídos pela máscara de qualidade')
        self.highlight_quality.toggled.connect(self._refresh_preview_images)
        previews.addWidget(self.highlight_quality)
        content_layout.addWidget(self.preview_group)
        indices = QGroupBox('3. Índices');grid = QGridLayout(indices);self.checks = {}
        for i,(key,spec) in enumerate(HGR_INDICES.items()):
            check = QCheckBox(key+' - '+spec[0]);check.setChecked(key in ('NDVI','EVI','NDMI','MNDWI','NDBI'))
            grid.addWidget(check,i//2,i%2);self.checks[key]=check
        content_layout.addWidget(indices)
        options = QGroupBox('4. Saída e parâmetros');form2 = QFormLayout(options)
        self.resolution = QComboBox()
        for label,value in [('20 m (padrão)',20),('10 m (SWIR reamostrado)',10),('30 m',30),('60 m',60)]:
            self.resolution.addItem(label,value)
        form2.addRow('Grade comum:',self.resolution)
        self.savi_l = QDoubleSpinBox();self.savi_l.setRange(0,1);self.savi_l.setDecimals(2);self.savi_l.setSingleStep(.05);self.savi_l.setValue(.5)
        form2.addRow('SAVI - fator L:',self.savi_l)
        self.min_valid = QSpinBox();self.min_valid.setRange(1,100);self.min_valid.setValue(70);self.min_valid.setSuffix('%')
        form2.addRow('Área válida comum mínima:',self.min_valid)
        path_row = QHBoxLayout();self.output = QLineEdit();self.browse = QPushButton('Escolher pasta')
        path_row.addWidget(self.output);path_row.addWidget(self.browse);form2.addRow('Salvar em:',path_row)
        content_layout.addWidget(options)
        self.status = QLabel('Escolha a área e consulte as imagens.');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.log = QTextEdit();self.log.setReadOnly(True);self.log.setMaximumHeight(100);layout.addWidget(self.log)
        buttons = QHBoxLayout();self.generate = QPushButton('Gerar relatório PDF');self.generate.setEnabled(False)
        self.generate.setStyleSheet('background:#0D9488;color:white;font-weight:bold;')
        self.cancel = QPushButton('Cancelar operação');self.cancel.setEnabled(False)
        self.open_pdf = QPushButton('Abrir PDF');self.open_pdf.setEnabled(False)
        for button in (self.generate,self.cancel,self.open_pdf):buttons.addWidget(button)
        layout.addLayout(buttons)
        self._editable = [inputs,self.preview_group,indices,options]
        self.search.clicked.connect(self._search)
        self.generate.clicked.connect(self._generate)
        self.browse.clicked.connect(self._browse)
        self.cancel.clicked.connect(self._cancel)
        self.open_pdf.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(self._pdf)))
        self.area.layerChanged.connect(self._area_changed)
        for control in (self.date_a,self.date_b):control.dateChanged.connect(self._invalidate)
        self.window.valueChanged.connect(self._invalidate);self.cloud.valueChanged.connect(self._invalidate)
        self.selected_only.toggled.connect(self._invalidate)
        self.scene_a.currentIndexChanged.connect(lambda index:self._queue_preview(0))
        self.scene_b.currentIndexChanged.connect(lambda index:self._queue_preview(1))
        self._area_changed()

    def _area_changed(self,*args):
        layer = self.area.currentLayer()
        has_selection = bool(layer and layer.selectedFeatureCount())
        self.selected_only.setEnabled(has_selection);self.selected_only.setChecked(has_selection)
        self._invalidate()

    def _invalidate(self,*args):
        self._groups = None;self._catalog_config = None
        for combo in (self.scene_a,self.scene_b):
            combo.blockSignals(True);combo.clear();combo.blockSignals(False)
        for side in (0,1):
            self._preview_versions[side]+=1;self._preview_info[side]=None
            self._preview_pending[side]=False;self._preview_cancelled[side]=False
            worker=self._preview_workers.get(side)
            if worker and worker.isRunning():worker.requestInterruption()
            self.preview_labels[side].clearImage('Busque as imagens para ver a prévia da sua área.')
            self.preview_meta[side].setText('');self.preview_zoom[side].setEnabled(False)
            self.preview_retry[side].setEnabled(False)
            for button in self.preview_navigation[side]:button.setEnabled(False)
        self.generate.setEnabled(False);self.open_pdf.setEnabled(False)

    def _snapshot(self):
        layer = self.area.currentLayer()
        if not layer or not layer.crs().isValid():
            raise ValueError('Selecione uma camada de polígonos com CRS válido.')
        if self.date_a.date() >= self.date_b.date():
            raise ValueError('A data final deve ser posterior à inicial.')
        features = layer.getSelectedFeatures() if self.selected_only.isChecked() else layer.getFeatures()
        geoms = []
        for feature in features:
            geom = QgsGeometry(feature.geometry())
            if geom.isNull() or geom.isEmpty() or not geom.isGeosValid():
                raise ValueError('A área contém geometria vazia ou inválida. Corrija a camada.')
            geoms.append(geom)
        if not geoms:
            raise ValueError('A área está vazia. Selecione polígonos para comparar.')
        area = QgsGeometry.unaryUnion(geoms)
        area.transform(QgsCoordinateTransform(layer.crs(),QgsCoordinateReferenceSystem('EPSG:4326'),
                                               QgsProject.instance().transformContext()))
        geometry = _hgr_json.loads(area.asJson(15))
        _hgr_grid(geometry,self.resolution.currentData())
        return {'area_name':layer.name(),'geometry':geometry,
            'targets':[self.date_a.date().toString('yyyy-MM-dd'),self.date_b.date().toString('yyyy-MM-dd')],
            'window_days':self.window.value(),'scene_cloud_pct':self.cloud.value()}

    def _busy(self,on):
        self._main_busy=on
        for widget in self._editable:widget.setEnabled(not on)
        if not on and self._groups and not self._closing:
            for side in (0,1):
                if self._preview_info[side] is None and not self._preview_cancelled[side]:
                    if side in self._preview_workers:self._preview_pending[side]=True
                    else:self._queue_preview(side)
        self._refresh_buttons();self._finish_close_if_idle()

    def _refresh_buttons(self):
        active=bool(self._preview_workers)
        ready=bool(self._groups) and all(info is not None for info in self._preview_info)
        self.generate.setEnabled(not self._main_busy and not active and ready and not self._closing)
        self.cancel.setEnabled(self._main_busy or active)

    def _step_scene(self,side,step):
        combo=(self.scene_a,self.scene_b)[side]
        index=combo.currentIndex()+step
        if 0<=index<combo.count():combo.setCurrentIndex(index)

    def _queue_preview(self,side):
        if not self._groups or self._closing:return
        combo=(self.scene_a,self.scene_b)[side];index=combo.currentIndex()
        if index<0:return
        self._preview_versions[side]+=1;version=self._preview_versions[side]
        self._preview_info[side]=None;self._preview_cancelled[side]=False
        self.open_pdf.setEnabled(False);self.preview_zoom[side].setEnabled(False)
        group=self._groups[side][index]
        self.preview_labels[side].clearImage('Carregando a prévia de %s...' % group['date'])
        self.preview_meta[side].setText('Nuvens na cena inteira: %.1f%%. Verificando a sua área...' % group['cloud'])
        self.preview_retry[side].setEnabled(False)
        previous,following=self.preview_navigation[side]
        previous.setEnabled(index>0);following.setEnabled(index<combo.count()-1)
        old=self._preview_workers.get(side)
        if old is not None:
            self._preview_pending[side]=True
            if old.isRunning():old.requestInterruption()
            self._refresh_buttons();return
        if self._main_busy:
            self._preview_pending[side]=True;self._refresh_buttons();return
        self._preview_pending[side]=False
        config=dict(self._catalog_config,preview_group=group)
        worker=HGeoPreviewWorker(config,self);self._preview_workers[side]=worker
        worker.pronto.connect(lambda result,s=side,v=version:self._preview_done(s,v,result))
        worker.falhou.connect(lambda message,s=side,v=version:self._preview_failed(s,v,message))
        worker.finished.connect(lambda s=side,w=worker:self._preview_finished(s,w))
        worker.start();self._refresh_buttons()

    def _preview_done(self,side,version,info):
        if version!=self._preview_versions[side] or not self._groups or self._closing:return
        self._preview_info[side]=info
        self._refresh_preview_images()
        text='Válida na área: %.1f%% | nuvem/sombra: %.1f%%\nCena inteira: %.1f%% de nuvens | prévia %s m, norte ↑' % (
            info['valid_pct'],info['cloud_shadow_pct'],info['scene_cloud_pct'],info['resolution'])
        self.preview_meta[side].setText(text)
        self.preview_meta[side].setStyleSheet('color:#96601D;' if info['valid_pct']<self.min_valid.value() else 'color:#245E52;')
        self.preview_zoom[side].setEnabled(True);self.preview_retry[side].setEnabled(True)
        if all(x is not None for x in self._preview_info):
            self.status.setText('Compare as duas prévias. Confirme as datas e escolha os índices para gerar o relatório.')

    def _preview_failed(self,side,version,message):
        if version!=self._preview_versions[side] or self._closing:return
        self._preview_info[side]=None;self._preview_cancelled[side]=True
        self.preview_labels[side].clearImage('Prévia indisponível. Use “Atualizar prévia” para tentar novamente.')
        self.preview_meta[side].setText(message);self.preview_meta[side].setStyleSheet('color:#A14F32;')
        self.preview_retry[side].setEnabled(True)

    def _preview_finished(self,side,worker):
        if self._preview_workers.get(side) is worker:self._preview_workers.pop(side)
        worker.deleteLater()
        if self._preview_pending[side] and self._groups and not self._closing:
            self._preview_pending[side]=False
            QTimer.singleShot(0,lambda s=side:self._queue_preview(s))
        self._refresh_buttons();self._finish_close_if_idle()

    def _refresh_preview_images(self,*args):
        for side,info in enumerate(self._preview_info):
            if info is not None:
                key='quality_image' if self.highlight_quality.isChecked() else 'image'
                self.preview_labels[side].setImage(info[key])

    def _zoom_preview(self,side):
        info=self._preview_info[side]
        if info is None:return
        key='quality_image' if self.highlight_quality.isChecked() else 'image'
        dialog=HGeoPreviewZoomDialog(info[key],'H-GEO | Prévia '+info['date'],self)
        dialog.exec_()

    def _finish_close_if_idle(self):
        if self._closing and not self._main_busy and not self._preview_workers:
            QTimer.singleShot(0,self.close)

    def _start(self,operation,config):
        self._busy(True)
        self.worker = HGeoRelatorioWorker(operation,config,self)
        self.worker.progresso.connect(self._progress)
        self.worker.pronto.connect(self._catalog_done if operation=='catalog' else self._report_done)
        self.worker.falhou.connect(self._failed)
        # Re-enable the dialog only after QThread has actually stopped.
        self.worker.finished.connect(lambda:self._busy(False))
        self.worker.start()

    def _progress(self,text):
        self.status.setText(text);self.log.append(text)

    def _search(self):
        try:
            config=self._snapshot();self._invalidate();self._catalog_config=config
            self._start('catalog',self._catalog_config)
        except Exception as exc:self._failed(str(exc))

    def _catalog_done(self,options):
        self._groups=options
        for combo,groups in zip((self.scene_a,self.scene_b),options):
            combo.blockSignals(True)
            combo.clear()
            for group in groups:
                combo.addItem('%s | nuvens %.1f%% | %s cena(s)' %
                              (group['date'],group['cloud'],len(group['features'])))
            combo.blockSignals(False)
        self._preview_info=[None,None];self._preview_cancelled=[False,False]
        self._preview_pending=[True,True]
        self._progress('Carregando as prévias das duas aquisições sobre a sua área...')
        QTimer.singleShot(0,lambda:self._scroll.ensureWidgetVisible(self.preview_group))

    def _browse(self):
        folder=QFileDialog.getExistingDirectory(self,'Pasta dos relatórios',self.output.text())
        if folder:self.output.setText(folder)

    def _generate(self):
        try:
            if not self._groups:raise ValueError('Busque as imagens primeiro.')
            if self._preview_workers or not all(x is not None for x in self._preview_info):
                raise ValueError('Aguarde o carregamento das duas prévias antes de gerar.')
            current=self._snapshot()
            if current!=self._catalog_config:
                self._invalidate();raise ValueError('A área ou a busca mudou. Consulte as imagens novamente.')
            selected=[k for k,v in self.checks.items() if v.isChecked()]
            if not selected:raise ValueError('Selecione pelo menos um índice.')
            if not self.output.text().strip():raise ValueError('Escolha a pasta de saída.')
            groups=[self._groups[i][combo.currentIndex()] for i,combo in enumerate((self.scene_a,self.scene_b))]
            if groups[0]['date']>=groups[1]['date']:
                raise ValueError('Escolha aquisições diferentes, com a inicial anterior à final.')
            config=dict(current,groups=groups,indices=selected,output=self.output.text().strip(),
                resolution=self.resolution.currentData(),savi_l=self.savi_l.value(),
                min_common_pct=self.min_valid.value())
            self.open_pdf.setEnabled(False);self._start('report',config)
        except Exception as exc:self._failed(str(exc))

    def _report_done(self,pdf):
        self._pdf=pdf;self.open_pdf.setEnabled(True)
        self._progress('Concluído: '+pdf)

    def _failed(self,message):
        self._progress('Atenção: '+message)

    def _cancel(self):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption();self.cancel.setEnabled(False)
            self.status.setText('Cancelando após a etapa de leitura em andamento...')
        for side,worker in list(self._preview_workers.items()):
            self._preview_versions[side]+=1;self._preview_pending[side]=False
            self._preview_info[side]=None;self._preview_cancelled[side]=True
            worker.requestInterruption()
            self.preview_labels[side].clearImage('Prévia cancelada. Use “Atualizar prévia” para retomar.')
            self.preview_retry[side].setEnabled(True)

    def closeEvent(self,event):
        if self._main_busy or self._preview_workers:
            self._closing=True;self._cancel();event.ignore()
        else:event.accept()

    def reject(self):self.close()


def abrir_relatorio_comparativo_hgeo(parent=None):
    global _hgr_report_dialog
    current = globals().get('_hgr_report_dialog')
    if current is not None:
        try:
            if isinstance(current,HGeoRelatorioDialog):
                current.show();current.raise_();current.activateWindow();return current
            # Re-running an updated script must not reopen an obsolete dialog.
            if not current.close():return current
            current.deleteLater()
        except RuntimeError:
            pass
    _hgr_report_dialog = HGeoRelatorioDialog(parent)
    _hgr_report_dialog.show()
    return _hgr_report_dialog
# === FIM H-GEO: RELATORIO COMPARATIVO ===








class ChatWindow(QDialog):
    def __init__(self, parent=None):
        super(ChatWindow, self).__init__(parent)
        global _modo_atual
        
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(460, 680)
        
        layout_invisivel = QVBoxLayout(self)
        layout_invisivel.setContentsMargins(15, 15, 15, 15)
        
        self.container = QWidget()
        self.container.setStyleSheet("""
            QWidget#chat_container {
                background-color: #F8FAFC; 
                border-radius: 12px;
                border: 1px solid #CFD8DC;
            }
        """)
        self.container.setObjectName("chat_container")
        aplicar_sombra(self.container, blur_radius=25, alpha=50, y_offset=5)
        layout_invisivel.addWidget(self.container)

        self.layout_principal = QVBoxLayout(self.container)
        self.layout_principal.setContentsMargins(25, 20, 25, 25)
        
        # --- BARRA SUPERIOR CUSTOMIZADA ---
        self.top_bar_layout = QHBoxLayout()
        
        titulo_texto = "Terminal Analítico" if _modo_atual == "analista" else "Copiloto Básico"
        self.lbl_title = QLabel(f"<b>H-Geo |</b> {titulo_texto}")
        self.lbl_title.setStyleSheet(f"font-size: 15px; color: {COR_PRIMARIA}; font-family: 'Segoe UI';")
        
        self.btn_voltar_menu = QPushButton("Trocar Perfil")
        self.btn_voltar_menu.setCursor(Qt.PointingHandCursor)
        self.btn_voltar_menu.setStyleSheet("QPushButton { background-color: #E2E8F0; color: #4A5568; font-weight: bold; padding: 6px 12px; border-radius: 6px; border: none; font-size: 12px; } QPushButton:hover { background-color: #CBD5E0; }")
        self.btn_voltar_menu.clicked.connect(self.abrir_menu_selecao)

        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(28, 28)
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.setStyleSheet("QPushButton { background-color: transparent; color: #94A3B8; font-weight: bold; font-size: 16px; border: none; border-radius: 14px; } QPushButton:hover { color: #E53E3E; background-color: #FEE2E2; }")
        self.btn_close.clicked.connect(self.hide)

        self.top_bar_layout.addWidget(self.lbl_title)
        self.top_bar_layout.addStretch()
        self.top_bar_layout.addWidget(self.btn_voltar_menu)
        self.top_bar_layout.addWidget(self.btn_close)
        
        self.layout_principal.addLayout(self.top_bar_layout)

        linha = QWidget()
        linha.setFixedHeight(1)
        linha.setStyleSheet("background-color: #E2E8F0; margin: 10px 0;")
        self.layout_principal.addWidget(linha)
        
        # --- SELETOR OPERACIONAL (Combobox) ---
        self.lbl_acao = QLabel("<b>Ações e Guias Disponíveis:</b>")
        self.lbl_acao.setStyleSheet("color: #2D3748; font-size: 13px; margin-top: 5px;")
        self.layout_principal.addWidget(self.lbl_acao)
        
        self.combo_atalhos = QComboBox()
        self.combo_atalhos.setStyleSheet(f"""
            QComboBox {{ border: 2px solid {COR_PRIMARIA}; border-radius: 8px; padding: 12px; background-color: white; font-size: 13px; font-weight: bold; color: {COR_PRIMARIA}; }}
            QComboBox:hover {{ background-color: #F0F4F8; }}
            QComboBox::drop-down {{ border: none; width: 30px; }}
        """)
        self.combo_atalhos.setCursor(Qt.PointingHandCursor)
        
        if _modo_atual == "operacional":
            self.combo_atalhos.addItems([
                "--- Selecione um Processo ---", 
                "⚡ AÇÃO: Adicionar Mapa Base (Google)",
                "⚡ AÇÃO: Nova Camada para Vetorização",
                "⚡ AÇÃO: Gerar Áreas de Influência / APP",
                "⚡ AÇÃO: Limpar feições selecionadas",
                "📖 GUIA: Tour Guiado (A Mesa de Trabalho)",
                "📖 GUIA: Preparar Shapefile para SISLA",
                "📖 GUIA: Como recortar um arquivo (Clip)?",
                "📖 GUIA: Como cruzar dados (Interseção)?",
                "📖 GUIA: Como subtrair áreas (Diferença)?",
                "📖 GUIA: Como unir atributos espacialmente?",
                "📖 GUIA: Como mesclar camadas (Merge)?",
                "📖 GUIA: Delimitar Bacias Hidrográficas", 
                "📖 GUIA: Correção de Projeção em Massa"
            ])
            
        self.combo_atalhos.currentIndexChanged.connect(self.usar_atalho)
        self.layout_principal.addWidget(self.combo_atalhos)
        
        # --- PAINEL ANALISTA (Botões/Dashboard) ---
        self.container_analista = QWidget()
        layout_analista = QVBoxLayout(self.container_analista)
        layout_analista.setContentsMargins(0, 10, 0, 0)
        layout_analista.setSpacing(12)
        
        def get_card_style(cor_fundo, cor_hover):
            return f"""
                QPushButton {{ background-color: {cor_fundo}; border-radius: 8px; padding: 16px; font-size: 13px; font-weight: bold; color: white; text-align: center; border: none; }}
                QPushButton:hover {{ background-color: {cor_hover}; }}
            """
        
        btn_multi = QPushButton("🛰️  Processamento Multiespectral e Topográfico")
        btn_multi.setCursor(Qt.PointingHandCursor)
        btn_multi.setStyleSheet(get_card_style("#1E507E", "#153A5B")) 
        btn_multi.clicked.connect(lambda: self.executar_acao_qgis("create_multispectral"))

        btn_bacias = QPushButton("💧  Extração de Bacias e Drenagem (GRASS)")
        btn_bacias.setCursor(Qt.PointingHandCursor)
        btn_bacias.setStyleSheet(get_card_style("#0D9488", "#0F766E")) 
        btn_bacias.clicked.connect(lambda: self.executar_acao_qgis("extract_basin"))
        
        layout_analista.addWidget(btn_multi)
        layout_analista.addWidget(btn_bacias)
        self.btn_relatorio = QPushButton("📑  Relatório Comparativo (PDF)")
        self.btn_relatorio.setCursor(Qt.PointingHandCursor)
        self.btn_relatorio.setStyleSheet(get_card_style("#34495E", "#243746"))
        self.btn_relatorio.clicked.connect(lambda: abrir_relatorio_comparativo_hgeo(self))
        layout_analista.addWidget(self.btn_relatorio)

        self.layout_principal.addWidget(self.container_analista)
        
        # --- PAINEL GUIADO (Apenas para Operacional) ---
        self.panel_guiado = QWidget()
        self.panel_guiado.setStyleSheet(f"background-color: white; border: 1px solid #E2E8F0; border-radius: 8px; margin-top: 15px;")
        self.layout_guiado = QHBoxLayout(self.panel_guiado)
        
        self.btn_proximo = QPushButton("✅ Próxima Etapa")
        self.btn_proximo.setCursor(Qt.PointingHandCursor)
        self.btn_proximo.setStyleSheet(f"QPushButton {{ background-color: {COR_PRIMARIA}; color: white; padding: 10px; font-weight: bold; border-radius: 6px; border: none; font-size: 13px; }} QPushButton:hover {{ background-color: {COR_PRIMARIA_HOVER}; }}")
        self.btn_proximo.clicked.connect(self.avancar_passo)
        
        self.btn_cancelar = QPushButton("❌ Cancelar")
        self.btn_cancelar.setCursor(Qt.PointingHandCursor)
        self.btn_cancelar.setStyleSheet("QPushButton { background-color: #EF5350; color: white; padding: 10px; font-weight: bold; border-radius: 6px; border: none; font-size: 13px; } QPushButton:hover { background-color: #E53935; }")
        self.btn_cancelar.clicked.connect(self.cancelar_rotina)
        
        self.layout_guiado.addWidget(self.btn_proximo)
        self.layout_guiado.addWidget(self.btn_cancelar)
        self.panel_guiado.hide() 
        self.layout_principal.addWidget(self.panel_guiado)
        
        # --- CONSOLE DE LOGS ---
        self.lbl_log = QLabel("<b>Console / Histórico:</b>")
        self.lbl_log.setStyleSheet("color: #718096; font-size: 13px; margin-top: 15px;")
        self.layout_principal.addWidget(self.lbl_log)
        
        self.chat_history = QTextEdit()
        self.chat_history.setReadOnly(True)
        self.chat_history.setStyleSheet("background-color: white; font-size: 13px; padding: 15px; border: 1px solid #E2E8F0; border-radius: 8px; color: #2D3748;")
        self.layout_principal.addWidget(self.chat_history)
        
        # --- LÓGICA DE VISUALIZAÇÃO POR PERFIL ---
        if _modo_atual == "analista":
            self.lbl_acao.hide()
            self.combo_atalhos.hide()
            self.container_analista.show()
            self.chat_history.append(f"<span style='color: {COR_PRIMARIA};'><b>⚙️ Terminal:</b></span> Módulos GDAL/GRASS carregados e prontos.")
        else:
            self.container_analista.hide()
            self.lbl_acao.show()
            self.combo_atalhos.show()
            self.chat_history.append(f"<span style='color: {COR_PRIMARIA};'><b>⚙️ Copiloto:</b></span> Módulo de Geoprocessamento ativo.")
            
        self.rotina_ativa = None
        self.passo_atual = 0
        self.ROTINAS_GUIADAS = {}
        self.BASE_DE_CONHECIMENTO = {}
        self.janela_passo3 = None
        
        self.carregar_cerebro_do_perfil()

    def abrir_menu_selecao(self):
        self.hide()
        global _assistente_welcome
        if _assistente_welcome is None:
            _assistente_welcome = WelcomeDialog(qgis.utils.iface.mainWindow())
        _assistente_welcome.show()
        _assistente_welcome.raise_()

    def carregar_cerebro_do_perfil(self):
        global _modo_atual
        
        guia_sisla = {
            "palavras": ["sisla", "imasul", "preparar shapefile", "classe", "cnpj", "licenciamento"],
            "nome": "Preparar Shapefile (Ex: SISLA)",
            "passos": [
                "<b>Atenção ao formato:</b> A maioria dos sistemas exige Shapefile (.shp). Se o seu ficheiro for um KML, clique com o botão direito nele, vá em <b>Exportar > Salvar Feições Como...</b> e salve como Shapefile (ESRI).",
                "<b>Unificar Polígonos:</b> Vá em <b>Vetor > Geoprocessamento > Dissolver</b> para unificá-las numa única feição.",
                "<b>Limpar Tabela:</b> Abra a <b>Tabela de Atributos</b>. Clique no ícone do Lápis (Edição) e no ícone 'Excluir Campo' (lixeira) para apagar todas as colunas antigas.",
                "<b>Criar Coluna Padrão:</b> Clique no ícone <b>'Novo Campo'</b> (Ctrl+W). Nomeie como <b>'CLASSE'</b>. Tipo: <b>'Texto (String)'</b> (comprimento 255).",
                "<b>Preencher Código:</b> Preencha a coluna 'CLASSE' com o código da atividade. Feche a edição e guarde.",
                "<b>Salvar e Nomear:</b> Exporte os arquivos (.shp, .shx, .dbf, .prj) com o <b>CNPJ</b> do empreendimento como nome."
            ]
        }
        
        if _modo_atual == "analista":
            self.ROTINAS_GUIADAS = {}
            self.BASE_DE_CONHECIMENTO = {
                "processar_multiespectral": {
                    "palavras": ["analise", "multiespectral", "gdal"], 
                    "resposta": "A iniciar Módulo de Processamento GDAL...",
                    "acao": "create_multispectral"
                }
            }
        else:
            self.ROTINAS_GUIADAS = {
                "tour": {
                    "palavras": ["tour", "guiado", "treinamento", "mesa", "trabalho"],
                    "nome": "Tour Guiado: A Mesa de Trabalho",
                    "passos": [
                        "<b>A Mesa de Trabalho:</b> Bem-vindo! Siga este guia rápido para entender onde estão as principais ferramentas do QGIS.",
                        "<b>Menu de Camadas (Vetores/Rasters):</b> No menu superior vá em <b>Camada > Adicionar Camada</b> para trazer os seus Shapefiles e Imagens de Satélite para o mapa.",
                        "<b>Painel de Camadas:</b> Aqui na barra lateral esquerda as camadas ficam empilhadas. A ordem importa: o que está no topo da lista fica por cima no mapa.",
                        "<b>Caixa de Processamento:</b> Aqui na direita fica o motor do QGIS. Precisa recortar, fazer buffer ou gerar mapas? Use a barra de pesquisa deste painel.",
                        "<b>Ferramentas de Edição:</b> Para desenhar polígonos ou linhas, primeiro clique no ícone de <b>Lápis amarelo</b> (Alternar Edição) na barra de ferramentas superior.",
                        "<b>O Segredo: Projeções (SRC):</b> Aqui no rodapé direito está o código EPSG. Ele garante que seu mapa não fique deslocado (Ex: Use EPSG 4674 para SIRGAS 2000 Geográfico)."
                    ]
                },
                "recortar": {
                    "palavras": ["recortar", "cortar", "clip", "mascara"],
                    "nome": "Recortar Camadas (Clip)",
                    "passos": [
                        "Se for um vetor, vá em <b>Vetor > Geoprocessamento > Recortar</b>.",
                        "Coloque a camada principal em 'Camada de Entrada'.",
                        "Coloque a camada delimitadora em 'Camada de Sobreposição' e Execute."
                    ]
                },
                "intersecao": {
                    "palavras": ["cruzar", "dados", "intersecao", "intersect", "sobreposicao"],
                    "nome": "Cruzar Dados (Interseção)",
                    "passos": [
                        "Vá em <b>Vetor > Geoprocessamento > Interseção</b>.",
                        "Em 'Camada de Entrada', coloque a sua camada principal (Ex: Lotes).",
                        "Em 'Camada de Sobreposição', coloque a camada que vai cruzar (Ex: APP ou Área de Risco).",
                        "Clique em Executar. O resultado será apenas a área onde ambas se tocam, mantendo os atributos das duas!"
                    ]
                },
                "diferenca": {
                    "palavras": ["subtrair", "areas", "diferenca", "apagar", "erase"],
                    "nome": "Subtrair Áreas (Diferença)",
                    "passos": [
                        "Vá em <b>Vetor > Geoprocessamento > Diferença</b>.",
                        "Em 'Camada de Entrada', coloque a camada total (Ex: Fazenda).",
                        "Em 'Camada de Sobreposição', coloque o que deseja recortar/apagar (Ex: Área de Preservação).",
                        "Clique em Executar. O resultado será a Fazenda com um 'buraco' exato onde ficava a APP."
                    ]
                },
                "spatial_join": {
                    "palavras": ["unir", "atributos", "espacialmente", "spatial", "join", "localizacao"],
                    "nome": "Unir Atributos por Localização",
                    "passos": [
                        "Vá em <b>Vetor > Gerenciar Dados > Unir atributos pela localização</b>.",
                        "Em 'Camada base', escolha a camada que vai receber as informações (Ex: Pontos de Poços).",
                        "Em 'Camada de união', escolha a camada que tem a informação (Ex: Polígonos dos Municípios).",
                        "Escolha o predicado geométrico (ex: 'intersecciona' ou 'está dentro') e clique em Executar."
                    ]
                },
                "merge": {
                    "palavras": ["mesclar", "juntar", "merge", "unir", "camadas"],
                    "nome": "Mesclar Camadas Vetoriais",
                    "passos": [
                        "Vá em <b>Vetor > Gerenciar Dados > Mesclar camadas vetoriais</b>.",
                        "Clique no botão [...] para selecionar as camadas que deseja juntar (precisam ser do mesmo tipo geométrico).",
                        "Defina o SRC de destino e clique em Executar. Terá um único arquivo com todas as feições."
                    ]
                },
                "sisla": guia_sisla,
                "bacias": {
                    "palavras": ["bacia", "bacias", "hidrografica", "delimitacao", "watershed", "siriema"],
                    "nome": "Delimitar Bacias Hidrográficas",
                    "passos": [
                        "Certifique-se de que o seu MDE não tem depressões irreais. Se necessário, use a ferramenta <b>'Fill sinks' (Wang & Liu)</b>.",
                        "Abra a Caixa de Processamento e pesquise por <b>r.watershed</b>.",
                        "Em 'Elevation', coloque o seu MDE preenchido. Defina o tamanho mínimo da bacia.",
                        "Marque <b>'Half-basins'</b> e <b>'Drainage direction'</b>.",
                        "Clique em Executar."
                    ]
                },
                "correcao_massa": {
                    "palavras": ["correcao", "projecao", "massa", "reprojetar"],
                    "nome": "Reprojetar Camadas em Lote",
                    "passos": [
                        "Abra a Caixa de Processamento e pesquise por <b>'Reprojetar camada'</b>.",
                        "Clique com o botão direito na ferramenta e escolha <b>'Executar como processo em lote...'</b>.",
                        "Clique em <b>'Preenchimento automático' > 'Selecionar a partir de camadas abertas'</b> e marque os vetores.",
                        "Defina o 'SRC de destino' (Ex: 31982 para Fuso 22S) e preencha para todas as linhas.",
                        "Execute o lote."
                    ]
                }
            }
            self.BASE_DE_CONHECIMENTO = {
                "add_google": {
                    "palavras": ["google", "satelite", "mapa base"],
                    "resposta": "Conectando ao serviço de satélite...",
                    "acao": "add_google_satellite"
                },
                "criar_rascunho": {
                    "palavras": ["rascunho", "camada temporaria", "vetorizacao", "desenhar", "nova", "camada"],
                    "resposta": "Iniciando o criador de vetores...",
                    "acao": "create_temp_layer"
                },
                "limpar_selecao": {
                    "palavras": ["limpar", "selecao", "desmarcar", "feicoes", "selecionadas"],
                    "resposta": "Limpando feições selecionadas no projeto...",
                    "acao": "clear_selections"
                },
                "gerar_buffer": {
                    "palavras": ["gerar", "areas", "influencia", "app", "buffer"],
                    "resposta": "A iniciar Módulo Espacial (Buffer)...",
                    "acao": "create_buffer"
                }
            }

    def processar_texto(self, texto):
        texto = texto.lower()
        for com_acento, sem_acento in {'á':'a', 'é':'e', 'í':'i', 'ó':'o', 'ú':'u', 'ã':'a', 'õ':'o', 'ç':'c'}.items():
            texto = texto.replace(com_acento, sem_acento)
        palavras = [p for p in texto.split() if p not in ["como", "faco", "fazer", "quero", "para", "um", "uma", "e", "ou"]]
        return " ".join(palavras)

    def usar_atalho(self):
        if self.combo_atalhos.currentIndex() == 0:
            return
            
        texto_selecionado = self.combo_atalhos.currentText()
        pergunta = texto_selecionado.replace("⚡ AÇÃO: ", "").replace("📖 GUIA: ", "")
        
        if self.rotina_ativa:
            self.chat_history.append(f"<br><span style='color: #e74c3c;'><b>Copiloto:</b> Conclua ou cancele o guia atual antes de pedir outra função.</span>")
            self.combo_atalhos.setCurrentIndex(0)
            self.rolar_chat()
            return
            
        self.chat_history.append(f"<br><span style='color: {COR_USUARIO};'><b>Você selecionou:</b> {pergunta}</span>")
        
        texto_limpo = self.processar_texto(pergunta)

        melhor_guia = None
        max_pts_guia = 0
        for chave, dados in self.ROTINAS_GUIADAS.items():
            pontos = sum(1 for p in dados["palavras"] if p in texto_limpo)
            if pontos > max_pts_guia:
                max_pts_guia = pontos
                melhor_guia = dados

        if melhor_guia:
            self.iniciar_rotina(melhor_guia)
            self.combo_atalhos.setCurrentIndex(0)
            return

        melhor_acao = None
        max_pts_acao = 0
        for chave, dados in self.BASE_DE_CONHECIMENTO.items():
            pontos = sum(1 for p in dados["palavras"] if p in texto_limpo)
            if pontos > max_pts_acao:
                max_pts_acao = pontos
                melhor_acao = dados

        if melhor_acao:
            self.chat_history.append(f"<span style='color: {COR_PRIMARIA};'><b>Copiloto:</b> {melhor_acao['resposta']}</span>")
            self.rolar_chat()
            if "acao" in melhor_acao:
                self.executar_acao_qgis(melhor_acao["acao"])
        else:
            self.chat_history.append(f"<span style='color: #e74c3c;'><b>Copiloto:</b> Comando não reconhecido internamente.</span>")
            self.rolar_chat()
            
        self.combo_atalhos.setCurrentIndex(0)

    def executar_acao_qgis(self, acao_nome):
        try:
            if acao_nome == "add_google_satellite":
                url_google = "type=xyz&url=https://mt1.google.com/vt/lyrs%3Ds%26x%3D%7Bx%7D%26y%3D%7By%7D%26z%3D%7Bz%7D&zmax=20&zmin=0"
                rlayer = QgsRasterLayer(url_google, "Google Satellite", "wms")
                if rlayer.isValid():
                    QgsProject.instance().addMapLayer(rlayer)
                    root = QgsProject.instance().layerTreeRoot()
                    my_layer = root.findLayer(rlayer.id())
                    clone = my_layer.clone()
                    root.insertChildNode(-1, clone)
                    root.removeChildNode(my_layer)
                    self.chat_history.append(f"<span style='color: {COR_DESTAQUE};'>✅ Satélite ativado com sucesso!</span>")
                else:
                    self.chat_history.append("<span style='color: #e74c3c;'>❌ Erro ao conectar ao servidor.</span>")

            elif acao_nome == "create_temp_layer":
                dialog = RascunhoDialog(self)
                if dialog.exec_() == QDialog.Accepted:
                    geom_type, epsg = dialog.get_data()
                    layer_name = f"Vetor_{geom_type}"
                    layer = QgsVectorLayer(f"{geom_type}?crs=epsg:{epsg}", layer_name, "memory")
                    if layer.isValid():
                        QgsProject.instance().addMapLayer(layer)
                        qgis.utils.iface.setActiveLayer(layer)
                        layer.startEditing()
                        self.chat_history.append(f"<span style='color: {COR_DESTAQUE};'>✅ Camada instanciada! (EPSG:{epsg}). Pronta para vetorização.</span>")

            elif acao_nome == "create_buffer":
                dialog = BufferDialog(self)
                if not dialog.layers:
                    self.chat_history.append("<span style='color: #e74c3c;'>❌ Erro: Carregue dados vetoriais primeiro.</span>")
                    self.rolar_chat()
                    return

                if dialog.exec_() == QDialog.Accepted:
                    layer_name, dist_str, unit, segments_str, cap_style_str, dissolve = dialog.get_data()
                    if "indisponível" in unit:
                        self.chat_history.append("<span style='color: #e74c3c;'>❌ Unidade 'hectares' ainda não é suportada. Selecione metros ou quilômetros.</span>")
                        self.rolar_chat()
                        return
                    dist_val = float(dist_str.replace(',', '.'))
                    segments = int(segments_str)
                    if unit == "quilômetros": dist_val *= 1000
                    
                    cap_map = {"Arredondado": 0, "Plano": 1, "Quadrado": 2}
                    layer = dialog.layers[layer_name]
                    params = {'INPUT': layer, 'DISTANCE': dist_val, 'SEGMENTS': segments, 'END_CAP_STYLE': cap_map[cap_style_str], 'DISSOLVE': dissolve, 'OUTPUT': 'memory:'}
                    result = processing.run("native:buffer", params)
                    output_layer = result['OUTPUT']
                    output_layer.setName(f"APP_Influencia_{layer_name}")
                    QgsProject.instance().addMapLayer(output_layer)
                    self.chat_history.append(f"<span style='color: {COR_DESTAQUE};'>✅ Área de proteção gerada com sucesso!</span>")

            elif acao_nome == "create_multispectral":
                dialog = AnaliseMultiespectralDialog(self)
                if not dialog.rasters:
                    self.chat_history.append(f"<span style='color: {COR_PRIMARIA};'>ℹ️ Nenhuma imagem encontrada no painel. Use 'Topografia / Relevo' &gt; 🌎 Baixar MDE para buscar automaticamente, ou carregue um raster antes.</span>")
                    self.rolar_chat()

                if dialog.exec_() == QDialog.Accepted:
                    cat, analise, nome_a, nome_b, nome_c, nome_d = dialog.get_data()
                    
                    self.chat_history.append(f"<br><span style='color: {COR_PRIMARIA};'>⏳ GDAL: Processando {analise.split(' ')[0]}...</span>")
                    self.rolar_chat()
                    
                    QApplication.setOverrideCursor(Qt.WaitCursor)
                    self.repaint() 
                    
                    try:
                        layer1 = dialog.rasters[nome_a] if nome_a else None
                        layer2 = dialog.rasters[nome_b] if nome_b else None
                        layer3 = dialog.rasters[nome_c] if nome_c else None
                        
                        if cat == "Composição Visual (RGB)":
                            sigla = "TrueColor" if "True" in analise else "FalseColor"
                            params = {
                                'INPUT': [layer1, layer2, layer3],
                                'RESOLUTION': 0, 'SEPARATE': True, 'OUTPUT': 'TEMPORARY_OUTPUT'
                            }
                            result = processing.run("gdal:buildvirtualraster", params)
                            rgb_layer = QgsRasterLayer(result['OUTPUT'], f"RGB_{sigla}")
                            if rgb_layer.isValid():
                                QgsProject.instance().addMapLayer(rgb_layer)
                                self.chat_history.append(f"<span style='color: {COR_DESTAQUE};'>✅ Composição Virtual RGB criada.</span>")
                            else:
                                raise Exception("Falha ao gerar VRT.")
                                
                        else:
                            formula = ""
                            sigla = analise.split(" ")[0]
                            
                            # NO_DATA=0: as bandas de satélite (Sentinel-2/Landsat) usam 0 como
                            # valor de "sem cobertura" nas bordas/fora da cena. Em todas essas
                            # fórmulas, A=B=C=0 sempre resulta em 0 — então marcar 0 como NoData
                            # na saída evita que a área sem imagem apareça pintada (ex: como se
                            # fosse "Água/Área Urbana" no NDVI) em vez de transparente.
                            if "NDVI" in analise:
                                formula = '(1.0 * A - 1.0 * B) / (1.0 * A + 1.0 * B + 0.000001)'
                                params = {'INPUT_A': layer1, 'BAND_A': 1, 'INPUT_B': layer2, 'BAND_B': 1, 'FORMULA': formula, 'NO_DATA': 0, 'RTYPE': 5, 'OUTPUT': 'TEMPORARY_OUTPUT'}
                            elif "SAVI" in analise:
                                formula = '((1.0 * A - 1.0 * B) / (1.0 * A + 1.0 * B + 0.5)) * 1.5'
                                params = {'INPUT_A': layer1, 'BAND_A': 1, 'INPUT_B': layer2, 'BAND_B': 1, 'FORMULA': formula, 'NO_DATA': 0, 'RTYPE': 5, 'OUTPUT': 'TEMPORARY_OUTPUT'}
                            elif "EVI" in analise:
                                formula = '2.5 * (1.0 * A - 1.0 * B) / (1.0 * A + 6.0 * B - 7.5 * C + 1.0)'
                                params = {'INPUT_A': layer1, 'BAND_A': 1, 'INPUT_B': layer2, 'BAND_B': 1, 'INPUT_C': layer3, 'BAND_C': 1, 'FORMULA': formula, 'NO_DATA': 0, 'RTYPE': 5, 'OUTPUT': 'TEMPORARY_OUTPUT'}
                            elif any(idx in analise for idx in ["NDWI", "MNDWI", "NDMI", "NDSI", "NBR", "NBR2", "NDBI", "UI"]):
                                formula = '(1.0 * A - 1.0 * B) / (1.0 * A + 1.0 * B + 0.000001)'
                                params = {'INPUT_A': layer1, 'BAND_A': 1, 'INPUT_B': layer2, 'BAND_B': 1, 'FORMULA': formula, 'NO_DATA': 0, 'RTYPE': 5, 'OUTPUT': 'TEMPORARY_OUTPUT'}
                            elif "BAI" in analise:
                                formula = '((1.0 * A + 1.0 * B) / 2.0) - 1.0 * C'
                                params = {'INPUT_A': layer1, 'BAND_A': 1, 'INPUT_B': layer2, 'BAND_B': 1, 'INPUT_C': layer3, 'BAND_C': 1, 'FORMULA': formula, 'NO_DATA': 0, 'RTYPE': 5, 'OUTPUT': 'TEMPORARY_OUTPUT'}
                            elif "Hipsometria" in analise:
                                formula = 'A * 1.0'
                                params = {'INPUT_A': layer1, 'BAND_A': 1, 'FORMULA': formula, 'RTYPE': 5, 'OUTPUT': 'TEMPORARY_OUTPUT'}

                            result = processing.run("gdal:rastercalculator", params)
                            idx_layer = QgsRasterLayer(result['OUTPUT'], f"{sigla}_Calculado")
                            
                            if idx_layer.isValid():
                                shader = QgsRasterShader()
                                color_ramp = QgsColorRampShader()
                                color_ramp.setColorRampType(QgsColorRampShader.Interpolated) 
                                
                                if "Hipsometria" in analise:
                                    provider = idx_layer.dataProvider()
                                    stats = provider.bandStatistics(1, QgsRasterBandStats.Min | QgsRasterBandStats.Max)
                                    vmin = stats.minimumValue
                                    vmax = stats.maximumValue
                                    
                                    if vmin < -500:
                                        vmin = 0
                                        
                                    diff = vmax - vmin
                                    if diff <= 0: diff = 1
                                    
                                    items = [
                                        QgsColorRampShader.ColorRampItem(vmin, QColor("#1a9641"), f"Baixada / Planície ({vmin:.0f}m)"),
                                        QgsColorRampShader.ColorRampItem(vmin + diff*0.33, QColor("#a6d96a"), f"Elevação Baixa ({vmin + diff*0.33:.0f}m)"),
                                        QgsColorRampShader.ColorRampItem(vmin + diff*0.66, QColor("#fdae61"), f"Elevação Média ({vmin + diff*0.66:.0f}m)"),
                                        QgsColorRampShader.ColorRampItem(vmax, QColor("#d7191c"), f"Crista / Topo de Morro ({vmax:.0f}m)")
                                    ]
                                    color_ramp.setColorRampItemList(items)
                                    shader.setRasterShaderFunction(color_ramp)
                                    renderer = QgsSingleBandPseudoColorRenderer(idx_layer.dataProvider(), 1, shader)
                                    renderer.setClassificationMin(vmin)
                                    renderer.setClassificationMax(vmax)

                                elif sigla in ["NDWI", "MNDWI"]:
                                    items = [
                                        QgsColorRampShader.ColorRampItem(-0.1, QColor("#e0f3f8"), "Solo Seco / Superfície Densa (<= -0.1)"),
                                        QgsColorRampShader.ColorRampItem(0.2, QColor("#92c5de"), "Transição / Umidade Leve (-0.1 a 0.2)"),
                                        QgsColorRampShader.ColorRampItem(1.0, QColor("#053061"), "Corpos D'água (> 0.2)")
                                    ]
                                    color_ramp.setColorRampItemList(items)
                                    shader.setRasterShaderFunction(color_ramp)
                                    renderer = QgsSingleBandPseudoColorRenderer(idx_layer.dataProvider(), 1, shader)
                                    renderer.setClassificationMin(-1.0)
                                    renderer.setClassificationMax(1.0)

                                elif sigla in ["NDVI", "SAVI", "EVI"]:
                                    items = [
                                        QgsColorRampShader.ColorRampItem(0.0, QColor("#d73027"), "Água / Área Urbana (<= 0.0)"),
                                        QgsColorRampShader.ColorRampItem(0.25, QColor("#fdae61"), "Solo Exposto / Seco (0.0 a 0.25)"),
                                        QgsColorRampShader.ColorRampItem(0.5, QColor("#ffffbf"), "Vegetação Esparsa / Pasto (0.25 a 0.5)"),
                                        QgsColorRampShader.ColorRampItem(0.7, QColor("#a6d96a"), "Vegetação Moderada (0.5 a 0.7)"),
                                        QgsColorRampShader.ColorRampItem(1.0, QColor("#1a9850"), "Vegetação Densa (> 0.7)")
                                    ]
                                    color_ramp.setColorRampItemList(items)
                                    shader.setRasterShaderFunction(color_ramp)
                                    renderer = QgsSingleBandPseudoColorRenderer(idx_layer.dataProvider(), 1, shader)
                                    renderer.setClassificationMin(-1.0)
                                    renderer.setClassificationMax(1.0)

                                elif sigla in ["NDBI", "UI"]:
                                    items = [
                                        QgsColorRampShader.ColorRampItem(-0.2, QColor("#1a9850"), "Vegetação Densa / Floresta (<= -0.2)"),
                                        QgsColorRampShader.ColorRampItem(0.0, QColor("#a6d96a"), "Vegetação Esparsa / Pastagem (-0.2 a 0.0)"),
                                        QgsColorRampShader.ColorRampItem(0.15, QColor("#fee08b"), "Solo Exposto / Misto (0.0 a 0.15)"),
                                        QgsColorRampShader.ColorRampItem(0.3, QColor("#f46d43"), "Área Urbana Leve / Solo Seco (0.15 a 0.3)"),
                                        QgsColorRampShader.ColorRampItem(1.0, QColor("#a50026"), "Asfalto / Urbano Denso (> 0.3)")
                                    ]
                                    color_ramp.setColorRampItemList(items)
                                    shader.setRasterShaderFunction(color_ramp)
                                    renderer = QgsSingleBandPseudoColorRenderer(idx_layer.dataProvider(), 1, shader)
                                    renderer.setClassificationMin(-1.0)
                                    renderer.setClassificationMax(1.0)

                                elif sigla == "NDMI":
                                    items = [
                                        QgsColorRampShader.ColorRampItem(-0.2, QColor("#303030"), "Superfície Seca (<= -0.2)"),
                                        QgsColorRampShader.ColorRampItem(0.2, QColor("#8c8c8c"), "Umidade Moderada (-0.2 a 0.2)"),
                                        QgsColorRampShader.ColorRampItem(1.0, QColor("#fee08b"), "Alta Umidade / Dossel (> 0.2)")
                                    ]
                                    color_ramp.setColorRampItemList(items)
                                    shader.setRasterShaderFunction(color_ramp)
                                    renderer = QgsSingleBandPseudoColorRenderer(idx_layer.dataProvider(), 1, shader)
                                    renderer.setClassificationMin(-1.0)
                                    renderer.setClassificationMax(1.0)

                                elif cat == "Solo e Áreas Expostas":
                                    items = [
                                        QgsColorRampShader.ColorRampItem(0.0, QColor("#27ae60"), "Vegetação / Sombra (<= 0.0)"),
                                        QgsColorRampShader.ColorRampItem(0.2, QColor("#f39c12"), "Transição / Solo Misto (0.0 a 0.2)"),
                                        QgsColorRampShader.ColorRampItem(1.0, QColor("#d35400"), "Solo Exposto / Areia (> 0.2)")
                                    ]
                                    color_ramp.setColorRampItemList(items)
                                    shader.setRasterShaderFunction(color_ramp)
                                    renderer = QgsSingleBandPseudoColorRenderer(idx_layer.dataProvider(), 1, shader)
                                    renderer.setClassificationMin(-1.0)
                                    renderer.setClassificationMax(1.0)

                                elif cat == "Queimada / Estresse":
                                    items = [
                                        QgsColorRampShader.ColorRampItem(-0.25, QColor("#000000"), "Fogo Muito Severo (<= -0.25)"),
                                        QgsColorRampShader.ColorRampItem(-0.1, QColor("#c0392b"), "Queimada Alta/Moderada (-0.25 a -0.1)"),
                                        QgsColorRampShader.ColorRampItem(0.1, QColor("#f39c12"), "Queimada Leve / Transição (-0.1 a 0.1)"),
                                        QgsColorRampShader.ColorRampItem(1.0, QColor("#1a9850"), "Não Queimado / Saudável (> 0.1)")
                                    ]
                                    color_ramp.setColorRampItemList(items)
                                    shader.setRasterShaderFunction(color_ramp)
                                    renderer = QgsSingleBandPseudoColorRenderer(idx_layer.dataProvider(), 1, shader)
                                    renderer.setClassificationMin(-1.0)
                                    renderer.setClassificationMax(1.0)

                                else: 
                                    items = [
                                        QgsColorRampShader.ColorRampItem(-0.3, QColor("#d7191c"), "Baixo (<= -0.3)"),
                                        QgsColorRampShader.ColorRampItem(0.3, QColor("#ffffbf"), "Médio (-0.3 a 0.3)"),
                                        QgsColorRampShader.ColorRampItem(1.0, QColor("#2b83ba"), "Alto (> 0.3)")
                                    ]
                                    color_ramp.setColorRampItemList(items)
                                    shader.setRasterShaderFunction(color_ramp)
                                    renderer = QgsSingleBandPseudoColorRenderer(idx_layer.dataProvider(), 1, shader)
                                    renderer.setClassificationMin(-1.0)
                                    renderer.setClassificationMax(1.0)
                                
                                idx_layer.setRenderer(renderer)
                                idx_layer.triggerRepaint()
                                
                                QgsProject.instance().addMapLayer(idx_layer)
                                self.chat_history.append(f"<span style='color: {COR_DESTAQUE};'>✅ {sigla} gerado e injetado com sucesso no mapa!</span>")
                            else:
                                raise Exception("Falha ao carregar o raster gerado.")
                                
                    except Exception as e:
                        self.chat_history.append(f"<span style='color: #e74c3c;'>❌ Erro no motor GDAL: {str(e)}</span>")
                    finally:
                        QApplication.restoreOverrideCursor()
                        self.rolar_chat()
            
            elif acao_nome == "extract_basin":
                self.chat_history.append(f"<br><span style='color: {COR_PRIMARIA};'>⏳ Iniciando Assistente de Modelagem Hidrológica (GRASS)...</span>")
                self.rolar_chat()
                self.dialog_mde = SelecaoMDEDialog(self)
                self.dialog_mde.show()

            elif acao_nome == "clear_selections":
                for layer in QgsProject.instance().mapLayers().values():
                    if isinstance(layer, QgsVectorLayer): layer.removeSelection()
                self.chat_history.append(f"<span style='color: {COR_DESTAQUE};'>✅ Seleções limpas no projeto.</span>")
                
            self.rolar_chat()
        except Exception as e:
            self.chat_history.append(f"<span style='color: #e74c3c;'>❌ Falha crítica: {str(e)}</span>")
            self.rolar_chat()

    def iniciar_rotina(self, rotina):
        self.rotina_ativa = rotina
        self.passo_atual = 0
        self.chat_history.append(f"<br><span style='color: {COR_DESTAQUE};'><b>Copiloto:</b> Guia ativado: <b>{rotina['nome']}</b></span>")
        self.panel_guiado.show()
        self.combo_atalhos.setEnabled(False)
        self.enviar_passo_atual()

    def enviar_passo_atual(self):
        passo_texto = self.rotina_ativa['passos'][self.passo_atual]
        self.chat_history.append(f"<br><span style='color: {COR_PRIMARIA};'><b>👉 Etapa {self.passo_atual + 1}:</b><br>{passo_texto}</span>")
        self.rolar_chat()
        if self.passo_atual == len(self.rotina_ativa['passos']) - 1:
            self.btn_proximo.setText("🎉 Finalizar Guia")
        else:
            self.btn_proximo.setText("✅ Avançar Etapa")

    def avancar_passo(self):
        self.passo_atual += 1
        if self.passo_atual < len(self.rotina_ativa['passos']):
            self.enviar_passo_atual()
        else:
            self.chat_history.append(f"<br><span style='color: {COR_DESTAQUE};'><b>Copiloto:</b> Procedimento concluído.</span>")
            self.encerrar_rotina()

    def cancelar_rotina(self):
        self.chat_history.append(f"<br><span style='color: #e74c3c;'><b>Copiloto:</b> Guia interrompido.</span>")
        self.encerrar_rotina()

    def encerrar_rotina(self):
        self.rotina_ativa = None
        self.passo_atual = 0
        self.panel_guiado.hide()
        self.combo_atalhos.setEnabled(True)
        self.rolar_chat()

    def rolar_chat(self):
        self.chat_history.verticalScrollBar().setValue(self.chat_history.verticalScrollBar().maximum())

class FloatingButton(QWidget):
    def __init__(self, parent=None):
        super(FloatingButton, self).__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.resize(74, 74) 
        self.setAttribute(Qt.WA_TranslucentBackground) 
        self.layout_principal = QVBoxLayout()
        self.layout_principal.setContentsMargins(5,5,5,5) 
        
        self.btn = QPushButton("🤖")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setStyleSheet(f"""
            QPushButton {{
                border-radius: 30px; 
                background-color: {COR_PRIMARIA}; 
                color: white; 
                font-size: 28px; 
                border: 2px solid white;
                padding-bottom: 2px;
            }}
            QPushButton:hover {{
                background-color: {COR_PRIMARIA_HOVER};
                border: 2px solid {COR_DESTAQUE};
            }}
        """)
        self.btn.setFixedSize(60, 60)
        aplicar_sombra(self.btn, blur_radius=15, alpha=50, y_offset=3)
        
        self.btn.clicked.connect(self.toggle_chat)
        
        self.layout_principal.addWidget(self.btn)
        self.setLayout(self.layout_principal)
        self.chat_window = ChatWindow(qgis.utils.iface.mainWindow())

    def toggle_chat(self):
        if self.chat_window.isVisible() and self.chat_window.isActiveWindow():
            self.chat_window.hide()
        else:
            geom = self.geometry()
            self.chat_window.move(geom.left() - 360, geom.top() - 530)
            self.chat_window.show()
            self.chat_window.raise_()
            self.chat_window.activateWindow()

def spawn_chat_button(abrir_chat_agora=False):
    global _assistente_chat 
    if _assistente_chat is not None:
        if getattr(_assistente_chat, '_hgeo_mode', None) == _modo_atual:
            _assistente_chat.show()
            if abrir_chat_agora:
                _assistente_chat.chat_window.show()
                _assistente_chat.chat_window.raise_()
                _assistente_chat.chat_window.activateWindow()
            return
        # Keep child tools/workers alive when changing mode.
        _assistente_chat.chat_window.hide()
        _assistente_chat.hide()
        _hgeo_previous_chats.append(_assistente_chat)

    main_window = qgis.utils.iface.mainWindow()
    geom = main_window.geometry()
    _assistente_chat = FloatingButton(main_window)
    _assistente_chat._hgeo_mode = _modo_atual
    _assistente_chat.move(geom.right() - 100, geom.bottom() - 130)
    
    _assistente_chat.show()
    _assistente_chat.raise_()
    if abrir_chat_agora and not _assistente_chat.chat_window.isVisible():
        _assistente_chat.toggle_chat()

class WelcomeDialog(QDialog):
    def __init__(self, parent=None):
        super(WelcomeDialog, self).__init__(parent)
        self.setWindowTitle("H-Geo")
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.resize(650, 420) 
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        self.layout_principal = QVBoxLayout(self)
        self.layout_principal.setContentsMargins(15, 15, 15, 15)
        
        self.container = QWidget()
        self.container.setStyleSheet("""
            QWidget#container {
                background-color: #F8FAFC; 
                border-radius: 12px;
                border: 1px solid #CFD8DC;
            }
        """)
        self.container.setObjectName("container")
        aplicar_sombra(self.container, blur_radius=25, alpha=50, y_offset=5)
        self.layout_principal.addWidget(self.container)

        self.split_layout = QHBoxLayout(self.container)
        self.split_layout.setContentsMargins(0, 0, 0, 0)
        self.split_layout.setSpacing(0)

        # === LADO ESQUERDO (Branding / Logo) ===
        self.left_panel = QWidget()
        self.left_panel.setFixedWidth(240)
        self.left_panel.setStyleSheet(f"""
            background-color: {COR_PRIMARIA};
            border-top-left-radius: 12px;
            border-bottom-left-radius: 12px;
        """)
        self.left_layout = QVBoxLayout(self.left_panel)
        self.left_layout.setContentsMargins(30, 40, 30, 40)
        
        self.lbl_icon = QLabel("🛰️")
        self.lbl_icon.setAlignment(Qt.AlignCenter)
        self.lbl_icon.setStyleSheet("font-size: 60px; color: white;")
        self.left_layout.addWidget(self.lbl_icon)

        self.lbl_title = QLabel("H-Geo")
        self.lbl_title.setAlignment(Qt.AlignCenter)
        self.lbl_title.setStyleSheet("font-size: 32px; font-weight: 900; color: white; margin-top: 10px; font-family: 'Segoe UI';")
        self.left_layout.addWidget(self.lbl_title)

        linha = QWidget()
        linha.setFixedHeight(2)
        linha.setStyleSheet("background-color: rgba(255, 255, 255, 50); margin: 15px 0;")
        self.left_layout.addWidget(linha)

        self.lbl_sub = QLabel("Plataforma de\nGeoprocessamento")
        self.lbl_sub.setAlignment(Qt.AlignCenter)
        self.lbl_sub.setStyleSheet("font-size: 14px; color: #E2E8F0; font-family: 'Segoe UI';")
        self.left_layout.addWidget(self.lbl_sub)
        self.left_layout.addStretch()
        
        self.split_layout.addWidget(self.left_panel)

        # === LADO DIREITO (Ações) ===
        self.right_panel = QWidget()
        self.right_panel.setStyleSheet("background-color: transparent;")
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_layout.setContentsMargins(40, 30, 40, 40)
        self.right_layout.setSpacing(15)

        top_bar = QHBoxLayout()
        top_bar.addStretch()
        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(30, 30)
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.setStyleSheet("QPushButton { background-color: transparent; color: #94A3B8; font-weight: bold; font-size: 16px; border: none; } QPushButton:hover { color: #E53E3E; }")
        self.btn_close.clicked.connect(self.close)
        top_bar.addWidget(self.btn_close)
        self.right_layout.addLayout(top_bar)

        lbl_instruct = QLabel("Selecione o módulo de trabalho:")
        lbl_instruct.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {COR_PRIMARIA}; margin-bottom: 5px; font-family: 'Segoe UI';")
        self.right_layout.addWidget(lbl_instruct)

        self.btn_skip = QPushButton(" ️   Modo Operacional (Copiloto)")
        self.btn_skip.setCursor(Qt.PointingHandCursor)
        self.btn_skip.setStyleSheet(f"""
            QPushButton {{
                background-color: white;
                color: {COR_PRIMARIA};
                padding: 16px 20px;
                font-size: 14px;
                font-weight: bold;
                border-radius: 8px;
                border: 2px solid {COR_PRIMARIA};
                text-align: center;
                font-family: 'Segoe UI';
            }}
            QPushButton:hover {{
                background-color: #F0F4F8;
            }}
        """)
        aplicar_sombra(self.btn_skip, blur_radius=10, y_offset=2, alpha=15)
        self.btn_skip.clicked.connect(self.skip_to_chat_operacional)
        self.right_layout.addWidget(self.btn_skip)
        
        self.btn_avancado = QPushButton("   Modo Analista (GDAL e Rasters)")
        self.btn_avancado.setCursor(Qt.PointingHandCursor)
        self.btn_avancado.setStyleSheet(f"""
            QPushButton {{
                background-color: {COR_DESTAQUE};
                color: white;
                padding: 16px 20px;
                font-size: 14px;
                font-weight: bold;
                border-radius: 8px;
                border: none;
                text-align: center;
                font-family: 'Segoe UI';
                margin-top: 10px;
            }}
            QPushButton:hover {{
                background-color: #58963E;
            }}
        """)
        aplicar_sombra(self.btn_avancado, blur_radius=10, y_offset=2, alpha=20)
        self.btn_avancado.clicked.connect(self.skip_to_chat_analista)
        self.right_layout.addWidget(self.btn_avancado)

        self.right_layout.addStretch()
        self.split_layout.addWidget(self.right_panel)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        cor_linha = QColor(COR_PRIMARIA)
        cor_linha.setAlpha(12) 
        pen = QPen(cor_linha)
        pen.setWidth(1)
        painter.setPen(pen)

        w = self.width()
        h = self.height()
        
        cx = w * 0.70
        cy = h / 2
        raio = min(w, h) * 0.80

        painter.drawEllipse(QPointF(cx, cy), raio, raio)

        for i in range(1, 6):
            painter.drawEllipse(QPointF(cx, cy), raio * (i / 6.0), raio)
            painter.drawEllipse(QPointF(cx, cy), raio, raio * (i / 6.0))

    def skip_to_chat_operacional(self):
        global _modo_atual
        _modo_atual = "operacional"
        self.close()
        spawn_chat_button(abrir_chat_agora=True)
        
    def skip_to_chat_analista(self):
        global _modo_atual
        _modo_atual = "analista"
        self.close()
        spawn_chat_button(abrir_chat_agora=True)

