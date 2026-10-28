"""Install the packages from requirements.txt into the Python that runs this script -
but only if requirements.txt changed since the last successful install.

Called by start.cmd / start.sh:  python -I tools/ensure_deps.py
Exit code 0 = everything is in place.
"""
import hashlib
import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "requirements.txt"
CHECK_IMPORTS = ("PySide6.QtWidgets", "pymupdf", "openpyxl", "xlrd")


def main() -> int:
    digest = hashlib.sha256(REQ.read_bytes()).hexdigest()[:16]
    marker = Path(sys.prefix) / f".advanced-ctrl-f-deps-{digest}"
    if marker.exists():
        return 0
    print(f"Installing packages for Python {sys.version.split()[0]} ({sys.executable}) ...", flush=True)
    try:
        import pip  # noqa: F401
    except ImportError:
        subprocess.check_call([sys.executable, "-I", "-m", "ensurepip", "--upgrade"])
    # --only-binary: never compile anything, only install prebuilt wheels
    cmd = [sys.executable, "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
           "--only-binary=:all:", "--upgrade", "-r", str(REQ)]
    if subprocess.call(cmd) != 0:
        print("\nERROR: packages could not be installed.\n"
              "Possible causes: no internet connection, or this Python version is so new that no prebuilt\n"
              "packages exist yet. In that case set PY_VERSION=3.13 in the launcher and empty the 'python' folder.")
        return 1
    importlib.invalidate_caches()
    for mod in CHECK_IMPORTS:
        try:
            importlib.import_module(mod)
        except Exception as e:
            print(f"ERROR: module {mod} cannot be loaded: {e}")
            return 1
    for old in Path(sys.prefix).glob(".advanced-ctrl-f-deps-*"):
        old.unlink()
    marker.write_text("ok\n")
    print("Packages installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
