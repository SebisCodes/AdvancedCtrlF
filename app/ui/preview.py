"""Hit preview: highlighted text context and a rendered page (PDF/image)."""
from __future__ import annotations

import html

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QScrollArea, QTabWidget, QTextBrowser,
                               QVBoxLayout, QWidget)

from ..search import Hit, SearchIndex

RENDERABLE = {"pdf", "xps", "oxps", "epub", "cbz", "fb2", "mobi", "png", "jpg", "jpeg", "tif", "tiff", "bmp",
              "gif", "webp", "pnm", "jxr", "jp2"}


class Preview(QWidget):
    open_requested = Signal()
    folder_requested = Signal()

    def __init__(self, colors: dict) -> None:
        super().__init__()
        self.colors = colors
        self.ocr = None  # set by the main window
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QWidget(objectName="locBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 8, 8, 8)
        self.title = QLabel("No hit selected", objectName="panelTitle")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.loc = QLabel("", objectName="muted")
        self.btn_open = QPushButton("Open at hit", objectName="primary")
        self.btn_folder = QPushButton("Show in folder")
        self.btn_open.clicked.connect(self.open_requested)
        self.btn_folder.clicked.connect(self.folder_requested)
        bl.addWidget(self.title, 1)
        bl.addWidget(self.loc)
        bl.addSpacing(8)
        bl.addWidget(self.btn_folder)
        bl.addWidget(self.btn_open)
        lay.addWidget(bar)

        self.tabs = QTabWidget()
        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        self.text.document().setDocumentMargin(14)
        self.page_label = QLabel(alignment=Qt.AlignHCenter | Qt.AlignTop)
        self.page_scroll = QScrollArea()
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setWidget(self.page_label)
        self.tabs.addTab(self.text, "Text")
        self.tabs.addTab(self.page_scroll, "Page view")
        lay.addWidget(self.tabs, 1)
        self.clear()

    def clear(self) -> None:
        self.title.setText("No hit selected")
        self.loc.setText("")
        self.text.setHtml("")
        self.page_label.clear()
        self.btn_open.setEnabled(False)
        self.btn_folder.setEnabled(False)
        self.tabs.setTabEnabled(1, False)

    # ---------------------------------------------------------------- Text
    def _chunk_html(self, text: str, spans, strong: bool, anchor: bool) -> str:
        c = self.colors
        out, last, first = [], 0, anchor
        for s, e in spans:
            if s < last:
                continue
            out.append(html.escape(text[last:s]))
            a = '<a name="hit"></a>' if first else ""
            first = False
            out.append(f'{a}<span style="background:{c["hl"]};color:{c["hl_text"]};font-weight:600;">'
                       f'{html.escape(text[s:e])}</span>')
            last = e
        out.append(html.escape(text[last:]))
        body = "".join(out).replace("\n", "<br>")
        color = c["text"] if strong else c["muted"]
        border = f'border-left:3px solid {c["accent"]};' if strong else ""
        return f'<div style="color:{color};{border}padding-left:10px;margin:8px 0;">{body}</div>'

    def show_hit(self, ix: SearchIndex, hit: Hit, location: str, target: dict, stored_path) -> None:
        doc = ix.docs[hit.doc_id]
        chunks = doc["chunks"]
        self.title.setText(doc.get("display_name") or doc["name"])
        self.loc.setText(location)
        self.btn_open.setEnabled(True)
        self.btn_folder.setEnabled(True)
        parts = []
        for ci in range(max(0, hit.chunk - 1), min(len(chunks), hit.chunk + 2)):
            ch = chunks[ci]
            spans = ix.highlights(ch["text"], hit.match_terms)
            parts.append(self._chunk_html(ch["text"], spans, ci == hit.chunk, ci == hit.chunk))
        self.text.setHtml(f'<div style="font-size:11pt;line-height:140%;">{"".join(parts)}</div>')
        self.text.scrollToAnchor("hit")

        kind = (doc.get("kind") or "").lower()
        can_render = kind in RENDERABLE and (target.get("page") or target.get("kind") == "image")
        self.tabs.setTabEnabled(1, bool(can_render))
        if can_render:
            words = {ch[s:e] for ch in [chunks[hit.chunk]["text"]] for s, e in ix.highlights(ch, hit.match_terms)}
            self._render_page(stored_path, int(target.get("page") or 1), words)
        elif self.tabs.currentIndex() == 1:
            self.tabs.setCurrentIndex(0)

    # ---------------------------------------------------------------- Page
    def _render_page(self, path, page_no: int, words: set[str]) -> None:
        try:
            try:
                import pymupdf
            except ImportError:
                import fitz as pymupdf
            with pymupdf.open(str(path)) as doc:
                page = doc[min(max(0, page_no - 1), doc.page_count - 1)]
                ratio = self.devicePixelRatioF()
                avail = max(400, self.page_scroll.viewport().width() - 24)
                zoom = min(2.5, avail / max(1.0, page.rect.width)) * ratio
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                def find(textpage=None):
                    found = []
                    for w in list(words)[:40]:
                        try:
                            found += page.search_for(w, quads=False, textpage=textpage)[:50]
                        except Exception:
                            pass
                    return found

                rects = find()
                if not rects and self.ocr is not None and self.ocr.available():
                    try:  # scanned page or hit inside an image: search an OCR text layer
                        rects = find(self.ocr.textpage(page))
                    except Exception:
                        pass
                img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
            painter = QPainter(img)
            col = QColor(self.colors["hl"] if self.colors["hl"].startswith("#ff") else "#ffd84a")
            col.setAlpha(110)
            painter.setPen(Qt.NoPen)
            painter.setBrush(col)
            for r in rects:
                painter.drawRect(QRectF(r.x0 * zoom, r.y0 * zoom, r.width * zoom, r.height * zoom))
            painter.end()
            pm = QPixmap.fromImage(img)
            pm.setDevicePixelRatio(ratio)
            self.page_label.setPixmap(pm)
            if rects:
                y = max(0, int(rects[0].y0 * zoom / ratio) - 80)
                QTimer.singleShot(0, lambda: self.page_scroll.verticalScrollBar().setValue(y))
        except Exception as e:
            self.page_label.setText(f"Page cannot be displayed: {e}")
