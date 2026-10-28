"""Start Advanced Ctrl+F. Called by start.cmd / start.sh as `python -I advanced_ctrl_f.py`.

-I (isolated mode) ignores PYTHONPATH, user site-packages and the current directory;
therefore only the program's own folder is added to sys.path explicitly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
