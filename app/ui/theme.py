"""Colors and stylesheet (light/dark follows the system setting)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

LIGHT = {
    "bg": "#f4f5f7", "surface": "#ffffff", "surface2": "#f9fafb", "border": "#dde1e6", "text": "#1d2128",
    "muted": "#68707c", "accent": "#2f6fed", "accent_text": "#ffffff", "accent_soft": "#e3ecfd",
    "hl": "#ffe58a", "hl_text": "#1d2128", "ok": "#2e9d5b", "warn": "#d99a1e", "err": "#d64545",
}
DARK = {
    "bg": "#15171c", "surface": "#1d2027", "surface2": "#22262e", "border": "#2f343e", "text": "#e5e8ec",
    "muted": "#9aa2ad", "accent": "#5b8ff9", "accent_text": "#0d1117", "accent_soft": "#22355a",
    "hl": "#8a6a12", "hl_text": "#fff6d6", "ok": "#4cc07f", "warn": "#e5b04a", "err": "#ef6b6b",
}


def is_dark() -> bool:
    hints = QGuiApplication.styleHints()
    try:
        scheme = hints.colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except AttributeError:
        pass
    return QGuiApplication.palette().color(QPalette.Window).lightness() < 128


def colors() -> dict:
    return DARK if is_dark() else LIGHT


def apply(app) -> dict:
    c = colors()
    app.setStyle("Fusion")
    pal = QPalette()
    for role, key in ((QPalette.Window, "bg"), (QPalette.Base, "surface"), (QPalette.AlternateBase, "surface2"),
                      (QPalette.Text, "text"), (QPalette.WindowText, "text"), (QPalette.Button, "surface"),
                      (QPalette.ButtonText, "text"), (QPalette.Highlight, "accent"),
                      (QPalette.HighlightedText, "accent_text"), (QPalette.ToolTipBase, "surface"),
                      (QPalette.ToolTipText, "text"), (QPalette.PlaceholderText, "muted")):
        pal.setColor(role, QColor(c[key]))
    app.setPalette(pal)
    app.setStyleSheet(QSS.format(**c))
    return c


QSS = """
QMainWindow, QWidget#central {{ background: {bg}; }}
QWidget {{ color: {text}; font-size: 10pt; }}
QToolTip {{ background: {surface}; color: {text}; border: 1px solid {border}; padding: 4px; }}
#header {{ background: {surface}; border-bottom: 1px solid {border}; }}
#appTitle {{ font-size: 15pt; font-weight: 700; color: {accent}; }}
#appSub {{ color: {muted}; }}
QLineEdit#search {{
    font-size: 13pt; padding: 9px 14px; border: 2px solid {border}; border-radius: 10px; background: {surface2};
}}
QLineEdit#search:focus {{ border-color: {accent}; background: {surface}; }}
QComboBox, QLineEdit {{ padding: 5px 8px; border: 1px solid {border}; border-radius: 6px; background: {surface}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QPushButton, QToolButton {{
    padding: 6px 12px; border: 1px solid {border}; border-radius: 6px; background: {surface};
}}
QPushButton:hover, QToolButton:hover {{ border-color: {accent}; }}
QPushButton:disabled {{ color: {muted}; }}
QPushButton#primary {{ background: {accent}; color: {accent_text}; border-color: {accent}; font-weight: 600; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
#panel {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; }}
#panelTitle {{ font-weight: 700; padding: 2px 0; }}
#muted {{ color: {muted}; }}
QTreeWidget, QTableView, QTextBrowser, QScrollArea {{
    background: {surface}; border: none; selection-background-color: {accent_soft}; selection-color: {text};
}}
QTableView {{ gridline-color: transparent; alternate-background-color: {surface2}; }}
QTableView::item, QTreeWidget::item {{ padding: 3px 4px; }}
QTableView::item:selected, QTreeWidget::item:selected {{ background: {accent_soft}; color: {text}; }}
QHeaderView::section {{
    background: {surface}; color: {muted}; border: none; border-bottom: 1px solid {border};
    padding: 6px 6px; font-weight: 600;
}}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ padding: 6px 14px; border: none; color: {muted}; background: transparent; }}
QTabBar::tab:selected {{ color: {accent}; border-bottom: 2px solid {accent}; }}
QSplitter::handle {{ background: transparent; }}
QStatusBar {{ background: {surface}; border-top: 1px solid {border}; color: {muted}; }}
QProgressBar {{ border: 1px solid {border}; border-radius: 5px; background: {surface2}; height: 10px; text-align: center; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}
#dropOverlay {{ background: rgba(47,111,237,0.12); border: 3px dashed {accent}; border-radius: 16px; }}
#dropOverlayLabel {{ font-size: 18pt; font-weight: 700; color: {accent}; background: transparent; }}
#emptyTitle {{ font-size: 16pt; font-weight: 700; }}
#locBar {{ background: {surface2}; border-bottom: 1px solid {border}; }}
QMenu {{ background: {surface}; border: 1px solid {border}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {accent_soft}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
"""
