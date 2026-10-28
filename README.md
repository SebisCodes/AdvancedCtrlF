# Advanced Ctrl+F

**Ctrl+F for many documents at once.** Drop files into the window, type what you are looking for, and get the
best matching passages ranked in a table – with page / slide / line / cell, a highlighted preview, and
"open at hit".

- Works on **Windows, macOS and Linux**, fully portable (everything lives in the program folder)
- Ships its own launcher that finds – or automatically downloads – a local Python
- No admin rights, no installer, no background services, no network access except for the optional downloads

## Quick start

| System  | Run           | Notes                                              |
|---------|---------------|----------------------------------------------------|
| Windows | `start.cmd`   | `start.cmd debug` keeps a console open for errors  |
| macOS   | `start.command` (double-click in Finder) | first time: right-click → *Open* → *Open* (unsigned script) |
| Linux   | `./start.sh`  | `./start.sh --setup` only prepares Python/packages |

The first start prepares Python and installs the required packages (a few minutes, ~200 MB). Later starts are instant.

### How the launcher finds Python

1. `PYTHON_BIN` – set it at the top of the launcher (or as environment variable) to use a specific interpreter
2. a Python inside the `python/` subfolder
3. an **archive** in `python/` – `.zip`, `.tar.gz`, `.tgz`, `.tar.xz` (Linux also `.deb`; Windows also an official
   `python-*.exe` installer) – is extracted/installed automatically and used. The Windows *embeddable* ZIP from
   python.org works too: pip is set up automatically (official wheel from PyPI, SHA256-verified)
4. otherwise the **latest stable CPython** is downloaded and verified:
   - **Windows:** the official build of the Python Software Foundation (NuGet package `python`) – SHA512 checked
     against the NuGet catalog and `python.exe` must carry a valid PSF code signature, so Smart App Control and
     Defender do not block it
   - **Linux and macOS** (Intel and Apple Silicon): [python-build-standalone](https://github.com/astral-sh/python-build-standalone),
     SHA256 checked against the published checksums
5. last resort: the system Python, inside a dedicated venv in `python/venv`

If a Python in `python/` cannot be started (for example because Windows blocks it), the Windows launcher sets it
aside as `python/_blocked` and downloads the signed official build instead.

**macOS:** the current Qt (PySide6) needs **macOS 14 Sonoma or newer**. On older versions set `PY_VERSION=3.12` in
`start.sh` so that an older PySide6 can be installed.

Set `PY_VERSION=3.13` in the launcher to pin a version. Packages from `requirements.txt` are installed as prebuilt
wheels only (nothing is compiled) and only re-installed when `requirements.txt` changes.

## Supported files

| Type | Location shown |
|------|----------------|
| PDF, XPS – incl. **OCR** for scanned pages with automatic **German/English** detection | page, line |
| Word (`.docx`, `.docm`) – paragraphs, tables, headers/footers, footnotes, comments, image alt texts, captions | page (approx.) |
| PowerPoint (`.pptx`) – text, tables, speaker notes, **image captions** and alt texts | slide |
| Excel (`.xlsx`, `.xls`), OpenDocument spreadsheet (`.ods`) | sheet + cell, e.g. `Sales!B7` |
| OpenDocument text / presentation (`.odt`, `.odp`) | paragraph / slide |
| CSV, TSV | line + column |
| HTML, XML, RTF, e-mail (`.eml`, `.mht`), EPUB, Markdown, JSON, source code, plain text | line / chapter |
| Images (`.png`, `.jpg`, `.tif`, …) via OCR | page |
| ZIP / TAR archives – every file inside is added individually | – |

**Any other file** can be added too: binaries (programs, databases, fonts, media …) are searchable by their file
name and by readable text strings inside; every file is at least found by its name. Jupyter notebooks are read cell
by cell (`Cell 3 · Code`).

**Repositories:** a folder that is a Git repository is added exactly as Git sees it (`.gitignore` respected, needs
`git` installed); otherwise `.git`, `node_modules`, virtual environments and caches are skipped. Public **GitHub**
repositories can be added directly – *Menu → Add from link / GitHub repository* or drag the link from the browser:
`https://github.com/owner/repo` (default branch), `…/tree/branch/folder` (one branch or folder) or `…/blob/…` (one
file). Any other `https://` link to a file works as well. Hits show up as `owner/repo › path/file.py`;
`file:repo` restricts the search to it.

Legacy binary formats (`.doc`, `.ppt`) are converted with LibreOffice if it is installed, otherwise their raw text
is indexed. 
**Images inside documents are read with OCR as well** (on by default, menu → *Always OCR all images inside
documents*): pictures in PDFs, Word, PowerPoint, Excel and OpenDocument files – screenshots, diagrams, scanned
notes – are extracted and recognised, and the hit points to the page, slide or cell where the picture sits
(e.g. `Slide 4 · Image (OCR)`, `Sales!D5 · Image (OCR)`). PDF text layers are kept as they are; scanned pages
are recognised as a whole. Tiny images such as icons are skipped and a picture that appears several times
(like a logo on every slide) is read and listed only once.

**OCR** does not need a separate Tesseract installation: PyMuPDF ships the engine, only the language data
(German + English, ~6 MB) is required. On the **first start** the program asks whether to download it
(once – the answer is remembered; it can be downloaded later via *Menu → Download OCR language data*). An existing
Tesseract installation is reused automatically, in which case no question is asked.

## Searching

Case, umlauts (*Größe = Groesse = Grosse*), word forms (*contract / contracts*, *Vertrag / Verträge*), typos
(*warrenty*) and parts of compound words (*vertrag* finds *Arbeitsvertrag*) are handled automatically.

Ranking uses BM25 per passage plus boosts for: all search words present, words close to each other, words in the
same order as typed, and exact phrases.

| Input             | Meaning                                  |
|-------------------|------------------------------------------|
| `"three months"`  | exact phrase                             |
| `-draft`          | exclude passages containing the word     |
| `warrant*`        | word prefix                              |
| `type:pdf,docx`   | only these file types                    |
| `file:report`     | only files whose name contains "report"  |

**Double-click** a hit to open the document at the hit: PDFs on the right page (SumatraPDF, Acrobat, Okular,
Evince, zathura, … or a browser), web pages in the browser scrolled to the hit, text files at the right line
(VS Code, Notepad++, Kate, gedit, …), everything else in its default application. The built-in page view renders
PDF and image pages with the hits highlighted.

> **Tip (Windows):** keep the program folder outside of OneDrive/Dropbox. Python and its packages are several
> thousand files – syncing them is slow and triggers OneDrive's "delete these items?" prompts.

## Troubleshooting

- **⚙ Menu → Copy diagnostics** copies versions, OCR status, documents that could not be read and the last log
  lines to the clipboard – paste it into a message to whoever helps you (contains file names and paths, no
  document contents).
- `data/advanced_ctrl_f.log` – application log: every document read (with OCR counts and problems), opened
  applications, errors with tracebacks. *Menu → Open log file* opens it.
- `data/setup.log` – launcher log: which Python was found or downloaded, checksum/signature checks, package
  installation (pip output) and the exact error if the start fails.
- `start.cmd debug` keeps the console window open on Windows.
- **Yellow dot "…not read: OCR language data missing"** – the OCR data was not downloaded (declined or blocked by a
  proxy/firewall). Use *Menu → Download OCR language data*; affected documents are re-read automatically afterwards.
  Without internet access, copy `eng.traineddata` and `deu.traineddata` (from
  [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)) into `data/tessdata/` and restart.

## Folder layout

```
start.cmd / start.sh   launchers (Windows / Linux and macOS)
start.command          macOS double-click launcher (runs start.sh)
advanced_ctrl_f.py     entry point
app/                   application code (extractors, search, UI)
tools/                 helper scripts used by the launchers
tests/                 automated tests
config/config.json     settings                          (created on first start)
data/documents/        copies of the added documents     (originals are never modified)
data/index/            one JSON per document: text passages with their location
data/tessdata/         OCR language data (downloaded on demand)
python/                local Python
website/               the web page advanced-ctrl-f.giit.ch (not part of the download, see website/README.md)
```

The JSON files in `data/index/` are plain and documented by example – each passage looks like
`{"loc": {"kind": "page", "page": 3, "line": 12}, "text": "..."}`.

## Security

- Python runs in isolated mode (`-I`): no `PYTHONPATH`, no user site-packages, no current directory on `sys.path`
- Downloads only over HTTPS; every download is verified (published checksums, and on Windows also the code signature)
- XML with entity definitions is rejected (no XXE / "billion laughs"); archives are extracted without path
  traversal, links, or unbounded sizes
- Documents are copied without execute permission; executable file types are never launched
- External programs are started with argument lists, never through a shell

## Tests

```
python/python/bin/python3 -I tests/test_all.py      # Linux
python\python\python.exe -I tests\test_all.py        # Windows
python/python/bin/python3 -I tests/smoke_gui.py      # GUI smoke test (works without a screen)
```

Every push runs the real launchers and all tests on Linux, macOS and Windows (GitHub Actions,
`.github/workflows/test.yml`), including the Python download/verification and the OCR data download.

The tests generate small sample files for every format (partly with German text to cover umlauts and language
detection) and check extraction, locations and ranking.
