"""Local MCP server extension for RenderDoc."""

import atexit

from .mcp_server import ExtensionService
import qrenderdoc as qrd
import traceback

try:
    import shiboken2
    from PySide2 import QtWidgets
except ImportError:
    shiboken2 = None
    QtWidgets = None


_service = None
_status_bar = None
_status_widget = None
_status_label = None
_atexit_registered = globals().get("_atexit_registered", False)


def _update_status(state, endpoint):
    if _status_label is None:
        return
    if state == "running":
        style = (
            "QLabel { background-color: #238636; border-radius: 5px; }"
        )
        tooltip = "RenderDoc MCP is listening at {}".format(endpoint)
    else:
        style = (
            "QLabel { background-color: #cf222e; border-radius: 5px; }"
        )
        if state == "error":
            tooltip = "RenderDoc MCP failed to start"
        else:
            tooltip = "RenderDoc MCP is stopped"
    _status_label.setStyleSheet(style)
    _status_label.setToolTip(tooltip)
    if _status_widget is not None:
        _status_widget.setToolTip(tooltip)


def _install_status_widget(pyrenderdoc):
    global _status_bar, _status_widget, _status_label
    _remove_status_widget()
    if QtWidgets is None or shiboken2 is None:
        print("RenderDoc MCP status widget unavailable because PySide2 is not loaded.")
        return
    try:
        widget = pyrenderdoc.GetMainWindow().Widget()
        pointer = shiboken2.getCppPointer(widget)[0]
        main_window = shiboken2.wrapInstance(pointer, QtWidgets.QMainWindow)
        status_bar = main_window.statusBar()
        status_widget = QtWidgets.QWidget(status_bar)
        layout = QtWidgets.QHBoxLayout(status_widget)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(3)
        prefix = QtWidgets.QLabel("MCP:", status_widget)
        label = QtWidgets.QLabel(status_widget)
        label.setObjectName("renderdocMcpStatus")
        label.setFixedSize(10, 10)
        layout.addWidget(prefix)
        layout.addWidget(label)
        status_bar.addPermanentWidget(status_widget)
        _status_bar = status_bar
        _status_widget = status_widget
        _status_label = label
        _update_status("stopped", "stopped")
    except Exception:
        print("RenderDoc MCP status widget setup failed:\n{}".format(traceback.format_exc()))


def _remove_status_widget():
    global _status_bar, _status_widget, _status_label
    if _status_bar is not None and _status_widget is not None:
        try:
            _status_bar.removeWidget(_status_widget)
            _status_widget.deleteLater()
        except RuntimeError:
            pass
    _status_bar = None
    _status_widget = None
    _status_label = None


def _start(context, data):
    global _service
    if _service is None:
        _service = ExtensionService(context, _update_status)
    _service.start()


def _stop(context, data):
    if _service is not None:
        _service.stop()


def register(version, pyrenderdoc):
    global _service
    _service = ExtensionService(pyrenderdoc, _update_status)
    _install_status_widget(pyrenderdoc)
    manager = pyrenderdoc.Extensions()
    manager.RegisterWindowMenu(
        qrd.WindowMenu.Tools,
        ["RenderDoc MCP", "Start"],
        _start,
    )
    manager.RegisterWindowMenu(
        qrd.WindowMenu.Tools,
        ["RenderDoc MCP", "Stop"],
        _stop,
    )

    print("RenderDoc MCP loaded. Use Tools > RenderDoc MCP > Start.")


def unregister():
    global _service
    if _service is not None:
        _service.stop()
        _service = None
    _remove_status_widget()


def _shutdown_at_exit():
    try:
        unregister()
    except Exception:
        pass


if not _atexit_registered:
    atexit.register(_shutdown_at_exit)
    _atexit_registered = True
