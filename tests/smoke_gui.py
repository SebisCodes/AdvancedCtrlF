"""GUI smoke test without a screen: start the main window, add the sample files,
wait until they are read, search, and build the diagnostics report.

Run:  QT_QPA_PLATFORM=offscreen python -I tests/smoke_gui.py      (exit code 0 = OK)
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import config as cfg  # noqa: E402

tmp = Path(tempfile.mkdtemp())
tessdata = cfg.TESSDATA_DIR                        # keep using downloaded OCR data, if any
cfg.DOCS_DIR, cfg.INDEX_DIR, cfg.CONFIG_DIR = tmp / "documents", tmp / "index", tmp / "config"
cfg.CONFIG_FILE, cfg.LOG_FILE = cfg.CONFIG_DIR / "config.json", tmp / "app.log"
cfg.TESSDATA_DIR = tessdata
cfg.DEFAULTS["auto_update"] = False                # no network access during the test

import samples  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)   # never block on dialogs
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)

from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.ocr_languages import LanguagePacksDialog  # noqa: E402

LanguagePacksDialog.exec = lambda self: LanguagePacksDialog.Rejected   # never block on the pack dialog

samples.make_all(tmp / "in")
app = QApplication([])
win = MainWindow(app)
win.show()
result = {"ok": False, "msg": "timeout"}


def check():
    win.search.setText("Probezeit")
    win.run_search()
    hits = win.model.rowCount()
    report = __import__("app.diagnostics", fromlist=["report"]).report(win.config, win.ocr, win.index.docs)
    ok = len(win.index.docs) >= 10 and hits >= 1 and "Advanced Ctrl+F" in report
    result.update(ok=ok, msg=f"documents={len(win.index.docs)} hits={hits} report_lines={len(report.splitlines())}")
    app.quit()


def wait_for_import(tries=[0]):
    tries[0] += 1
    if win.progress.isVisible() and tries[0] < 600:
        QTimer.singleShot(200, wait_for_import)
    else:
        check()


QTimer.singleShot(500, lambda: (win.add_paths([str(tmp / "in")]), QTimer.singleShot(500, wait_for_import)))
QTimer.singleShot(180_000, app.quit)
app.exec()
win.jobs.stop()
win.jobs.wait(3000)
shutil.rmtree(tmp, ignore_errors=True)
print(("OK: " if result["ok"] else "FAILED: ") + result["msg"])
sys.exit(0 if result["ok"] else 1)
