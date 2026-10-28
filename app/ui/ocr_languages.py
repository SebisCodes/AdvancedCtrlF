"""Dialog to choose, install and remove OCR language packs."""
from __future__ import annotations

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHeaderView, QLabel, QLineEdit, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

from .. import lang as langmod
from ..ocr import FIRST_PASS_MAX


def suggested_packs(ocr) -> set[str]:
    """Preselection when nothing is installed yet: English, German and the system language."""
    picks = set(langmod.DEFAULT_PACKS)
    system_iso = QLocale.system().name().split("_")[0]
    if QLocale.system().name() in ("zh_TW", "zh_HK"):
        system_iso = "zh-Hant"
    tess = langmod.tesseract_code(system_iso)
    if tess:
        picks.add(tess)
    return picks | set(ocr.system_packs())


class LanguagePacksDialog(QDialog):
    """Lists all catalog languages; checked = should be installed. After accept():
    `to_install` and `to_remove` hold Tesseract codes."""

    def __init__(self, parent, ocr, first_start: bool = False) -> None:
        super().__init__(parent)
        self.ocr = ocr
        self.setWindowTitle("OCR language packs")
        self.resize(680, 660)
        own, system = set(ocr.own_packs()), ocr.system_packs()
        selected = own or (suggested_packs(ocr) if first_start or not ocr.installed() else set(ocr.installed()))
        self.own = own

        lay = QVBoxLayout(self)
        intro = ("Text recognition (OCR) reads scanned PDFs, image files and pictures inside documents. "
                 "It needs a language pack for every language you want to recognise.\n\n"
                 "The language of a document is detected automatically: the first pages are read with up to "
                 f"{FIRST_PASS_MAX} of the installed packs, then with the detected language only. "
                 "Fewer packs = faster OCR.")
        if first_start:
            intro = "Welcome! " + intro + "\n\nYou can change this later via ⚙ Menu → OCR language packs."
        text = QLabel(intro, wordWrap=True)
        lay.addWidget(text)

        self.filter = QLineEdit(placeholderText="Filter languages …")
        self.filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self.filter)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Language", "Pack", "Size", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        known = set()
        for l in langmod.LANGUAGES:
            known.add(l.tess)
            self._add_item(l.tess, f"{l.name}  ·  {l.native}", f"{l.size_mb:.1f} MB", l.tess in selected, own, system)
        for code in sorted((own | set(system)) - known):   # packs installed by hand, not in the catalog
            self._add_item(code, code, "", code in selected, own, system)
        self.tree.itemChanged.connect(self._update_summary)
        lay.addWidget(self.tree, 1)

        self.summary = QLabel(objectName="muted")
        lay.addWidget(self.summary)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Apply")
        if first_start:
            self.buttons.button(QDialogButtonBox.Cancel).setText("Skip OCR for now")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._update_summary()

    def _add_item(self, code, label, size, checked, own, system) -> None:
        if code in own:
            status = "installed"
        elif code in system:
            status = "found locally - no download"
        else:
            status = ""
        it = QTreeWidgetItem([label, code, size, status])
        it.setData(0, Qt.UserRole, code)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        self.tree.addTopLevelItem(it)

    def _items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _apply_filter(self, text: str) -> None:
        t = text.lower().strip()
        for it in self._items():
            it.setHidden(bool(t) and t not in (it.text(0) + " " + it.text(1)).lower())

    def selected(self) -> list[str]:
        return [it.data(0, Qt.UserRole) for it in self._items() if it.checkState(0) == Qt.Checked]

    @property
    def to_install(self) -> list[str]:
        return [c for c in self.selected() if c not in self.own]

    @property
    def to_remove(self) -> list[str]:
        return [c for c in self.own if c not in self.selected()]

    def _update_summary(self, *_):
        system = self.ocr.system_packs()
        new = self.to_install
        size = sum(langmod.BY_TESS[c].size_mb for c in new if c in langmod.BY_TESS and c not in system)
        parts = [f"{len(self.selected())} selected"]
        if new:
            parts.append(f"install {len(new)}" + (f" (download about {size:.1f} MB)" if size else ""))
        if self.to_remove:
            parts.append(f"remove {len(self.to_remove)}")
        if len(self.selected()) > FIRST_PASS_MAX:
            parts.append(f"note: the first OCR pass uses the first {FIRST_PASS_MAX} in this list")
        self.summary.setText(" · ".join(parts))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(new or self.to_remove) or not self.own)
