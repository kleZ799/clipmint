"""Build ClipMint's GitHub Pages site into _site/.

    pip install markdown
    python site/build.py

The home page is written by hand. The privacy page is rendered from PRIVACY.md
at the repo root, so the policy Google's consent screen links to can never
drift from the one in the repo -- there is only one copy of the words.

Google's OAuth consent screen and YouTube's API audit both link to these pages,
which is why they live on github.io rather than github.com: Google only accepts
links on a domain it can list as authorised, and github.com is not anyone's to
authorise.
"""
import html
import re
import shutil
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
OUT = ROOT / "_site"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy policy — ClipMint</title>
<meta name="description" content="How ClipMint handles your data, including the YouTube data it uses to upload clips.">
<link rel="icon" href="icon.png">
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="top">
  <div class="wrap">
    <a class="brand" href="./"><img src="icon.png" alt="">ClipMint</a>
    <nav class="links">
      <a href="./#youtube">YouTube uploads</a>
      <a href="privacy.html">Privacy policy</a>
      <a href="https://github.com/kleZ799/clipmint">Source code</a>
    </nav>
    <!-- The same author block as the app's top bar. -->
    <div class="by">
      <span class="by-who"><span class="by-label">Built by</span><span class="by-name">Parth Bhadana</span></span>
      <span class="by-links">
        <a class="bl yt" href="https://www.youtube.com/@ParthBhadana799" title="YouTube — @ParthBhadana799" aria-label="YouTube"><svg aria-hidden="true"><use href="icons.svg#yt"/></svg></a>
        <a class="bl gh" href="https://github.com/kleZ799" title="GitHub — kleZ799" aria-label="GitHub"><svg aria-hidden="true"><use href="icons.svg#github"/></svg></a>
        <a class="bl li" href="https://www.linkedin.com/in/parth-bhadana-530014202/" title="LinkedIn — Parth Bhadana" aria-label="LinkedIn"><svg aria-hidden="true"><use href="icons.svg#linkedin"/></svg></a>
        <a class="bl dc" href="https://discord.gg/jnMrGbBz3m" title="Discord — join the server" aria-label="Discord"><svg aria-hidden="true"><use href="icons.svg#discord"/></svg></a>
        <a class="bl donate" href="https://buymeacoffee.com/parthbhadana" title="Support ClipMint — buy me a coffee" aria-label="Buy me a coffee"><svg aria-hidden="true"><use href="icons.svg#heart"/></svg></a>
      </span>
    </div>
  </div>
</header>
<main class="wrap">
<article class="doc">
{body}
</article>
</main>
<footer>
  <div class="wrap">
    <span>© Parth Bhadana · MIT licence</span>
    <a href="./">Home</a>
    <a href="https://www.youtube.com/t/terms">YouTube Terms of Service</a>
    <a href="https://github.com/kleZ799/clipmint/blob/main/PRIVACY.md">This policy on GitHub</a>
  </div>
</footer>
</body>
</html>
"""


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir()

    for name in ("index.html", "style.css", "icons.svg"):
        shutil.copy2(SITE / name, OUT / name)
    shutil.copy2(ROOT / "assets" / "icon.png", OUT / "icon.png")
    shutil.copy2(ROOT / "assets" / "screenshots" / "01-create.png", OUT / "screenshot.png")
    shutil.copy2(ROOT / "assets" / "screenshots" / "09-youtube-upload.png", OUT / "upload.png")
    shutil.copy2(ROOT / "assets" / "showcase" / "clipmint-showcase.mp4", OUT / "showcase.mp4")
    shutil.copy2(ROOT / "assets" / "showcase" / "clipmint-showcase-poster.jpg", OUT / "showcase.jpg")
    # The caption samples on the home page use the fonts the app burns in.
    (OUT / "fonts").mkdir()
    for font in (ROOT / "assets" / "fonts").iterdir():   # the fonts and their OFL licences
        shutil.copy2(font, OUT / "fonts" / font.name)

    text = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=["tables"])
    # Tables are wider than a phone; let them scroll inside the page instead of
    # making the whole page scroll sideways.
    body = re.sub(r"<table>", '<div class="table-scroll"><table>', body)
    body = body.replace("</table>", "</table></div>")
    (OUT / "privacy.html").write_text(PAGE.replace("{body}", body), encoding="utf-8")

    # Serve the files as they are; nothing here is a Jekyll site.
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    for f in sorted(OUT.iterdir()):
        print(f"  {f.name}")
    print(f"built {OUT}")


if __name__ == "__main__":
    main()
