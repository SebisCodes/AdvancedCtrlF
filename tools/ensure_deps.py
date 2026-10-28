"""Install the packages from requirements.txt into the Python that runs this script -
but only if requirements.txt changed since the last successful install.

Also prepares Pythons that ship without pip, e.g. the Windows "embeddable" ZIP from
python.org: it enables site-packages in its ._pth file and installs pip from the
official wheel on PyPI (SHA256-verified).

Called by start.cmd / start.sh:  python -I tools/ensure_deps.py [--log FILE]
With --log, all messages and the pip output are also appended to FILE (used by
start.cmd; start.sh records its whole output itself).
Exit code 0 = everything is in place.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
CHECK_IMPORTS = ("PySide6.QtWidgets", "pymupdf", "openpyxl", "xlrd")
PY = sys.executable
LOG: Path | None = None


def say(msg: str = "") -> None:
    print(msg, flush=True)
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def run(*args) -> int:
    """Run Python with `args`; output goes to the console (and the log, if any)."""
    if not LOG:
        return subprocess.call([PY, "-I", *args])
    proc = subprocess.Popen([PY, "-I", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        say(line.rstrip())
    return proc.wait()


def has_module(name: str) -> bool:
    # checked in a fresh process: changes to ._pth / new installs are not visible in this one
    return subprocess.call([PY, "-I", "-c", f"import {name}"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def enable_site_packages() -> None:
    """Embeddable Python: its pythonXY._pth file disables site-packages - turn it on."""
    for pth in Path(PY).parent.glob("python*._pth"):
        lines = pth.read_text(encoding="utf-8").splitlines()
        new = [("import site" if ln.strip() == "#import site" else ln) for ln in lines]
        if "import site" not in new:
            new.append("import site")
        if not any(ln.strip().lower() == r"lib\site-packages" for ln in new):
            new.insert(0, r"Lib\site-packages")
        if new != lines:
            pth.write_text("\n".join(new) + "\n", encoding="utf-8")
            say(f"Enabled site-packages in {pth.name}")
        (Path(PY).parent / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)


def install_pip_from_pypi() -> bool:
    """Download the official pip wheel, verify its SHA256 and install pip with it."""
    req = urllib.request.Request("https://pypi.org/pypi/pip/json", headers={"User-Agent": "AdvancedCtrlF"})
    with urllib.request.urlopen(req, timeout=60) as r:
        info = json.load(r)
    wheel = next(u for u in info["urls"] if u["packagetype"] == "bdist_wheel")
    if not wheel["url"].startswith("https://"):
        return False
    say(f"Downloading {wheel['filename']} ...")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, wheel["filename"])
        with urllib.request.urlopen(urllib.request.Request(wheel["url"], headers={"User-Agent": "AdvancedCtrlF"}),
                                    timeout=120) as r:
            path.write_bytes(r.read())
        if hashlib.sha256(path.read_bytes()).hexdigest() != wheel["digests"]["sha256"]:
            say("ERROR: pip download checksum mismatch.")
            return False
        # Run pip straight from its wheel to install itself. Started via -c (not as ".../pip"),
        # because on Windows pip refuses to modify itself when sys.argv[0] is named "pip".
        boot = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "from pip._internal.cli.main import main; "
                "sys.exit(main(['install', '--no-index', '--no-warn-script-location', "
                "'--disable-pip-version-check', sys.argv[1]]))")
        return run("-c", boot, str(path)) == 0


def ensure_pip() -> bool:
    if has_module("pip"):
        return True
    if has_module("ensurepip") and run("-m", "ensurepip", "--upgrade") == 0:
        return True
    say("This Python has no pip (e.g. the Windows embeddable package) - setting it up ...")
    enable_site_packages()
    return install_pip_from_pypi() and has_module("pip")


def main() -> int:
    global LOG
    if "--log" in sys.argv[1:-1]:
        LOG = Path(sys.argv[sys.argv.index("--log") + 1])
    digest = hashlib.sha256(REQ.read_bytes()).hexdigest()[:16]
    marker = Path(sys.prefix) / f".advanced-ctrl-f-deps-{digest}"
    if marker.exists():
        return 0
    say(f"Installing packages for Python {sys.version.split()[0]} ({PY}) - this can take a few minutes ...")
    if not ensure_pip():
        say("\nERROR: pip could not be set up for this Python.\n"
              "Use a regular Python instead: empty the 'python' folder and start again - the launcher then\n"
              "downloads a suitable one automatically.")
        return 1
    # --only-binary: never compile anything, only install prebuilt wheels
    if run("-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
           "--only-binary=:all:", "--upgrade", "-r", str(REQ)) != 0:
        say("\nERROR: packages could not be installed.\n"
              "Possible causes: no internet connection, or this Python version is so new that no prebuilt\n"
              "packages exist yet. In that case set PY_VERSION=3.13 in the launcher and empty the 'python' folder.")
        return 1
    for mod in CHECK_IMPORTS:
        if not has_module(mod):
            say(f"ERROR: module {mod} cannot be loaded.")
            run("-c", f"import {mod}")  # show the actual import error
            return 1
    for old in Path(sys.prefix).glob(".advanced-ctrl-f-deps-*"):
        old.unlink()
    marker.write_text("ok\n")
    say("Packages installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
