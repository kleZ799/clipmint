"""Build ClipMint's GitHub Pages site into _site/.

    pip install markdown
    python site/build.py

The home page is written by hand. The privacy page is rendered from PRIVACY.md
at the repo root, so the policy Google's consent screen links to can never
drift from the one in the repo -- there is only one copy of the words. The
comparison page and the engineering case study are rendered from docs/ the same
way, so GitHub and the site always say the same thing.

It also writes robots.txt, sitemap.xml and llms.txt, the files search engines
and AI assistants read first.

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
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="ClipMint">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{base}og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="icon.png">
<link rel="stylesheet" href="style.css">
</head>
<body>
<header class="top">
  <div class="wrap">
    <a class="brand" href="./"><img src="icon.png" alt="">ClipMint</a>
    <nav class="links">
      <a href="./#download">Download</a>
      <a href="compare.html">Compare</a>
      <a href="case-study.html">How it's built</a>
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
    <a href="https://github.com/kleZ799/clipmint/blob/main/{source}">This page on GitHub</a>
  </div>
</footer>
</body>
</html>
"""


BASE = "https://klez799.github.io/clipmint/"

# Pages rendered from markdown in the repo: (output, source, title, description).
DOCS = [
    ("privacy.html", "PRIVACY.md", "Privacy policy — ClipMint",
     "How ClipMint handles your data, including the YouTube data it uses to upload clips."),
    ("compare.html", "docs/compare.md", "ClipMint vs Opus Clip, Klap and Vizard — free AI clip generators compared",
     "An honest comparison of AI clip generators for YouTube Shorts, Reels and TikTok: price, "
     "free-plan limits, privacy and open source, including where other tools do more."),
    ("case-study.html", "docs/case-study.md", "How ClipMint is built — an engineering case study by Parth Bhadana",
     "Architecture, the hard problems and the measured results behind ClipMint, a desktop AI video "
     "clipper built with Python, FastAPI, Whisper, OpenCV, ffmpeg and LLMs."),
]

LLMS = """# ClipMint

> ClipMint is a free, open-source (MIT) desktop app for Windows, macOS and Linux that turns
> long videos (podcasts, interviews, vlogs, tutorials, gaming videos and stream VODs) into
> captioned vertical YouTube Shorts, Instagram Reels and TikToks on the user's own PC. It is a
> free alternative to Opus Clip, Klap and Vizard with no subscription, no credits, no watermark
> and no account. Built by Parth Bhadana.

Transcription runs locally with faster-whisper; the video never leaves the computer unless the
user presses Upload. An LLM (Gemini, Groq or OpenAI, on the user's own free key) ranks moments by
rules for each kind of video, a vision model checks the best ones, and ffmpeg renders 9:16 clips
with word-by-word captions, dead air cut and a crop that follows whoever is talking. It writes
titles and tags, uploads to YouTube on a schedule, and reads back each Short's views to tune its
ranking when a permutation test says the pattern is real.

## Pages

- [Home and download]({base}): what it does, screenshots, downloads for all three platforms
- [Comparison]({base}compare.html): ClipMint vs Opus Clip, Klap, Vizard and open-source clippers
- [Engineering case study]({base}case-study.html): architecture, hard problems, measured results
- [Privacy policy]({base}privacy.html)

## Source

- [GitHub repository](https://github.com/kleZ799/clipmint): code, and a README with every feature
- [HOW_IT_WORKS.md](https://github.com/kleZ799/clipmint/blob/main/HOW_IT_WORKS.md): module-by-module walkthrough
- [Latest release](https://github.com/kleZ799/clipmint/releases/latest)

## Author

- Parth Bhadana: [GitHub](https://github.com/kleZ799), [LinkedIn](https://www.linkedin.com/in/parth-bhadana-530014202/), parthbhadana57@gmail.com
"""


def render_doc(source: str) -> str:
    text = (ROOT / source).read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    # Tables are wider than a phone; let them scroll inside the page instead of
    # making the whole page scroll sideways.
    body = re.sub(r"<table>", '<div class="table-scroll"><table>', body)
    body = body.replace("</table>", "</table></div>")
    # The docs link to each other as .md so the links work on GitHub; here they
    # are .html pages beside this one.
    for out, src, _, _ in DOCS:
        body = body.replace(f'href="{Path(src).name}"', f'href="{out}"')
    return body


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir()

    for name in ("index.html", "style.css", "icons.svg"):
        shutil.copy2(SITE / name, OUT / name)
    shutil.copy2(ROOT / "assets" / "icon.png", OUT / "icon.png")
    shutil.copy2(ROOT / "assets" / "screenshots" / "01-create.png", OUT / "screenshot.png")
    shutil.copy2(ROOT / "assets" / "screenshots" / "09-youtube-upload.png", OUT / "upload.png")
    shutil.copy2(ROOT / "assets" / "screenshots" / "15-frame.png", OUT / "frame.png")
    shutil.copy2(ROOT / "assets" / "showcase" / "clipmint-showcase.mp4", OUT / "showcase.mp4")
    shutil.copy2(ROOT / "assets" / "showcase" / "clipmint-showcase-poster.jpg", OUT / "showcase.jpg")
    # The caption samples on the home page use the fonts the app burns in.
    (OUT / "fonts").mkdir()
    for font in (ROOT / "assets" / "fonts").iterdir():   # the fonts and their OFL licences
        shutil.copy2(font, OUT / "fonts" / font.name)

    # The card shown when a link to the site is shared.
    shutil.copy2(ROOT / "assets" / "social-preview.png", OUT / "og.png")

    for out, source, title, description in DOCS:
        page = PAGE
        for key, value in (("{title}", html.escape(title)), ("{description}", html.escape(description)),
                           ("{url}", BASE + out), ("{base}", BASE), ("{source}", source)):
            page = page.replace(key, value)
        (OUT / out).write_text(page.replace("{body}", render_doc(source)), encoding="utf-8")

    (OUT / "llms.txt").write_text(LLMS.replace("{base}", BASE), encoding="utf-8")
    # Every crawler is welcome, AI ones included: being read is the point.
    (OUT / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {BASE}sitemap.xml\n", encoding="utf-8")
    urls = [BASE] + [BASE + out for out, *_ in DOCS]
    (OUT / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{u}</loc></url>\n" for u in urls)
        + "</urlset>\n", encoding="utf-8")

    # Serve the files as they are; nothing here is a Jekyll site.
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    for f in sorted(OUT.iterdir()):
        print(f"  {f.name}")
    print(f"built {OUT}")


if __name__ == "__main__":
    main()
