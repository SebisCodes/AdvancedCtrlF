"""Background threads: loading the index at startup and importing / re-indexing."""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .. import config as cfg
from .. import remote, semantic
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


class UpdateWorker(QObject):
    """Looks for a newer version and prepares it (download, signature and checksum check,
    unpack) - unless `install` is False or the program folder is a Git checkout. Runs in a
    daemon thread: without internet it never delays starting or closing the program. The
    program files themselves are only replaced later, in the window, when the user restarts."""
    progress = Signal(str)
    available = Signal(str)                # newer version exists, not prepared
    staged = Signal(str)                   # newer version downloaded, checked and ready to apply
    up_to_date = Signal()
    failed = Signal(str)

    def __init__(self, install: bool = True) -> None:
        super().__init__()
        self.install = install
        self.done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="update", daemon=True)
        self._thread.start()

    def isRunning(self) -> bool:
        return self._thread is not None and not self.done.is_set()

    def run(self) -> None:
        from .. import updater
        try:
            manifest = updater.fetch_manifest()
            version = manifest["version"].lstrip("v")
            if not updater.newer(manifest):
                self.up_to_date.emit()
            elif not self.install or updater.is_git_checkout(cfg.ROOT):
                self.available.emit(version)
            else:
                updater.stage(cfg.ROOT, manifest, self.progress.emit)
                self.staged.emit(version)
        except Exception as e:
            log.info("Update check failed: %s", e)
            self.failed.emit(str(e))
        finally:
            self.done.set()


class MeaningThread(QThread):
    """Loads the meaning model and computes the vectors of all chunks of an index."""
    progress = Signal(int, int)            # chunks done, total
    ready = Signal(object, object)         # model, {chunk id: vectors}
    failed = Signal(str)

    def __init__(self, index: SearchIndex, folder: Path) -> None:
        super().__init__()
        self.index, self.folder = index, folder
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            model = semantic.load(self.folder)
            if model is None:
                self.failed.emit("the meaning model could not be loaded")
                return
            vecs = self.index.compute_vectors(model, lambda d, t: self.progress.emit(d, t), lambda: self._stop)
            if not self._stop:
                self.ready.emit(model, vecs)
        except Exception as e:
            log.exception("Meaning vectors failed")
            self.failed.emit(str(e))


class JobThread(QThread):
    """Processes jobs one after another: ("add", [paths]), ("url", [links]), ("reindex", [docs]),
    ("ocr_download", [codes]), ("meaning_download", None)."""
    progress = Signal(str, int, int)       # text, done, total
    doc_ready = Signal(object)             # new/updated document
    message = Signal(str)                  # message for the status bar
    ocr_installed = Signal()               # OCR language data was downloaded successfully
    ocr_failed = Signal(str)               # OCR language data download failed (reason)
    meaning_installed = Signal()           # the meaning model was downloaded and converted
    meaning_failed = Signal(str)
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
                if kind == "ocr_download":
                    label = "OCR language packs"
                elif kind == "meaning_download":
                    label = "Meaning model"
                elif isinstance(item, (str, Path)):
                    label = Path(item).name if kind == "add" else str(item)
                else:
                    label = (item or {}).get("name", "")
                ctx = Context(self.config, self.ocr,
                              progress=lambda m, l=label, d=done: self.progress.emit(f"{l}: {m}", d, self.pending),
                              cancelled=lambda: self._cancel or self._stop)
                try:
                    if kind == "ocr_download":
                        self.ocr.download(item, lambda m: self.progress.emit(m, done, self.pending))
                        self.message.emit("OCR language packs installed")
                        self.ocr_installed.emit()
                    elif kind == "meaning_download":
                        semantic.install(cfg.MEANING_DIR, self.config.get("meaning_url") or semantic.DEFAULT_URL,
                                         lambda m: self.progress.emit(m, done, self.pending),
                                         lambda: self._cancel or self._stop)
                        self.message.emit("Meaning model installed")
                        self.meaning_installed.emit()
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
                    elif kind == "meaning_download":
                        self.meaning_failed.emit(str(e))
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
