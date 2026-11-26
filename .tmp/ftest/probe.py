import sys
try:
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    print("FFMPEG_EXE:", exe)
    import os
    print("EXISTS:", os.path.isfile(exe))
except Exception as e:
    print("FFMPEG_FAIL:", type(e).__name__, e)
