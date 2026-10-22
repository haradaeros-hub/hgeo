
from qgis.PyQt.QtWidgets import QAction, QDialog
from qgis.PyQt.QtCore import QThread
from qgis.core import QgsApplication, QgsMessageLog, Qgis


class HGeoPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self.runtime = None
        self.windows = []

    def initGui(self):
        if self.actions:
            return
        for title, callback in (
            ('Abrir H-GEO', self.run),
            ('Modo Operacional', lambda: self.open_mode('operacional')),
            ('Modo Analista', lambda: self.open_mode('analista')),
            ('Relatório comparativo', self.report),
        ):
            action = QAction(title, self.iface.mainWindow())
            action.triggered.connect(lambda checked=False, fn=callback: self._invoke(fn))
            self.iface.addPluginToMenu('&H-GEO', action)
            self.actions.append(action)
        self.actions[0].setIcon(QgsApplication.getThemeIcon('/mActionShowAllLayers.svg'))
        self.iface.addToolBarIcon(self.actions[0])

    def _invoke(self, callback):
        try:
            callback()
        except Exception:
            import traceback
            QgsMessageLog.logMessage(traceback.format_exc(), 'H-GEO', Qgis.Critical)
            self.iface.messageBar().pushCritical('H-GEO', 'Não foi possível abrir a ferramenta. Consulte o painel de mensagens.')

    def _load(self):
        if self.runtime is None:
            from . import legacy
            self.runtime = legacy
        return self.runtime

    def _show(self, window):
        if window not in self.windows:
            self.windows.append(window)
        window.show()
        window.raise_()
        window.activateWindow()

    def run(self):
        rt = self._load()
        if rt._assistente_welcome is None:
            rt._assistente_welcome = rt.WelcomeDialog(self.iface.mainWindow())
        self._show(rt._assistente_welcome)

    def open_mode(self, mode):
        rt = self._load()
        rt._modo_atual = mode
        rt.spawn_chat_button(abrir_chat_agora=True)

    def report(self):
        self._show(self._load().abrir_relatorio_comparativo_hgeo(self.iface.mainWindow()))

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu('&H-GEO', action)
            self.iface.removeToolBarIcon(action)
            action.deleteLater()
        self.actions.clear()
        if self.runtime is None:
            return
        
        
        rt = self.runtime
        roots = list(self.windows)
        roots.extend(rt._hgeo_previous_chats)
        roots.extend(chat.chat_window for chat in rt._hgeo_previous_chats)
        for name in ('_assistente_chat', '_assistente_welcome', '_hgr_report_dialog'):
            window = getattr(rt, name, None)
            if window is not None and window not in roots:
                roots.append(window)
        if rt._assistente_chat is not None:
            roots.append(rt._assistente_chat.chat_window)
        windows = []
        for root in roots:
            windows.extend(root.findChildren(QDialog))
            windows.append(root)
        for window in set(windows):
            window.hide()
            for worker in window.findChildren(QThread):
                if worker.isRunning():
                    worker.requestInterruption()
        self.windows.clear()
