"""Headless startup check for the actual frozen application, before release."""
from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

from . import __version__


def smoke_test(report_path: str) -> int:
    report = {"version": __version__, "ok": False}
    try:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6 import QtCore, QtGui, QtWidgets
        from .ui import app as app_module

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        widget = QtWidgets.QWidget()
        widget.resize(160, 80)
        widget.setWindowTitle("OpenDownloader startup check")
        widget.show()
        app.processEvents()
        image = widget.grab().toImage()
        if image.isNull() or not callable(app_module.run_gui):
            raise RuntimeError("Qt UI initialization failed")
        widget.close()
        app.processEvents()
        report.update(ok=True, qt=QtCore.qVersion(), image_format=int(QtGui.QImage.Format.Format_RGB32.value))
    except Exception:
        report["error"] = traceback.format_exc()
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
