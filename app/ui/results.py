"""Results table: model + delegates (relevance bar, highlighted text snippet)."""
from __future__ import annotations

import html

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, QSize, Qt
from PySide6.QtGui import QAbstractTextDocumentLayout, QColor, QPainter, QPalette, QTextDocument
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..search import MEANING_FLOOR, Hit, SearchIndex, format_location, target_of

COLS = ("Relevance", "Document", "Location", "Text")


def highlight_html(text: str, spans, c: dict) -> str:
    out, last = [], 0
    for s, e in spans:
        if s < last:
            continue
        out.append(html.escape(text[last:s]))
        out.append(f'<span style="background:{c["hl"]};color:{c["hl_text"]};">{html.escape(text[s:e])}</span>')
        last = e
    out.append(html.escape(text[last:]))
    return "".join(out).replace("\n", " ⏎ ")


class ResultsModel(QAbstractTableModel):
    def __init__(self, colors: dict) -> None:
        super().__init__()
        self.hits: list[Hit] = []
        self.index_: SearchIndex | None = None
        self.colors = colors
        self._cache: dict[int, tuple] = {}

    def set_hits(self, hits: list[Hit], index: SearchIndex) -> None:
        self.beginResetModel()
        self.hits, self.index_, self._cache = hits, index, {}
        self.endResetModel()

    def info(self, row: int) -> tuple:
        """(snippet HTML, location text, target dict, offset) - computed lazily."""
        if row not in self._cache:
            hit = self.hits[row]
            snip, spans, off = self.index_.snippet(hit)
            ch = self.index_.chunk_of(hit)
            target = target_of(ch["loc"], ch["text"], off)
            self._cache[row] = (highlight_html(snip, spans, self.colors), format_location(target), target, off)
        return self._cache[row]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.hits)

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        hit = self.hits[index.row()]
        col = index.column()
        doc = self.index_.docs[hit.doc_id]
        if role == Qt.DisplayRole:
            if col == 0:
                return hit.relevance
            if col == 1:
                return doc.get("display_name") or doc.get("name")
            if col == 2:
                return self.info(index.row())[1]
            if col == 3:
                return self.info(index.row())[0]
        if role == Qt.ToolTipRole:
            if col == 1:
                return f"{doc.get('display_name')}\nType: {doc.get('kind')}"
            if col == 0:
                tip = f"Relevance {hit.relevance} % · matches {round(hit.coverage * 100)} % of the search words"
                if hit.meaning >= MEANING_FLOOR:
                    tip += f"\nSimilar in meaning: {round(hit.meaning * 100)} %"
                    if not hit.coverage:
                        tip += " - found by meaning, without the search words"
                return tip
        return None


class BarDelegate(QStyledItemDelegate):
    def __init__(self, colors: dict, parent=None) -> None:
        super().__init__(parent)
        self.colors = colors

    def paint(self, painter: QPainter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.widget.style().drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        val = int(index.data() or 0)
        r = option.rect.adjusted(8, option.rect.height() // 2 - 4, -8, -(option.rect.height() // 2 - 4))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self.colors["border"]))
        painter.drawRoundedRect(r, 4, 4)
        fill = QRectF(r)
        fill.setWidth(max(6, r.width() * val / 100))
        col = QColor(self.colors["accent"])
        col.setAlphaF(0.35 + 0.65 * val / 100)
        painter.setBrush(col)
        painter.drawRoundedRect(fill, 4, 4)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(90, 24)


class HtmlDelegate(QStyledItemDelegate):
    """Renders the text snippet with highlights (max. 2 lines)."""

    def paint(self, painter: QPainter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        html_text = opt.text
        opt.text = ""
        opt.widget.style().drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(2)
        doc.setHtml(html_text)
        doc.setTextWidth(opt.rect.width() - 8)
        painter.save()
        dy = max(2, (opt.rect.height() - doc.size().height()) / 2)
        painter.translate(opt.rect.left() + 4, opt.rect.top() + dy)
        clip = QRectF(0, 0, opt.rect.width() - 8, opt.rect.height() - 4)
        painter.setClipRect(clip)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette.setColor(QPalette.Text, opt.palette.color(QPalette.Text))
        ctx.clip = clip
        doc.documentLayout().draw(painter, ctx)
        painter.restore()
