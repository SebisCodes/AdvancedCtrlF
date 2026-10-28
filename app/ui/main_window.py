"""Main window of Advanced Ctrl+F."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QSplitter, QStackedWidget, QTableView, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import config as cfg
from .. import opener
from ..ocr import OcrEngine
from ..search import SearchIndex
from ..store import Store
from . import theme
from .preview import Preview
from .results import BarDelegate, HtmlDelegate, ResultsModel
from .workers import JobThread, LoadThread

TYPE_FILTERS = [
    ("All file types", set()),
    ("PDF", {"pdf", "xps", "oxps"}),
    ("Word / text documents", {"docx", "docm", "dotx", "doc", "odt", "ott", "rtf", "pages", "wps", "wpd"}),
    ("Presentations", {"pptx", "pptm", "ppsx", "potx", "ppt", "pps", "odp", "otp", "key"}),
    ("Spreadsheets", {"xlsx", "xlsm", "xltx", "xls", "ods", "ots", "csv", "tsv"}),
    ("Web / XML", {"html", "htm", "xhtml", "mhtml", "mht", "xml", "svg"}),
    ("Images (OCR)", {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp"}),
    ("Text / code / e-mail", {"txt", "md", "log", "json", "eml", "epub"}),
]
OCR_HINT_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".zip",
                ".tar", ".gz", ".tgz", ".xz", ".bz2"}
UNITS = {"pptx": "slides", "pptm": "slides", "odp": "slides", "xlsx": "sheets", "xls": "sheets", "ods": "sheets",
         "csv": "lines", "tsv": "lines", "txt": "lines", "epub": "chapters"}

HELP = """<h3>Searching</h3>
<p>Just type words - the best matches are listed first.<br>
Upper/lower case, umlauts (<i>Größe = Groesse = Grosse</i>) and word forms (<i>contract/contracts</i>)
are handled automatically, as are typos and parts of words (<i>vertrag</i> finds <i>Arbeitsvertrag</i>).</p>
<table cellpadding="3">
<tr><td><b>"exact phrase"</b></td><td>words in exactly this order</td></tr>
<tr><td><b>-word</b></td><td>exclude passages containing this word</td></tr>
<tr><td><b>contr*</b></td><td>word beginning</td></tr>
<tr><td><b>type:pdf</b></td><td>only certain file types (also type:docx,pptx)</td></tr>
<tr><td><b>file:report</b></td><td>only files whose name contains this</td></tr>
</table>
<h3>Documents</h3>
<p>Drag files, folders or ZIP archives into the window. Copies are kept in <code>data/documents</code>,
the extracted text (JSON) in <code>data/index</code>.</p>
<p><b>Double-click</b> a hit to open the document as close to the hit as possible.</p>"""


def dot_icon(color: str) -> QIcon:
    pm = QPixmap(12, 12)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(2, 2, 8, 8)
    p.end()
    return QIcon(pm)


class DropOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("dropOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay = QVBoxLayout(self)
        lbl = QLabel("Drop files here", objectName="dropOverlayLabel", alignment=Qt.AlignCenter)
        lay.addWidget(lbl)
        self.hide()


class MainWindow(QMainWindow):
    def __init__(self, app) -> None:
        super().__init__()
        self.config = cfg.Config()
        self.colors = theme.apply(app)
        self.store = Store(self.config)
        self.ocr = OcrEngine(cfg.TESSDATA_DIR, int(self.config.get("ocr_dpi", 300)), self.config.get("tessdata_url", ""))
        self.index = SearchIndex()
        self.ready = False

        self.setWindowTitle("Advanced Ctrl+F")
        self.setAcceptDrops(True)
        self.resize(1300, 820)
        self._build_ui()
        self._restore_geometry()

        self.jobs = JobThread(self.store, self.config, self.ocr)
        self.jobs.progress.connect(self._on_progress)
        self.jobs.doc_ready.connect(self._on_doc_ready)
        self.jobs.message.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.jobs.idle.connect(self._on_idle)
        self.jobs.start()

        self.search_timer = QTimer(self, singleShot=True, interval=220)
        self.search_timer.timeout.connect(self.run_search)

        self.statusBar().showMessage("Loading index …")
        self.loader = LoadThread(self.store)
        self.loader.loaded.connect(self._on_loaded)
        self.loader.start()

    # ================================================================ UI
    def _build_ui(self) -> None:
        central = QWidget(objectName="central")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- header with search field
        header = QWidget(objectName="header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(18, 12, 18, 12)
        hl.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title_box.addWidget(QLabel("Advanced Ctrl+F", objectName="appTitle"))
        title_box.addWidget(QLabel("Search all your documents", objectName="appSub"))
        hl.addLayout(title_box)
        hl.addSpacing(16)
        self.search = QLineEdit(objectName="search")
        self.search.setPlaceholderText('Search …   e.g.  notice period   "exact phrase"   -without   type:pdf')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        self.search.returnPressed.connect(self.run_search)
        hl.addWidget(self.search, 1)
        self.type_box = QComboBox()
        for label, _ in TYPE_FILTERS:
            self.type_box.addItem(label)
        self.type_box.currentIndexChanged.connect(self.run_search)
        hl.addWidget(self.type_box)
        self.one_per_doc = QCheckBox("1 hit per document")
        self.one_per_doc.setChecked(bool(self.config.get("one_hit_per_doc")))
        self.one_per_doc.toggled.connect(self._toggle_one_per_doc)
        hl.addWidget(self.one_per_doc)
        hl.addWidget(self._settings_button())
        root.addWidget(header)

        # --- main area
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(10)
        split.setContentsMargins(12, 12, 12, 0)
        split.addWidget(self._docs_panel())
        right = QSplitter(Qt.Vertical)
        right.setHandleWidth(10)
        right.addWidget(self._results_panel())
        prev_panel = QWidget(objectName="panel")
        pl = QVBoxLayout(prev_panel)
        pl.setContentsMargins(1, 1, 1, 1)
        self.preview = Preview(self.colors)
        self.preview.ocr = self.ocr
        self.preview.open_requested.connect(self.open_current)
        self.preview.folder_requested.connect(self.show_current_in_folder)
        pl.addWidget(self.preview)
        right.addWidget(prev_panel)
        right.setSizes([460, 340])
        split.addWidget(right)
        split.setSizes([330, 970])
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)
        root.addSpacing(12)
        self.setCentralWidget(central)
        self.splitters = (split, right)

        # --- status bar
        self.progress = QProgressBar(maximumWidth=220, textVisible=False)
        self.progress.hide()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.hide()
        self.cancel_btn.clicked.connect(self._cancel_jobs)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.cancel_btn)

        self.overlay = DropOverlay(central)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: (self.search.setFocus(), self.search.selectAll()))
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.add_files_dialog)
        QShortcut(QKeySequence("Escape"), self, activated=lambda: self.search.clear())

    def _settings_button(self) -> QToolButton:
        btn = QToolButton(text="⚙  Menu")
        btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(btn)
        self.act_ocr = QAction("OCR for scanned PDFs and images", menu, checkable=True,
                               checked=bool(self.config.get("ocr_enabled")))
        self.act_ocr.toggled.connect(lambda v: self._set_cfg("ocr_enabled", v))
        self.act_force = QAction("Always OCR all images inside documents (slower)", menu, checkable=True,
                                 checked=bool(self.config.get("ocr_all_images")))
        self.act_force.toggled.connect(lambda v: self._set_cfg("ocr_all_images", v))
        menu.addAction(self.act_ocr)
        menu.addAction(self.act_force)
        menu.addAction("Download OCR language data (German/English)", self.download_ocr)
        menu.addSeparator()
        menu.addAction("Add files …", self.add_files_dialog)
        menu.addAction("Add folder …", self.add_folder_dialog)
        menu.addAction("Re-read documents with warnings", self.reindex_problematic)
        menu.addAction("Re-read all documents", lambda: self.reindex(list(self.index.docs.values())))
        menu.addSeparator()
        menu.addAction("Choose preferred PDF viewer …", self.choose_pdf_viewer)
        menu.addAction("Open data folder", lambda: opener.show_in_folder(cfg.INDEX_DIR))
        menu.addSeparator()
        menu.addAction("Search help", lambda: QMessageBox.information(self, "Help", HELP))
        btn.setMenu(menu)
        return btn

    def _docs_panel(self) -> QWidget:
        panel = QWidget(objectName="panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 12)
        top = QHBoxLayout()
        self.docs_title = QLabel("Documents", objectName="panelTitle")
        top.addWidget(self.docs_title, 1)
        lay.addLayout(top)
        self.docs = QTreeWidget()
        self.docs.setHeaderLabels(["Name", "Type", "Size"])
        self.docs.setRootIsDecorated(False)
        self.docs.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.docs.setSortingEnabled(True)
        self.docs.sortByColumn(0, Qt.AscendingOrder)
        self.docs.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.docs.header().setStretchLastSection(False)
        self.docs.itemDoubleClicked.connect(self._open_doc_item)
        self.docs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.docs.customContextMenuRequested.connect(self._docs_menu)
        lay.addWidget(self.docs, 1)
        self.docs_empty = QLabel("Drag documents here\n\nPDF · Word · PowerPoint · Excel\nOpenDocument · HTML · CSV · XML\n"
                                 "Images · e-mails · ZIP …", objectName="muted", alignment=Qt.AlignCenter)
        lay.addWidget(self.docs_empty)
        row = QHBoxLayout()
        b_add = QPushButton("+ Files", objectName="primary")
        b_add.clicked.connect(self.add_files_dialog)
        b_dir = QPushButton("+ Folder")
        b_dir.clicked.connect(self.add_folder_dialog)
        b_del = QPushButton("Remove")
        b_del.clicked.connect(self.remove_selected)
        row.addWidget(b_add)
        row.addWidget(b_dir)
        row.addStretch(1)
        row.addWidget(b_del)
        lay.addLayout(row)
        return panel

    def _results_panel(self) -> QWidget:
        panel = QWidget(objectName="panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(1, 8, 1, 1)
        self.results_title = QLabel("", objectName="muted")
        self.results_title.setContentsMargins(12, 0, 12, 4)
        lay.addWidget(self.results_title)
        self.stack = QStackedWidget()
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch(1)
        self.empty_title = QLabel("Welcome!", objectName="emptyTitle", alignment=Qt.AlignCenter)
        self.empty_text = QLabel("", objectName="muted", alignment=Qt.AlignCenter, wordWrap=True)
        el.addWidget(self.empty_title)
        el.addWidget(self.empty_text)
        el.addStretch(2)
        self.stack.addWidget(empty)

        self.model = ResultsModel(self.colors)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setItemDelegateForColumn(0, BarDelegate(self.colors, self.table))
        self.table.setItemDelegateForColumn(3, HtmlDelegate(self.table))
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Fixed)
        h.resizeSection(0, 90)
        h.setSectionResizeMode(1, QHeaderView.Interactive)
        h.resizeSection(1, 230)
        h.setSectionResizeMode(2, QHeaderView.Interactive)
        h.resizeSection(2, 130)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        h.setHighlightSections(False)
        self.table.selectionModel().currentRowChanged.connect(self._on_row)
        self.table.doubleClicked.connect(self.open_current)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._results_menu)
        self.stack.addWidget(self.table)
        lay.addWidget(self.stack, 1)
        self._update_empty()
        return panel

    # ================================================================ Drag & drop
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.overlay.setGeometry(self.centralWidget().rect().adjusted(16, 16, -16, -16))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()
            self.overlay.setGeometry(self.centralWidget().rect().adjusted(16, 16, -16, -16))
            self.overlay.show()
            self.overlay.raise_()

    def dragLeaveEvent(self, e):
        self.overlay.hide()

    def dropEvent(self, e):
        self.overlay.hide()
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            e.acceptProposedAction()
            self.add_paths(paths)

    # ================================================================ Documents
    def add_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add documents")
        if files:
            self.add_paths(files)

    def add_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Add folder")
        if d:
            self.add_paths([d])

    def add_paths(self, paths: list[str]) -> None:
        paths = [p for p in paths if Path(p).exists()]
        if not paths:
            return
        self._maybe_offer_ocr(paths)
        self.jobs.submit("add", paths)
        self._busy(True)

    def _maybe_offer_ocr(self, paths) -> None:
        if not self.config.get("ocr_enabled") or self.ocr.available() or self.config.get("ocr_download_declined"):
            return
        if not any(Path(p).is_dir() or Path(p).suffix.lower() in OCR_HINT_EXT for p in paths):
            return
        r = QMessageBox.question(
            self, "Text recognition (OCR)",
            "Reading scanned PDFs and images needs OCR language data for German and English "
            "(one-time download of about 6 MB from github.com/tesseract-ocr).\n\nDownload it now?")
        if r == QMessageBox.Yes:
            self.jobs.submit("ocr_download", None)
        else:
            self._set_cfg("ocr_download_declined", True)
            self.statusBar().showMessage("OCR skipped - you can download the data any time via ⚙ Menu", 8000)

    def download_ocr(self):
        self._set_cfg("ocr_download_declined", False)
        self.jobs.submit("ocr_download", None)
        self._busy(True)

    def reindex(self, docs: list[dict]) -> None:
        docs = [d for d in docs if d.get("id")]
        if docs:
            self.jobs.submit("reindex", docs)
            self._busy(True)

    def reindex_problematic(self):
        self.reindex([d for d in self.index.docs.values() if d.get("status") != "ok"])

    def selected_docs(self) -> list[dict]:
        return [self.index.docs[i.data(0, Qt.UserRole)] for i in self.docs.selectedItems()
                if i.data(0, Qt.UserRole) in self.index.docs]

    def remove_selected(self):
        docs = self.selected_docs()
        if not docs:
            self.statusBar().showMessage("Select documents in the list first", 5000)
            return
        names = "\n".join(d["display_name"] for d in docs[:10]) + ("\n…" if len(docs) > 10 else "")
        if QMessageBox.question(self, "Remove", f"Remove {len(docs)} document(s) from Advanced Ctrl+F?\n\n{names}\n\n"
                                "(Your original files are not touched.)") != QMessageBox.Yes:
            return
        for d in docs:
            self.store.remove(d)
            self.index.remove(d["id"])
        self._refresh_docs()
        self.run_search()

    def _docs_menu(self, pos):
        docs = self.selected_docs()
        if not docs:
            return
        m = QMenu(self)
        m.addAction("Open", lambda: self._open_doc(docs[0], None))
        m.addAction("Show in folder", lambda: opener.show_in_folder(self.store.stored_path(docs[0])))
        m.addAction("Search only in this document", lambda: self._search_in(docs[0]))
        m.addAction("Re-read", lambda: self.reindex(docs))
        m.addSeparator()
        m.addAction("Remove …", self.remove_selected)
        m.exec(self.docs.viewport().mapToGlobal(pos))

    def _search_in(self, doc):
        name = doc["name"].split(" ")[0]
        text = " ".join(w for w in self.search.text().split() if not w.lower().startswith(("file:", "datei:")))
        self.search.setText(f'{text} file:{name}'.strip())

    def _refresh_docs(self) -> None:
        self.docs.setSortingEnabled(False)
        self.docs.clear()
        c = self.colors
        for d in self.index.docs.values():
            pages = d.get("pages")
            kind = (d.get("kind") or "").lower()
            unit = UNITS.get(kind, "pages")
            count = pages or len(d.get("chunks") or [])
            unit = unit if pages else "sections"
            if count == 1:
                unit = unit[:-1]  # "1 page", "1 slide", "1 section"
            amount = f"{'~' if pages and d.get('pages_approx') else ''}{count} {unit}"
            it = QTreeWidgetItem([d.get("display_name") or d["name"], kind.upper(), amount])
            it.setData(0, Qt.UserRole, d["id"])
            status = d.get("status", "ok")
            it.setIcon(0, dot_icon(c["ok"] if status == "ok" else c["warn"] if status == "warn" else c["err"]))
            tip = [d.get("display_name", ""), f"Type: {kind}", f"Language: {d.get('language', '?')}"]
            if d.get("ocr_pages"):
                tip.append(f"OCR: {d['ocr_pages']} scanned page(s)")
            if d.get("ocr_images"):
                tip.append(f"OCR: {d['ocr_images']} image(s)")
            if d.get("message"):
                tip.append(d["message"])
            it.setToolTip(0, "\n".join(tip))
            self.docs.addTopLevelItem(it)
        self.docs.setSortingEnabled(True)
        n = len(self.index.docs)
        self.docs_title.setText(f"Documents ({n})")
        self.docs_empty.setVisible(n == 0)
        self.docs.setVisible(n > 0)
        self.docs.resizeColumnToContents(1)
        self.docs.resizeColumnToContents(2)
        self._update_empty()

    # ================================================================ Search
    def _toggle_one_per_doc(self, v):
        self._set_cfg("one_hit_per_doc", v)
        self.run_search()

    def run_search(self):
        self.search_timer.stop()
        if not self.ready:
            return
        text = self.search.text().strip()
        if not text:
            self.model.set_hits([], self.index)
            self.preview.clear()
            self._update_empty()
            return
        kinds = TYPE_FILTERS[self.type_box.currentIndex()][1]
        limit = int(self.config.get("max_results", 300))
        hits, q = self.index.search(text, kinds=set(kinds), limit=limit, one_per_doc=self.one_per_doc.isChecked())
        self.model.set_hits(hits, self.index)
        if hits:
            ndocs = len({h.doc_id for h in hits})
            self.results_title.setText(f"{len(hits)} hit(s) in {ndocs} document(s)"
                                       + (" (limited)" if len(hits) >= limit else ""))
            self.stack.setCurrentIndex(1)
            self.table.selectRow(0)
        else:
            self.results_title.setText("")
            self.preview.clear()
            self.empty_title.setText("No hits")
            self.empty_text.setText("Try a different spelling or fewer words.\n"
                                    "Tip: use * for word beginnings (e.g. contr*) and \"…\" for phrases.")
            self.stack.setCurrentIndex(0)

    def _update_empty(self):
        if self.search.text().strip() and self.model.rowCount():
            return
        self.stack.setCurrentIndex(0)
        self.results_title.setText("")
        if not self.ready:
            self.empty_title.setText("Loading …")
            self.empty_text.setText("")
        elif not self.index.docs:
            self.empty_title.setText("Drop documents here")
            self.empty_text.setText("Drag files, folders or ZIP archives into this window,\n"
                                    "then type a search term at the top.")
        else:
            self.empty_title.setText("What are you looking for?")
            self.empty_text.setText(f"{len(self.index.docs)} documents ready. Type a search term above (Ctrl+F).\n"
                                    '"phrase"  ·  -exclude  ·  word*  ·  type:pdf  ·  file:name')

    def _on_row(self, current, _prev=None):
        if not current.isValid():
            return
        row = current.row()
        hit = self.model.hits[row]
        _, loc_text, target, _ = self.model.info(row)
        self.preview.show_hit(self.index, hit, loc_text, target, self.store.stored_path(self.index.docs[hit.doc_id]))

    def _current(self):
        idx = self.table.currentIndex()
        if not idx.isValid() or self.stack.currentIndex() != 1:
            return None
        row = idx.row()
        return self.model.hits[row], self.model.info(row)

    def _hit_word(self, hit, offset: int) -> str:
        """The highlighted word closest to the snippet position (used to jump to the hit in web pages)."""
        text = self.index.chunk_of(hit)["text"]
        spans = self.index.highlights(text, hit.match_terms)
        if not spans:
            return ""
        s, e = min(spans, key=lambda sp: abs(sp[0] - offset))
        return text[s:e]

    def open_current(self, *_):
        cur = self._current()
        if cur:
            hit, (_, loc_text, target, offset) = cur
            self._open_doc(self.index.docs[hit.doc_id], target, loc_text, self._hit_word(hit, offset))

    def show_current_in_folder(self):
        cur = self._current()
        if cur:
            opener.show_in_folder(self.store.stored_path(self.index.docs[cur[0].doc_id]))

    def _open_doc_item(self, item, _col):
        did = item.data(0, Qt.UserRole)
        if did in self.index.docs:
            self._open_doc(self.index.docs[did], None)

    def _open_doc(self, doc, target, loc_text: str = "", highlight: str = ""):
        ok, msg = opener.open_document(self.store.stored_path(doc), target, self.config.get("pdf_viewer", ""),
                                       highlight=highlight)
        if loc_text:
            msg += f"  ·  Location: {loc_text}"
        self.statusBar().showMessage(msg, 10000)
        if not ok:
            QMessageBox.warning(self, "Open", msg)

    def _results_menu(self, pos):
        cur = self._current()
        if not cur:
            return
        hit, (_, loc_text, target, _) = cur
        doc = self.index.docs[hit.doc_id]
        m = QMenu(self)
        m.addAction("Open at hit", self.open_current)
        m.addAction("Show in folder", self.show_current_in_folder)
        m.addAction("Copy location", lambda: self._copy(f"{doc.get('display_name')} – {loc_text}"))
        m.addAction("Copy text passage", lambda: self._copy(self.index.chunk_of(hit)["text"]))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _copy(self, text):
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage("Copied to clipboard", 3000)

    # ================================================================ Background work
    def _on_loaded(self, ix):
        self.index = ix
        self.ready = True
        self._refresh_docs()
        self.statusBar().showMessage(f"{len(ix.docs)} documents loaded", 4000)
        self.search.setFocus()
        if self.search.text().strip():
            self.run_search()

    def _on_doc_ready(self, doc):
        if doc.get("duplicate"):
            self.statusBar().showMessage(f"{doc['name']} is already in the list", 4000)
            return
        if not doc.get("id"):
            self.statusBar().showMessage(f"{doc.get('name')}: {doc.get('message')}", 8000)
            return
        self.index.add(doc)
        self._refresh_docs()
        if self.search.text().strip():
            self.search_timer.start()

    def _on_progress(self, text, done, total):
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        self.statusBar().showMessage(text)

    def _on_idle(self, summary, failed):
        self._busy(False)
        if failed:
            summary += " - hover over the red dots in the list for details"
        self.statusBar().showMessage(summary, 10000)

    def _busy(self, on: bool):
        self.progress.setVisible(on)
        self.cancel_btn.setVisible(on)
        if on:
            self.progress.setValue(0)

    def _cancel_jobs(self):
        self.jobs.cancel()
        self.statusBar().showMessage("Cancelling …")

    # ================================================================ Settings
    def _set_cfg(self, key, value):
        self.config[key] = value
        self.config.save()

    def choose_pdf_viewer(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose PDF viewer (e.g. SumatraPDF, Acrobat, Okular)")
        self._set_cfg("pdf_viewer", f or "")

    def _restore_geometry(self):
        w = self.config.get("window") or {}
        if w.get("geometry"):
            self.restoreGeometry(QByteArray.fromBase64(w["geometry"].encode()))

    def closeEvent(self, e):
        self.config["window"] = {"geometry": bytes(self.saveGeometry().toBase64()).decode()}
        self.config.save()
        self.jobs.stop()
        self.jobs.wait(3000)
        super().closeEvent(e)

    def sizeHint(self):
        return QSize(1300, 820)
