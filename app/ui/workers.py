"""Background threads: loading the index at startup and importing / re-indexing."""
from __future__ import annotations

import logging
import queue
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .. import remote
from ..extractors import Context
from ..search import SearchIndex
from ..store import Store

log = logging.getLogger(__name__)


class LoadThread(QThread):
    loaded = Signal(object)  # SearchIndex

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store

    def run(self) -> None:
        ix = SearchIndex()
        for doc in self.store.load_all():
            try:
                ix.add(doc)
            except Exception:
                log.exception("Index for %s is broken", doc.get("name"))
        self.loaded.emit(ix)


class JobThread(QThread):
    """Processes jobs one after another: ("add", [paths]), ("url", [links]), ("reindex", [docs]), ("ocr_download", None)."""
    progress = Signal(str, int, int)       # text, done, total
    doc_ready = Signal(object)             # new/updated document
    message = Signal(str)                  # message for the status bar
    ocr_installed = Signal()               # OCR language data was downloaded successfully
    ocr_failed = Signal(str)               # OCR language data download failed (reason)
    idle = Signal(str, int)                     # queue empty: summary text, number of failures

    def __init__(self, store: Store, config: dict, ocr) -> None:
        super().__init__()
        self.store, self.config, self.ocr = store, config, ocr
        self.jobs: queue.Queue = queue.Queue()
        self._cancel = False
        self._stop = False
        self.pending = 0

    def submit(self, kind: str, items) -> None:
        self.pending += len(items) if items else 1
        self._cancel = False
        self.jobs.put((kind, items))

    def cancel(self) -> None:
        self._cancel = True
        while not self.jobs.empty():
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break

    def stop(self) -> None:
        self._stop = True
        self.cancel()
        self.jobs.put(None)

    def run(self) -> None:
        done = 0
        stats = {"added": 0, "duplicate": 0, "failed": 0}
        while not self._stop:
            try:
                job = self.jobs.get(timeout=0.3)
            except queue.Empty:
                continue
            if job is None:
                break
            kind, items = job
            for item in (items or [None]):
                if self._cancel or self._stop:
                    break
                label = Path(item).name if isinstance(item, (str, Path)) else (item or {}).get("name", "OCR")
                ctx = Context(self.config, self.ocr,
                              progress=lambda m, l=label, d=done: self.progress.emit(f"{l}: {m}", d, self.pending),
                              cancelled=lambda: self._cancel or self._stop)
                try:
                    if kind == "ocr_download":
                        self.ocr.download(lambda m: self.progress.emit(m, done, self.pending))
                        self.message.emit("OCR language data installed")
                        self.ocr_installed.emit()
                    elif kind in ("add", "url"):
                        docs = (self.store.add_path(Path(item), ctx) if kind == "add"
                                else remote.fetch(item, self.store, ctx))
                        for doc in docs:
                            if doc.get("duplicate"):
                                stats["duplicate"] += 1
                            elif doc.get("status") == "error":
                                stats["failed"] += 1
                            else:
                                stats["added"] += 1
                            self.doc_ready.emit(doc)
                    elif kind == "reindex":
                        self.doc_ready.emit(self.store.reindex(item, ctx))
                        stats["added"] += 1
                except Exception as e:
                    log.exception("Job %s failed", kind)
                    if kind == "ocr_download":
                        self.ocr_failed.emit(str(e))
                    stats["failed"] += 1
                    self.message.emit(f"Error with {label}: {e}")
                done += 1
                self.progress.emit(label, done, self.pending)
            if self.jobs.empty():
                parts = [f"{v} {k}" for k, v in stats.items() if v]
                self.idle.emit(("Cancelled. " if self._cancel else "Done: ") + (", ".join(parts) or "nothing to do"),
                               stats["failed"])
                done, self.pending = 0, 0
                stats = {"added": 0, "duplicate": 0, "failed": 0}
