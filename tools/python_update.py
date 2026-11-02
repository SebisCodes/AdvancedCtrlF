"""Prepare a newer Python for Advanced Ctrl+F in python/_next/ (see app/pyupdate.py).

Started by the program in the background:  python -I tools/python_update.py
Messages go to data/python-update.log. Exit code 0 = nothing to do or prepared.
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import pyupdate  # noqa: E402


def main() -> int:
    (ROOT / "data").mkdir(exist_ok=True)
    logging.basicConfig(filename=ROOT / "data" / "python-update.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        pyupdate.prepare(ROOT, progress=logging.info)
        return 0
    except Exception as e:
        logging.warning("Python update not prepared: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
