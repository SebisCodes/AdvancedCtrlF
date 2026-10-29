"""Dialog for languages: OCR language packs, word forms per language and search by meaning."""
from __future__ import annotations

from PySide6.QtCore import QLocale, Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .. import lang as langmod
from .. import semantic
from ..ocr import FIRST_PASS_MAX
from ..search import has_stemmer

COL_LANG, COL_OCR, COL_FORMS, COL_SIZE, COL_STATUS = range(5)


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
    """Lists all catalog languages. Column "OCR pack" checked = pack should be installed,
    column "Word forms" checked = words are reduced to their stem in that language.
    After accept(): `to_install`/`to_remove` (Tesseract codes), `stemming_off` (ISO codes),
    `meaning_enabled`, `meaning_install`, `meaning_remove`."""

    def __init__(self, parent, ocr, first_start: bool = False, config: dict | None = None,
                 meaning_dir=None) -> None:
        super().__init__(parent)
        self.ocr = ocr
        self.config = config or {}
        self.meaning_dir = meaning_dir
        self.meaning_install = self.meaning_remove = False
        self.setWindowTitle("Languages and packs")
        self.resize(760, 720)
        own, system = set(ocr.own_packs()), ocr.system_packs()
        selected = own or (suggested_packs(ocr) if first_start or not ocr.installed() else set(ocr.installed()))
        self.own = own
        off = set(self.config.get("stemming_off") or [])

        lay = QVBoxLayout(self)
        intro = ("The language of every document is detected automatically. Per language you can choose:\n"
                 "• OCR pack - text recognition for scanned PDFs, image files and pictures inside documents. "
                 f"The first pages are read with up to {FIRST_PASS_MAX} installed packs, then with the detected "
                 "language only; fewer packs = faster OCR.\n"
                 "• Word forms - finds other forms of a word too (\"contracts\" finds \"contract\").")
        if first_start:
            intro = "Welcome! " + intro + "\n\nYou can change this later via ⚙ Menu → Languages and packs."
        lay.addWidget(QLabel(intro, wordWrap=True))
        lay.addWidget(self._meaning_box())

        self.filter = QLineEdit(placeholderText="Filter languages …")
        self.filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self.filter)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Language", "OCR pack", "Word forms", "Size", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(COL_LANG, QHeaderView.Stretch)
        known = set()
        for l in langmod.LANGUAGES:
            known.add(l.tess)
            self._add_item(l.tess, l.iso, f"{l.name}  ·  {l.native}", f"{l.size_mb:.1f} MB",
                           l.tess in selected, own, system, off)
        for code in sorted((own | set(system)) - known):   # packs installed by hand, not in the catalog
            self._add_item(code, None, code, "", code in selected, own, system, off)
        self.tree.itemChanged.connect(self._update_summary)
        lay.addWidget(self.tree, 1)

        self.summary = QLabel(objectName="muted")
        lay.addWidget(self.summary)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Apply")
        if first_start:
            self.buttons.button(QDialogButtonBox.Cancel).setText("Skip for now")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._update_summary()

    # ------------------------------------------------ Search by meaning
    def _meaning_box(self) -> QGroupBox:
        box = QGroupBox("Search by meaning")
        v = QVBoxLayout(box)
        v.addWidget(QLabel("Finds passages that mean the same as the search words, even if they use other words "
                           "(\"notice period\" also finds \"terminate the contract with three months' notice\") - "
                           "in more than 100 languages, also across languages. Works offline once the model is "
                           "installed.", wordWrap=True))
        row = QHBoxLayout()
        self.meaning_on = QCheckBox("Use search by meaning")
        self.meaning_on.setChecked(bool(self.config.get("meaning_enabled", True)))
        self.meaning_on.toggled.connect(self._update_summary)
        row.addWidget(self.meaning_on)
        row.addStretch(1)
        self.meaning_status = QLabel(objectName="muted")
        row.addWidget(self.meaning_status)
        self.meaning_btn = QPushButton()
        self.meaning_btn.clicked.connect(self._toggle_meaning_pack)
        row.addWidget(self.meaning_btn)
        v.addLayout(row)
        self._refresh_meaning()
        return box

    def _meaning_installed(self) -> bool:
        return bool(self.meaning_dir) and semantic.installed(self.meaning_dir)

    def _refresh_meaning(self) -> None:
        if not semantic.available():
            self.meaning_status.setText("needs the Python package numpy (start the program via the launcher)")
            self.meaning_btn.setEnabled(False)
            self.meaning_btn.setText("Install")
            return
        inst = self._meaning_installed()
        if inst and self.meaning_remove:
            self.meaning_status.setText("will be removed")
            self.meaning_btn.setText("Keep")
        elif inst:
            self.meaning_status.setText(f"installed (about {semantic.DISK_MB} MB)")
            self.meaning_btn.setText("Remove")
        elif self.meaning_install:
            self.meaning_status.setText(f"will be downloaded (about {semantic.DOWNLOAD_MB} MB, "
                                        f"{semantic.DISK_MB} MB on disk)")
            self.meaning_btn.setText("Don't install")
        else:
            self.meaning_status.setText(f"not installed - download about {semantic.DOWNLOAD_MB} MB")
            self.meaning_btn.setText("Install")

    def _toggle_meaning_pack(self) -> None:
        if self._meaning_installed():
            self.meaning_remove = not self.meaning_remove
        else:
            self.meaning_install = not self.meaning_install
            if self.meaning_install:
                self.meaning_on.setChecked(True)
        self._refresh_meaning()
        self._update_summary()

    @property
    def meaning_enabled(self) -> bool:
        return self.meaning_on.isChecked()

    # ------------------------------------------------ Languages
    def _add_item(self, code, iso, label, size, checked, own, system, off) -> None:
        if code in own:
            status = "installed"
        elif code in system:
            status = "found locally - no download"
        else:
            status = ""
        it = QTreeWidgetItem([label, code, "", size, status])
        it.setData(COL_LANG, Qt.UserRole, code)
        it.setData(COL_FORMS, Qt.UserRole, iso)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(COL_OCR, Qt.Checked if checked else Qt.Unchecked)
        if iso and has_stemmer(iso):
            it.setCheckState(COL_FORMS, Qt.Unchecked if iso in off else Qt.Checked)
        else:
            it.setText(COL_FORMS, "exact" if iso else "")
            it.setToolTip(COL_FORMS, "Words are matched as written (plus prefixes, compounds and typos)")
        self.tree.addTopLevelItem(it)

    def _items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _apply_filter(self, text: str) -> None:
        t = text.lower().strip()
        for it in self._items():
            it.setHidden(bool(t) and t not in (it.text(COL_LANG) + " " + it.text(COL_OCR)).lower())

    def selected(self) -> list[str]:
        return [it.data(COL_LANG, Qt.UserRole) for it in self._items() if it.checkState(COL_OCR) == Qt.Checked]

    @property
    def to_install(self) -> list[str]:
        return [c for c in self.selected() if c not in self.own]

    @property
    def to_remove(self) -> list[str]:
        return [c for c in self.own if c not in self.selected()]

    @property
    def stemming_off(self) -> list[str]:
        return sorted(it.data(COL_FORMS, Qt.UserRole) for it in self._items()
                      if it.data(COL_FORMS, Qt.UserRole) and has_stemmer(it.data(COL_FORMS, Qt.UserRole))
                      and it.checkState(COL_FORMS) == Qt.Unchecked)

    def _changed(self) -> bool:
        return bool(self.to_install or self.to_remove or self.meaning_install or self.meaning_remove
                    or self.meaning_enabled != bool(self.config.get("meaning_enabled", True))
                    or self.stemming_off != sorted(self.config.get("stemming_off") or []))

    def _update_summary(self, *_):
        system = self.ocr.system_packs()
        new = self.to_install
        size = sum(langmod.BY_TESS[c].size_mb for c in new if c in langmod.BY_TESS and c not in system)
        if self.meaning_install:
            size += semantic.DOWNLOAD_MB
        parts = [f"{len(self.selected())} OCR packs selected"]
        if new:
            parts.append(f"install {len(new)}")
        if self.to_remove:
            parts.append(f"remove {len(self.to_remove)}")
        if size:
            parts.append(f"download about {size:.0f} MB" if size >= 10 else f"download about {size:.1f} MB")
        if len(self.selected()) > FIRST_PASS_MAX:
            parts.append(f"note: the first OCR pass uses the first {FIRST_PASS_MAX} in this list")
        self.summary.setText(" · ".join(parts))
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(self._changed() or not self.own)
