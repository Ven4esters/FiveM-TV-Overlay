import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(sys.executable), '..'))
from tvhost2.core.ffmpeg_bin import find_ffmpeg, version
p = find_ffmpeg()
print("find_ffmpeg():", p)
print("version():", version())
if p:
    print("существует:", os.path.isfile(p))
    try:
        import subprocess
        out = subprocess.run([p,'-version'], capture_output=True, text=True, timeout=20)
        print("запуск:", out.stdout.splitlines()[0][:70] if out.stdout else out.stderr[:70])
    except Exception as e:
        print("ЗАПУСК ОШИБКА:", type(e).__name__, str(e)[:100])
