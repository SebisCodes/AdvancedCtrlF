"""Check a manually downloaded release archive of Advanced Ctrl+F.

    python3 tools/verify_download.py AdvancedCtrlF-1.5.3.zip

Downloads https://advanced-ctrl-f.giit.ch/updates/SHA256SUMS and SHA256SUMS.sig, checks the
Ed25519 signature with the public key built into the program (app/updater.py) and then the
SHA-256 of the archive against the signed list. Needs only Python 3.10+, no packages.
Run it from a copy you already trust (e.g. a Git checkout), not from the archive being checked.
Exit code 0 = signature and checksum are valid.
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ed25519  # noqa: E402
from app.updater import PUBLIC_KEY, SITE  # noqa: E402


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "AdvancedCtrlF"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(1024 * 1024)


def check(archive: Path, sums: bytes, sig_hex: str, public: bytes = PUBLIC_KEY) -> str:
    """Error message, or "" if the archive is listed in the validly signed SHA256SUMS."""
    try:
        signature = bytes.fromhex(sig_hex.strip())
    except ValueError:
        return "the signature file is damaged"
    if not ed25519.verify(public, sums, signature):
        return "INVALID SIGNATURE - SHA256SUMS was not published by the maintainer"
    expected = {name: digest for digest, name in
                (ln.split(None, 1) for ln in sums.decode("utf-8").splitlines() if ln.strip())}
    if archive.name not in expected:
        return f"{archive.name} is not listed in SHA256SUMS"
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected[archive.name].strip():
        return f"CHECKSUM MISMATCH for {archive.name} - do not use this file"
    return ""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    archive = Path(sys.argv[1])
    error = check(archive, get(SITE + "SHA256SUMS"), get(SITE + "SHA256SUMS.sig").decode("ascii", "replace"))
    print(error or f"OK: {archive.name} matches the signed SHA256SUMS")
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
