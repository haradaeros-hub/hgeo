def classFactory(iface):
    from .plugin import HGeoPlugin
    return HGeoPlugin(iface)
