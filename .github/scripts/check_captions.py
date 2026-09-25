"""Prove the ffmpeg about to be bundled can burn the app's captions.

Captions are drawn by ffmpeg's `subtitles` filter, which only exists when
ffmpeg was built with libass. A build without it does not fail -- the edit
pass keeps the plain clip and says so in the log -- which is exactly why it
has to be caught here: every clip would quietly ship without captions, and
the first report would come from a user.

Listing the filter is not enough either. It is run for real, on a second of
video, with one of the bundled caption fonts, the same way autoedit.py runs
it: from a working folder, naming the script and the fonts relatively.

    python .github/scripts/check_captions.py bin/ffmpeg [--warn-only]
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FONT = ROOT / "assets" / "fonts" / "Montserrat-Black.ttf"

ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 568

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Montserrat Black,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,5,10,10,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Cap,,0,0,0,,{\\an5\\pos(160,400)}CAPTIONS {\\1c&H004DE1FF&}WORK
"""


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ffmpeg = os.path.abspath(sys.argv[1])
    warn_only = "--warn-only" in sys.argv[2:]
    level = "warning" if warn_only else "error"

    work = tempfile.mkdtemp(prefix="caption-check-")
    try:
        Path(work, "captions.ass").write_text(ASS, encoding="utf-8")
        fonts = Path(work, "fonts")
        fonts.mkdir()
        shutil.copyfile(FONT, fonts / FONT.name)
        result = subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-f", "lavfi",
             "-i", "color=c=black:s=320x568:d=1:r=10",
             "-vf", "subtitles=captions.ass:fontsdir=fonts",
             "-frames:v", "5", "-f", "null", "-"],
            cwd=work, capture_output=True, text=True, timeout=120,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    said = (result.stderr or "").strip()
    if result.returncode != 0:
        first = said.splitlines()[-1] if said else f"exit status {result.returncode}"
        print(f"::{level}::the bundled ffmpeg cannot burn captions ({first}). "
              f"It needs to be built with libass; every clip would ship without them.")
        return 0 if warn_only else 1
    print("captions burn with the bundled font: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
