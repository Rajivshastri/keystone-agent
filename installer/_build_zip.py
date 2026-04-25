"""Bundle the PyInstaller dist into KeystoneAgent-<ver>-win64.zip."""
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC  = REPO / "installer" / "dist" / "KeystoneAgent"
OUT  = Path(r"C:\Users\Administrator") / "KeystoneAgent-0.1.0-win64.zip"

if not SRC.is_dir():
    sys.exit(f"Source folder missing: {SRC}\nRun PyInstaller first.")

if OUT.exists():
    backup = OUT.with_name(f"{OUT.stem}.bak-{int(time.time())}.zip")
    shutil.move(str(OUT), str(backup))
    print(f"Backed up old zip -> {backup}")

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, _dirs, files in os.walk(SRC):
        for fname in files:
            full = Path(root) / fname
            arc  = full.relative_to(SRC.parent).as_posix()
            zf.write(full, arc)

size_mb = OUT.stat().st_size / 1024 / 1024
print(f"Built {OUT} ({size_mb:.1f} MB)")
