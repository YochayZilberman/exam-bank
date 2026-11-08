"""Copy every new *.tmp in ~/Downloads into data/cache/drive/incoming/ as soon as it appears.
The built-in browser removes its download files shortly after writing them."""
import shutil, sys, time
from pathlib import Path
src = Path.home() / "Downloads"
dst = Path(__file__).resolve().parents[1] / "data/cache/drive/incoming"
dst.mkdir(parents=True, exist_ok=True)
seen = {}
end = time.time() + float(sys.argv[1] if len(sys.argv) > 1 else 3600)
while time.time() < end:
    for f in src.glob("*.tmp"):
        try:
            size = f.stat().st_size
            if seen.get(f.name) == size:
                continue
            time.sleep(0.3)
            if f.stat().st_size != size or size == 0:
                continue
            shutil.copy2(f, dst / f.name)
            seen[f.name] = size
            print("grabbed", f.name, size, flush=True)
        except OSError:
            pass
    time.sleep(0.2)
