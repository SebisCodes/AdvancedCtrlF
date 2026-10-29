"""Main window of Advanced Ctrl+F."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QSplitter, QStackedWidget, QStyle, QTableView, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import __version__
from .. import config as cfg
from .. import diagnostics, opener, remote
from ..ocr import OcrEngine
from .. import semantic
from ..search import SearchIndex, folder_of, set_stemming_off
from ..store import Store
from . import theme
from .ocr_languages import LanguagePacksDialog
from .preview import Preview
from .results import BarDelegate, HtmlDelegate, ResultsModel
from .workers import JobThread, LoadThread, MeaningThread, UpdateWorker

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
OCR_HINT_EXT = {".pdf", ".xps", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp",
                ".docx", ".docm", ".pptx", ".pptm", ".ppsx", ".xlsx", ".xlsm", ".odt", ".odp", ".ods",
                ".zip", ".tar", ".gz", ".tgz", ".xz", ".bz2"}
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
<p>Drag files, folders, ZIP archives or links into the window - any file type works (files without
text can be found by name). Folders that are Git repositories are added as Git sees them (<code>.gitignore</code>
respected); <code>node_modules</code>, <code>.git</code>, caches etc. are skipped. Public GitHub repositories,
branches, folders or files: <i>⚙ Menu → Add from link</i> or drag the link from the browser.
Copies are kept in <code>data/documents</code>,
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


FOLDER_SEP = "\x1f"   # joins folder parts into one key for the folder list
FOLDER_ROLE = Qt.UserRole + 1
RESTART_SECONDS = 10   # countdown before restarting after an update


class DocItem(QTreeWidgetItem):
    """Row of the document list; folders sort before documents."""

    def __lt__(self, other):
        a, b = self.data(0, FOLDER_ROLE) is not None, other.data(0, FOLDER_ROLE) is not None
        if a != b:
            return a
        col = self.treeWidget().sortColumn() if self.treeWidget() else 0
        return self.text(col).casefold() < other.text(col).casefold()

class MainWindow(QMainWindow):
    def __init__(self, app) -> None:
        super().__init__()
        self.config = cfg.Config()
        self.colors = theme.apply(app)
        self.store = Store(self.config)
        self.ocr = OcrEngine(cfg.TESSDATA_DIR, int(self.config.get("ocr_dpi", 300)), self.config.get("tessdata_url", ""))
        set_stemming_off(self.config.get("stemming_off") or [])
        self.index = SearchIndex()
        self.meaning_thread: MeaningThread | None = None
        self.update_thread: UpdateWorker | None = None
        self._update_manual = False
        self._notice_shown = False
        self._asking_update = False
        self._staged_offered = False
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
        self.jobs.ocr_installed.connect(self._on_ocr_installed)
        self.jobs.ocr_failed.connect(self._on_ocr_failed)
        self.jobs.meaning_installed.connect(self._start_meaning)
        self.jobs.meaning_failed.connect(self._on_meaning_failed)
        self.jobs.start()

        self.search_timer = QTimer(self, singleShot=True, interval=220)
        self.search_timer.timeout.connect(self.run_search)

        self.statusBar().showMessage("Loading index …")
        self._load_index()

    def _load_index(self) -> None:
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
        self.scope_box = QComboBox()
        self.scope_box.setToolTip("Search only in this folder (and its subfolders) - or type folder:name")
        self.scope_box.setMinimumContentsLength(14)
        self.scope_box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.scope_box.addItem("All folders", "")
        self.scope_box.currentIndexChanged.connect(self.run_search)
        hl.addWidget(self.scope_box)
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
        menu.addAction("Languages and packs …", self.manage_ocr_packs)
        menu.addSeparator()
        menu.addAction("Add files …", self.add_files_dialog)
        menu.addAction("Add folder …", self.add_folder_dialog)
        menu.addAction("Add from link / GitHub repository …", self.add_url_dialog)
        menu.addAction("Re-read documents with warnings", self.reindex_problematic)
        menu.addAction("Re-read all documents", lambda: self.reindex(list(self.index.docs.values())))
        menu.addSeparator()
        menu.addAction("Choose preferred PDF viewer …", self.choose_pdf_viewer)
        menu.addAction("Open data folder", lambda: opener.show_in_folder(cfg.INDEX_DIR))
        menu.addSeparator()
        menu.addAction("Open log file", self.open_log)
        menu.addAction("Copy diagnostics (for support)", self.copy_diagnostics)
        menu.addSeparator()
        self.act_update = QAction("Automatic updates", self, checkable=True)
        self.act_update.setChecked(bool(self.config.get("auto_update", True)))
        self.act_update.setToolTip("Look for a new version once a day, install it and restart")
        self.act_update.toggled.connect(lambda v: self._set_cfg("auto_update", v))
        menu.addAction(self.act_update)
        menu.addAction("Check for updates now", lambda: self.check_updates(manual=True))
        menu.addSeparator()
        menu.addAction("Search help", lambda: QMessageBox.information(self, "Help", HELP))
        menu.addAction(f"About Advanced Ctrl+F {__version__}", self.show_about)
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
        self.docs.setRootIsDecorated(True)
        self.docs.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.docs.setSortingEnabled(True)
        self.docs.sortByColumn(0, Qt.AscendingOrder)
        self.docs.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.docs.header().setStretchLastSection(False)
        self.docs.itemDoubleClicked.connect(self._open_doc_item)
        self.docs.itemClicked.connect(self._doc_item_clicked)
        self._expanded: set[str] | None = None      # folder keys open in the list (None = not filled yet)
        self.docs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.docs.customContextMenuRequested.connect(self._docs_menu)
        lay.addWidget(self.docs, 1)
        self.docs_empty = QLabel("Drag documents here\n\nPDF · Word · PowerPoint · Excel\nOpenDocument · HTML · CSV · XML\n"
                                 "Images · e-mails · code · ZIP …\nany other file (by name)\n\nor a link / GitHub repository", objectName="muted", alignment=Qt.AlignCenter)
        lay.addWidget(self.docs_empty, 1)  # takes the free space while the list is hidden, title stays on top
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

    @staticmethod
    def _web_links(mime) -> list[str]:
        links = [u.toString() for u in mime.urls() if u.scheme() in ("https", "http")] if mime.hasUrls() else []
        if not links and mime.hasText():
            links = [t for t in mime.text().split() if t.lower().startswith(("https://", "http://"))]
        return links

    def dragEnterEvent(self, e):
        mime = e.mimeData()
        if (mime.hasUrls() and any(u.isLocalFile() for u in mime.urls())) or self._web_links(mime):
            e.acceptProposedAction()
            self.overlay.setGeometry(self.centralWidget().rect().adjusted(16, 16, -16, -16))
            self.overlay.show()
            self.overlay.raise_()

    def dragLeaveEvent(self, e):
        self.overlay.hide()

    def dropEvent(self, e):
        self.overlay.hide()
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        links = self._web_links(e.mimeData())
        if paths or links:
            e.acceptProposedAction()
        if paths:
            self.add_paths(paths)
        if links:
            self.add_urls(links)

    # ================================================================ Documents
    def add_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add documents")
        if files:
            self.add_paths(files)

    def add_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Add folder")
        if d:
            self.add_paths([d])

    def add_url_dialog(self):
        text, ok = QInputDialog.getText(
            self, "Add from link",
            "GitHub repository, folder or file - or a direct link to a file (https://…):\n\n"
            "  https://github.com/owner/repo\n"
            "  https://github.com/owner/repo/tree/branch/folder\n"
            "  https://example.com/report.pdf\n\n"
            "Several links can be separated by spaces. Only public content can be downloaded.")
        if ok and text.strip():
            self.add_urls(text.split())

    def add_urls(self, links: list[str]) -> None:
        good, bad = [], []
        for link in links:
            try:
                remote.parse(link)
                good.append(link)
            except Exception as e:
                bad.append(f"{link}: {e}")
        if bad:
            QMessageBox.warning(self, "Add from link", "These links cannot be used:\n\n" + "\n".join(bad))
        if good:
            self.jobs.submit("url", good)
            self._busy(True)

    def add_paths(self, paths: list[str]) -> None:
        paths = [p for p in paths if Path(p).exists()]
        if not paths:
            return
        self._maybe_offer_ocr(paths)
        self.jobs.submit("add", paths)
        self._busy(True)

    def manage_ocr_packs(self, first_start: bool = False) -> None:
        """Languages: OCR packs, word forms per language, search by meaning."""
        self._set_cfg("ocr_first_start_asked", True)
        dlg = LanguagePacksDialog(self, self.ocr, first_start, self.config, cfg.MEANING_DIR)
        if dlg.exec() != LanguagePacksDialog.Accepted:
            if first_start or not self.ocr.available():
                self._set_cfg("ocr_download_declined", True)
                self.statusBar().showMessage("OCR skipped - choose ⚙ Menu → Languages and packs any time", 8000)
            return
        self._set_cfg("ocr_download_declined", False)
        if dlg.stemming_off != sorted(self.config.get("stemming_off") or []):
            self._set_cfg("stemming_off", dlg.stemming_off)
            set_stemming_off(dlg.stemming_off)
            self.statusBar().showMessage("Word forms changed - rebuilding the index …", 6000)
            self._load_index()
        self._set_cfg("meaning_enabled", dlg.meaning_enabled)
        if dlg.meaning_remove:
            self._stop_meaning()
            self.index.attach_model(None)
            semantic.remove(cfg.MEANING_DIR)
            self.statusBar().showMessage("Meaning model removed", 6000)
        elif dlg.meaning_install:
            self.jobs.submit("meaning_download", None)
            self._busy(True)
        elif not dlg.meaning_enabled and self.index.model is not None:
            self._stop_meaning()
            self.index.attach_model(None)
        elif dlg.meaning_enabled and self.index.model is None:
            self._start_meaning()
        if dlg.to_remove:
            self.ocr.remove(dlg.to_remove)
            self.statusBar().showMessage(f"Removed {len(dlg.to_remove)} language pack(s)", 6000)
        if dlg.to_install:
            self.jobs.submit("ocr_download", [tuple(dlg.to_install)])
            self._busy(True)

    def _ask_ocr_download(self) -> None:
        self.manage_ocr_packs(first_start=not self.config.get("ocr_first_start_asked"))

    def _maybe_ask_ocr_on_first_start(self) -> None:
        if (not self.config.get("ocr_first_start_asked") and self.config.get("ocr_enabled")
                and not self.ocr.available()):
            self.manage_ocr_packs(first_start=True)

    def _maybe_offer_ocr(self, paths) -> None:
        """When files that may need OCR are added and the data is still missing (and was never declined)."""
        if not self.config.get("ocr_enabled") or self.ocr.available() or self.config.get("ocr_download_declined"):
            return
        if any(Path(p).is_dir() or Path(p).suffix.lower() in OCR_HINT_EXT for p in paths):
            self._ask_ocr_download()

    def reindex(self, docs: list[dict]) -> None:
        docs = [d for d in docs if d.get("id")]
        if docs:
            self.jobs.submit("reindex", docs)
            self._busy(True)

    def reindex_problematic(self):
        self.reindex([d for d in self.index.docs.values() if d.get("status") != "ok"])

    def selected_docs(self) -> list[dict]:
        """Selected documents; a selected folder stands for all documents inside it."""
        out, seen = [], set()

        def collect(item):
            did = item.data(0, Qt.UserRole)
            if did in self.index.docs and did not in seen:
                seen.add(did)
                out.append(self.index.docs[did])
            for k in range(item.childCount()):
                collect(item.child(k))
        for it in self.docs.selectedItems():
            collect(it)
        return out

    def _doc_item_clicked(self, item, _col) -> None:
        """A click on a folder searches only in that folder."""
        key = item.data(0, FOLDER_ROLE)
        if key is not None:
            self.set_scope(tuple(key.split(FOLDER_SEP)))

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
        item = self.docs.itemAt(pos)
        key = item.data(0, FOLDER_ROLE) if item is not None else None
        docs = self.selected_docs()
        if not docs:
            return
        m = QMenu(self)
        if key is not None:
            folder = tuple(key.split(FOLDER_SEP))
            m.addAction("Search only in this folder", lambda: self.set_scope(folder))
            m.addAction(f"Re-read all {len(docs)} document(s)", lambda: self.reindex(docs))
            m.addSeparator()
            m.addAction("Remove folder …", self.remove_selected)
            m.exec(self.docs.viewport().mapToGlobal(pos))
            return
        m.addAction("Open", lambda: self._open_doc(docs[0], None))
        m.addAction("Show in folder", lambda: opener.show_in_folder(self.store.stored_path(docs[0])))
        m.addAction("Search only in this document", lambda: self._search_in(docs[0]))
        if folder_of(docs[0]):
            m.addAction("Search only in this folder", lambda: self.set_scope(folder_of(docs[0])))
        m.addAction("Re-read", lambda: self.reindex(docs))
        m.addSeparator()
        m.addAction("Remove …", self.remove_selected)
        m.exec(self.docs.viewport().mapToGlobal(pos))

    def _search_in(self, doc):
        name = doc["name"].split(" ")[0]
        text = " ".join(w for w in self.search.text().split() if not w.lower().startswith(("file:", "datei:")))
        self.search.setText(f'{text} file:{name}'.strip())

    def set_scope(self, folder) -> None:
        """Restrict the search to a folder (None = all documents)."""
        i = self.scope_box.findData(FOLDER_SEP.join(folder) if folder else "")
        self.scope_box.setCurrentIndex(max(0, i))

    def current_scope(self) -> tuple[str, ...] | None:
        key = self.scope_box.currentData()
        return tuple(key.split(FOLDER_SEP)) if key else None

    def _refresh_scope(self) -> None:
        current = self.scope_box.currentData()
        self.scope_box.blockSignals(True)
        self.scope_box.clear()
        self.scope_box.addItem("All folders", "")
        for parts, n in sorted(self.index.folders().items(), key=lambda kv: [p.casefold() for p in kv[0]]):
            self.scope_box.addItem(f"{'    ' * (len(parts) - 1)}{parts[-1]}  ({n})", FOLDER_SEP.join(parts))
            self.scope_box.setItemData(self.scope_box.count() - 1, " / ".join(parts), Qt.ToolTipRole)
        i = self.scope_box.findData(current)
        self.scope_box.setCurrentIndex(max(0, i))
        self.scope_box.blockSignals(False)
        self.scope_box.setVisible(self.scope_box.count() > 1)
        if current and i < 0:                  # folder no longer exists
            self.run_search()

    def _folder_item(self, parts: tuple, items: dict, counts: dict) -> QTreeWidgetItem | None:
        """Tree item of a folder, created with its parents on first use."""
        if not parts:
            return None
        if parts not in items:
            parent = self._folder_item(parts[:-1], items, counts)
            n = counts.get(parts, 0)
            it = DocItem([f"{parts[-1]}  ({n})", "", ""])
            it.setIcon(0, self.style().standardIcon(QStyle.SP_DirIcon))
            it.setData(0, FOLDER_ROLE, FOLDER_SEP.join(parts))
            it.setToolTip(0, f"{' / '.join(parts)} - {n} document{'s' if n != 1 else ''}\n"
                             "Click: search only in this folder")
            f = it.font(0)
            f.setBold(True)
            it.setFont(0, f)
            (parent.addChild(it) if parent is not None else self.docs.addTopLevelItem(it))
            items[parts] = it
        return items[parts]

    def _refresh_docs(self) -> None:
        self._refresh_scope()
        if any(it.data(0, FOLDER_ROLE) is not None for it in self._all_items()):   # remember open folders
            self._expanded = {it.data(0, FOLDER_ROLE) for it in self._all_items()
                              if it.data(0, FOLDER_ROLE) is not None and it.isExpanded()}
        self.docs.setSortingEnabled(False)
        self.docs.clear()
        c = self.colors
        folders, counts = {}, self.index.folders()
        for d in self.index.docs.values():
            pages = d.get("pages")
            kind = (d.get("kind") or "").lower()
            unit = UNITS.get(kind, "pages")
            count = pages or len(d.get("chunks") or [])
            unit = unit if pages else "sections"
            if count == 1:
                unit = unit[:-1]  # "1 page", "1 slide", "1 section"
            amount = f"{'~' if pages and d.get('pages_approx') else ''}{count} {unit}"
            parts = folder_of(d)
            label = d.get("display_name") or d["name"]
            short = label.split(" › ")[-1].rsplit("/", 1)[-1] if parts else label
            it = DocItem([short, kind.upper(), amount])
            it.setData(0, Qt.UserRole, d["id"])
            status = d.get("status", "ok")
            it.setIcon(0, dot_icon(c["ok"] if status == "ok" else c["warn"] if status == "warn" else c["err"]))
            tip = [d.get("display_name", ""), f"Type: {kind}", f"Language: {d.get('language', '?')}"]
            if d.get("source"):
                tip.append(f"Source: {d['source']}")
            if d.get("ocr_pages"):
                tip.append(f"OCR: {d['ocr_pages']} scanned page(s)")
            if d.get("ocr_images"):
                tip.append(f"OCR: {d['ocr_images']} image(s)")
            if d.get("message"):
                tip.append(d["message"])
            it.setToolTip(0, "\n".join(tip))
            parent = self._folder_item(parts, folders, counts)
            (parent.addChild(it) if parent is not None else self.docs.addTopLevelItem(it))
        self.docs.setSortingEnabled(True)
        for parts, it in folders.items():      # first time: top-level folders open, later as left by the user
            key = FOLDER_SEP.join(parts)
            it.setExpanded(len(parts) == 1 if self._expanded is None else key in self._expanded)
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
        folder = self.current_scope()
        hits, q = self.index.search(text, kinds=set(kinds), limit=limit, one_per_doc=self.one_per_doc.isChecked(),
                                    folder=folder)
        self.model.set_hits(hits, self.index)
        if hits:
            ndocs = len({h.doc_id for h in hits})
            self.results_title.setText(f"{len(hits)} hit(s) in {ndocs} document(s)"
                                       + (f" · folder {' / '.join(folder)}" if folder else "")
                                       + (" (limited)" if len(hits) >= limit else ""))
            self.stack.setCurrentIndex(1)
            self.table.selectRow(0)
        else:
            self.results_title.setText("")
            self.preview.clear()
            self.empty_title.setText("No hits")
            self.empty_text.setText("Try a different spelling or fewer words."
                                    + (f"\nOnly the folder {' / '.join(folder)} was searched - choose «All folders» "
                                       "at the top to search everything." if folder else "")
                                    + "\nTip: use * for word beginnings (e.g. contr*) and \"…\" for phrases.")
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
            self.empty_text.setText("Drag files, folders, ZIP archives or links (e.g. a GitHub repository)\n"
                                    "into this window, then type a search term at the top.")
        else:
            self.empty_title.setText("What are you looking for?")
            self.empty_text.setText(f"{len(self.index.docs)} documents ready. Type a search term above (Ctrl+F).\n"
                                    '"phrase"  ·  -exclude  ·  word*  ·  type:pdf  ·  file:name  ·  folder:name')

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
        spans = self.index.highlights(text, hit.match_terms, hit.lang)
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

    def _all_items(self):
        stack = [self.docs.topLevelItem(i) for i in range(self.docs.topLevelItemCount())]
        while stack:
            it = stack.pop()
            yield it
            stack += [it.child(k) for k in range(it.childCount())]

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
        QTimer.singleShot(300, self._maybe_ask_ocr_on_first_start)  # after the window is visible
        self._start_meaning()
        QTimer.singleShot(500, self._show_update_notice)
        QTimer.singleShot(1500, self._offer_staged)
        QTimer.singleShot(8000, self._auto_update_check)
        if self.ocr.available():  # e.g. language data copied by hand or Tesseract installed meanwhile
            self._on_ocr_installed()

    # ------------------------------------------------ Updates
    def _show_update_notice(self) -> None:
        """Once per program start: report an update that was applied before the restart."""
        from .. import updater   # loaded on use: keeps the start fast
        if self._notice_shown:
            return
        self._notice_shown = True
        notice = self.config.get("update_notice") or ""
        if ">" in notice:
            self._set_cfg("update_notice", "")
            old, new = notice.split(">", 1)
            QMessageBox.information(self, "Advanced Ctrl+F updated",
                                    f"Advanced Ctrl+F was updated from version {old} to {new}.\n\n"
                                    f"What's new: {updater.PAGE_URL}/commits/main")

    def _offer_staged(self) -> None:
        """An update prepared earlier («Later») is offered again - local check, no network."""
        from .. import updater   # loaded on use: keeps the start fast
        if self._staged_offered or not self.config.get("auto_update", True):
            return
        self._staged_offered = True
        found = updater.staged(cfg.ROOT)
        if found and found[0] != self.config.get("skip_version"):
            self._on_update_staged(found[0])

    def _auto_update_check(self) -> None:
        if not self.config.get("auto_update", True):
            return
        last = self.config.get("last_update_check") or ""
        try:
            if last and datetime.now() - datetime.fromisoformat(last) < timedelta(hours=20):
                return
        except ValueError:
            pass
        self.check_updates(manual=False)

    def check_updates(self, manual: bool = False) -> None:
        """Look for a new version in the background and prepare it."""
        if self.update_thread is not None and self.update_thread.isRunning():
            return
        self._set_cfg("last_update_check", datetime.now().isoformat(timespec="seconds"))
        self._update_manual = manual
        # signals arrive from a background thread: connected to methods of this window,
        # Qt delivers them in the window's thread
        th = UpdateWorker(install=manual or bool(self.config.get("auto_update", True)))
        th.progress.connect(self._on_update_progress)
        th.staged.connect(self._on_update_staged)
        th.available.connect(self._on_update_available)
        th.up_to_date.connect(self._on_up_to_date)
        th.failed.connect(self._on_update_failed)
        self.update_thread = th
        if manual:
            self.statusBar().showMessage("Checking for updates …", 5000)
        th.start()

    def _on_update_progress(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 5000)

    def _on_up_to_date(self) -> None:
        if self._update_manual:
            QMessageBox.information(self, "Updates", f"You have the latest version ({__version__}).")

    def _on_update_failed(self, reason: str) -> None:
        if self._update_manual:            # automatic checks stay silent, e.g. without internet
            QMessageBox.warning(self, "Updates", f"Checking for updates failed:\n{reason}")

    def _on_update_available(self, version: str) -> None:
        from .. import updater   # loaded on use: keeps the start fast
        text = f"Version {version} is available (you have {__version__})."
        if updater.is_git_checkout(cfg.ROOT):
            text += "\nThis program folder is a Git checkout - update it with git pull."
        self.statusBar().showMessage(text.replace("\n", " "), 15000)
        if self._update_manual:
            QMessageBox.information(self, "Updates", text)

    def _on_update_staged(self, version: str) -> None:
        if not self._update_manual and version == self.config.get("skip_version"):
            return
        if self._asking_update:
            return
        if self.jobs.pending:                    # never restart in the middle of reading documents
            self.statusBar().showMessage(f"Version {version} is ready - it is offered when the current work is done",
                                         15000)
            self.jobs.idle.connect(lambda *_: self._ask_update(version), Qt.SingleShotConnection)
            return
        self._ask_update(version)

    def _ask_update(self, version: str, seconds: int | None = None) -> None:
        """Restart now (countdown) / Later / Skip this version."""
        from .. import updater   # loaded on use: keeps the start fast
        found = updater.staged(cfg.ROOT)
        if not found or found[0] != version or self._asking_update:
            return
        self._asking_update = True
        seconds = RESTART_SECONDS if seconds is None else seconds
        box = QMessageBox(QMessageBox.Information, "Update available", "", parent=self)
        now = box.addButton("Restart and update now", QMessageBox.AcceptRole)
        later = box.addButton("Later", QMessageBox.RejectRole)
        skip = box.addButton("Skip this version", QMessageBox.DestructiveRole)
        box.setDefaultButton(now)
        box.setEscapeButton(later)
        left = [seconds]
        timed_out = [False]

        def tick():
            box.setText(f"Advanced Ctrl+F {version} is ready to install (you have {__version__}).\n"
                        "It was downloaded and its signature and checksum were verified.\n\n"
                        f"The program restarts in {left[0]} s to update. Your documents and settings stay "
                        "as they are.\n\n«Later»: you are asked again the next time you open the program.")
            if left[0] <= 0:
                timed_out[0] = True
                box.accept()
            left[0] -= 1
        timer = QTimer(box, interval=1000)
        timer.timeout.connect(tick)
        tick()
        timer.start()
        box.exec()
        timer.stop()
        self._asking_update = False
        clicked = box.clickedButton()
        if clicked is skip:
            self._set_cfg("skip_version", version)
            updater.discard(cfg.ROOT)
            self.statusBar().showMessage(f"Version {version} skipped - newer versions are offered again", 10000)
        elif timed_out[0] or clicked is now:
            try:
                new = updater.apply(cfg.ROOT, found[1])
            except Exception as e:
                QMessageBox.warning(self, "Update", f"The update could not be installed:\n{e}\n\n"
                                    "Advanced Ctrl+F keeps running with the current version.")
                return
            self._set_cfg("update_notice", f"{__version__}>{new}")
            updater.restart(cfg.ROOT)
            self.close()
        else:
            self.statusBar().showMessage(f"Version {version} is offered again the next time you open the program",
                                         10000)

    # ------------------------------------------------ Search by meaning
    def _stop_meaning(self) -> None:
        if self.meaning_thread is not None:
            self.meaning_thread.stop()
            self.meaning_thread.wait(5000)
            self.meaning_thread = None

    def _start_meaning(self) -> None:
        """Load the meaning model and compute the vectors of the current index in the background."""
        self._stop_meaning()
        if not (self.config.get("meaning_enabled", True) and semantic.available()
                and semantic.installed(cfg.MEANING_DIR)):
            return
        th = MeaningThread(self.index, cfg.MEANING_DIR)
        th.progress.connect(lambda d, t: self.statusBar().showMessage(
            f"Preparing search by meaning … {d} of {t} passages", 2000))
        th.ready.connect(lambda model, vecs, ix=self.index: self._on_meaning_ready(ix, model, vecs))
        th.failed.connect(self._on_meaning_failed)
        self.meaning_thread = th
        th.start()

    def _on_meaning_ready(self, ix, model, vecs) -> None:
        self.meaning_thread = None
        if ix is not self.index:          # the index was rebuilt meanwhile
            self._start_meaning()
            return
        ix.attach_model(model, vecs)
        self.statusBar().showMessage("Search by meaning is ready", 5000)
        if self.search.text().strip():
            self.run_search()

    def _on_meaning_failed(self, reason: str) -> None:
        self.meaning_thread = None
        QMessageBox.warning(self, "Search by meaning",
                            f"Search by meaning is not available:\n{reason}\n\n"
                            "The normal search works as before. Try again via ⚙ Menu → Languages and packs.")

    def _on_ocr_installed(self):
        """Language data arrived: re-read everything that could not be OCR'd before."""
        pending = [d for d in self.index.docs.values() if d.get("needs_ocr")]
        if pending:
            self.statusBar().showMessage(f"OCR ready - re-reading {len(pending)} document(s) that need it", 8000)
            self.reindex(pending)

    def _on_ocr_failed(self, reason: str):
        QMessageBox.warning(
            self, "OCR download failed",
            f"The OCR language packs could not be downloaded:\n{reason}\n\n"
            "Common causes: no internet access, or a company proxy/firewall blocking github.com.\n\n"
            "Alternative: get the files <pack>.traineddata (e.g. eng.traineddata, deu.traineddata) from\n"
            "github.com/tesseract-ocr/tessdata_fast and copy them into this folder:\n"
            f"{cfg.TESSDATA_DIR}\n\nThey are used after a restart; affected documents are re-read automatically.")

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

    def open_log(self):
        if not cfg.LOG_FILE.exists():
            self.statusBar().showMessage("No log file yet", 5000)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(cfg.LOG_FILE))):
            opener.show_in_folder(cfg.LOG_FILE)

    def copy_diagnostics(self):
        text = diagnostics.report(self.config, self.ocr, self.index.docs)
        QApplication.clipboard().setText(text)
        QMessageBox.information(
            self, "Diagnostics copied",
            "Diagnostic information was copied to the clipboard:\nversions, OCR status, documents that could not "
            "be read and the last lines of the logs.\n\nPaste it into an e-mail or chat to the person who helps you.\n"
            "It contains file names and folder paths, but no document contents.")

    def show_about(self):
        QMessageBox.information(self, "About", "\n".join(diagnostics.environment()))

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
        self._stop_meaning()
        self.jobs.stop()
        self.jobs.wait(3000)
        super().closeEvent(e)

    def sizeHint(self):
        return QSize(1300, 820)
