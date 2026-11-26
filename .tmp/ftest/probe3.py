import sys
from tvhost2.core.ffmpeg_bin import find_ffmpeg, version
import subprocess
p = find_ffmpeg()
print("НАЙДЕН:", p)
print("ВЕРСИЯ:", version())
if p:
    r = subprocess.run([p,"-version"], capture_output=True, text=True, timeout=20, check=False)
    print("ЗАПУСКАЕТСЯ:", r.returncode == 0)
    print("БАННЕР:", (r.stdout or "").splitlines()[0][:70])
else:
    print("FFMPEG НЕ НАЙДЕН")
