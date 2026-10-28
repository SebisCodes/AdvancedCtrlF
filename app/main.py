"""GUI entry point."""
from __future__ import annotations

import logging
import sys
import traceback

from . import config as cfg


def main() -> int:
    cfg.ensure_dirs()
    from .diagnostics import setup_logging
    setup_logging()
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        logging.exception("PySide6 cannot be imported")
        print("PySide6 is missing - please start via start.cmd / start.sh (installs everything automatically).")
        return 1

    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Advanced Ctrl+F")

    def excepthook(etype, value, tb):
        logging.error("Unhandled error:\n%s", "".join(traceback.format_exception(etype, value, tb)))
        QMessageBox.critical(None, "Advanced Ctrl+F - Error", f"{etype.__name__}: {value}\n\nDetails: {cfg.LOG_FILE}")

    sys.excepthook = excepthook
    win = MainWindow(app)
    win.show()
    if len(sys.argv) > 1:  # files passed as arguments (e.g. "Send to")
        win.add_paths(sys.argv[1:])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
