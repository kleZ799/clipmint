"""Build the ClipMint app with PyInstaller.

    pip install -r requirements-web.txt pyinstaller
    python build_exe.py

Output lands in dist/. On Windows that is a ClipMint folder, or a single
ClipMint.exe with --onefile (slower to start, since it unpacks to a temp
dir each launch, but the only build that can replace itself when an update
arrives). On Linux it is those same two shapes without the extension. On macOS
it is ClipMint.app, a bundle you drag to Applications.

PyInstaller cannot cross-compile: a Windows build has to be made on Windows, a
mac build on a Mac, and a Linux build on Linux. The release workflow runs one
of each.

Optional: drop ffmpeg and ffprobe into a ./bin folder before building and they
get bundled, so users don't have to install ffmpeg themselves. Without them the
app still builds and tells the user what's missing at startup.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.resolve()
NAME = "ClipMint"

MAC = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")

# Reverse-DNS, because macOS identifies an app by this rather than by its name.
# Two apps sharing one identifier confuse everything from window restoration to
# the keychain, so it is spelled out rather than left to PyInstaller's default.
BUNDLE_ID = "com.github.klez799.clipmint"


def _mb(size: int) -> str:
    """Bytes as the number the user will be shown.

    Divided by 1024s and still called MB, because that is what the GitHub
    releases page reports, what Explorer and Finder report, and therefore what
    the README says. Dividing by 1000s gives a different number for the same
    file -- 230 where every one of those says 219 -- and a figure copied from
    here into the docs once already disagreed with the download it described.
    """
    return f"{size / 1048576:.0f}"


def _sep() -> str:
    # PyInstaller's --add-data separator is platform-specific.
    return ";" if os.name == "nt" else ":"


def _check_version_agreement() -> None:
    """Refuse to build when version_info.txt and APP_VERSION disagree."""
    sys.path.insert(0, str(ROOT))
    from shorts_generator.version import APP_VERSION

    vf = ROOT / "version_info.txt"
    if not vf.exists():
        return
    text = vf.read_text(encoding="utf-8", errors="replace")
    found = set(re.findall(r"String[Ss]truct\(\s*'(?:File|Product)Version',\s*'([^']+)'", text))
    mismatched = {v for v in found if v.strip() != APP_VERSION}
    if mismatched:
        raise SystemExit(
            f"version mismatch: shorts_generator/version.py says {APP_VERSION}, "
            f"version_info.txt says {', '.join(sorted(mismatched))}. "
            f"Make them agree before building."
        )
    print(f"version {APP_VERSION} — version_info.txt agrees")


# --- macOS ----------------------------------------------------------------

def _mac_icon() -> Optional[Path]:
    """An .icns for the bundle, rendered from assets/icon.png.

    macOS will not read a .ico, so the icon Windows uses is no help here. Both
    sips and iconutil ship with macOS, so this needs nothing installed -- and
    generating the file beats committing a third copy of the same artwork and
    hoping whoever redraws it remembers all three.
    """
    icns = ROOT / "assets" / "icon.icns"
    png = ROOT / "assets" / "icon.png"
    if icns.exists():
        return icns
    if not png.exists():
        return None

    iconset = ROOT / "build" / "icon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True, exist_ok=True)
    try:
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                out = iconset / (f"icon_{size}x{size}.png" if scale == 1
                                 else f"icon_{size}x{size}@2x.png")
                subprocess.run(["sips", "-z", str(px), str(px), str(png),
                                "--out", str(out)],
                               check=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                       check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"could not build an .icns ({e}) — the app will use a blank icon")
        return None
    finally:
        shutil.rmtree(iconset, ignore_errors=True)

    print(f"rendered {icns.name} from {png.name}")
    return icns


def _stamp_bundle(app: Path) -> None:
    """Write the version and the app's real name into Info.plist.

    PyInstaller has a flag for neither, so a bundle otherwise calls itself
    0.0.0 -- which is what Finder's Get Info shows, and what the release
    workflow reads to check that the build it is about to publish really is the
    version its tag claims. This is version_info.txt's opposite number.
    """
    import plistlib

    sys.path.insert(0, str(ROOT))
    from shorts_generator.version import APP_VERSION

    plist = app / "Contents" / "Info.plist"
    data = plistlib.loads(plist.read_bytes())
    data["CFBundleShortVersionString"] = APP_VERSION
    data["CFBundleVersion"] = APP_VERSION
    data["CFBundleDisplayName"] = "ClipMint"
    data["NSHumanReadableCopyright"] = (
        "MIT licence. Source: github.com/klez799/clipmint")

    # The window is a view onto a server this app runs itself, on 127.0.0.1
    # over plain http, and App Transport Security blocks that by default --
    # with no error, just a window that comes up blank while everything behind
    # it works perfectly. pywebview does patch this into the bundle's info
    # dictionary at runtime, so this is a second lock on the same door: it is
    # declared rather than assumed, and it survives pywebview changing its
    # mind about doing that.
    ats = data.setdefault("NSAppTransportSecurity", {})
    ats["NSAllowsLocalNetworking"] = True

    plist.write_bytes(plistlib.dumps(data))
    print(f"stamped {APP_VERSION} into Info.plist")


def _finish_bundle(app: Path) -> int:
    """Make the bundled tools executable, then sign the bundle.

    Two things macOS insists on and Windows never asked for.

    ffmpeg arrives as a data file, and a data file has no execute bit -- so the
    app would build, ship, launch, and only then fail on the first clip with
    "permission denied", which is a long way to travel to find that out.

    And on Apple Silicon every executable must carry a signature. An unsigned
    binary there is not merely untrusted, it is killed on exec. There is no
    certificate here, so it is signed ad-hoc: enough to run, not enough to say
    who wrote it. Gatekeeper still has its say on first launch.

    Signing goes last, because a signature covers the bundle's contents. Touch
    anything afterwards and it no longer matches.
    """
    for name in ("ffmpeg", "ffprobe"):
        for path in app.rglob(name):
            if path.is_file() and not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o111)
                # Signed one at a time, because --deep below signs nested
                # *code* and ffmpeg arrived here as a resource. It is a real
                # Mach-O binary either way, and on Apple Silicon an unsigned
                # one does not run.
                subprocess.run(["codesign", "--force", "--sign", "-", str(path)],
                               stdout=subprocess.DEVNULL)
                print(f"  +x, signed  {path.relative_to(app)}")

    print("signing the bundle (ad-hoc)...")
    signed = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(app)])
    if signed.returncode != 0:
        print("codesign failed — this bundle will not launch on Apple Silicon",
              file=sys.stderr)
    return signed.returncode


def _youtube_client() -> Optional[Path]:
    """ClipMint's Google client for YouTube uploads, as a file to bundle.

    It comes from the CLIPMINT_YOUTUBE_CLIENT environment variable -- the JSON
    Google Cloud hands out for a Desktop app client -- which the release
    workflow fills from a repository secret. It never lives in the repo:
    YouTube's API policies (III.D.1) forbid embedding credentials in open
    source projects. Without it the build is complete; users paste their own
    client in Settings instead.
    """
    raw = os.environ.get("CLIPMINT_YOUTUBE_CLIENT", "").strip()
    local = ROOT / "webapp" / "youtube_client.json"
    if not raw:
        if local.is_file():
            print(f"bundling the YouTube client from {local.relative_to(ROOT)}")
            return local
        print("no CLIPMINT_YOUTUBE_CLIENT — users will add their own Google client")
        return None

    sys.path.insert(0, str(ROOT))
    from webapp.youtube_upload import parse_client
    try:
        client = parse_client(raw)
    except ValueError as e:
        raise SystemExit(f"CLIPMINT_YOUTUBE_CLIENT is set but unusable: {e}")

    out = ROOT / "build" / "youtube_client.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"installed": {"client_id": client["id"],
                                             "client_secret": client["secret"]}}),
                   encoding="utf-8")
    print("bundling ClipMint's YouTube client from CLIPMINT_YOUTUBE_CLIENT")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the ClipMint executable")
    ap.add_argument("--onefile", action="store_true",
                    help="Single .exe instead of a folder (slower first launch)")
    ap.add_argument("--clean", action="store_true", help="Wipe build/ and dist/ first")
    ap.add_argument("--cuda", dest="cuda", action="store_true", default=None,
                    help="Bundle the CUDA runtime (~2GB). Default for --onedir.")
    ap.add_argument("--no-cuda", dest="cuda", action="store_false",
                    help="Leave the CUDA runtime out. Default for --onefile.")
    args = ap.parse_args()

    if MAC and args.onefile:
        raise SystemExit(
            "--onefile is a Windows shape. A mac release ships ClipMint.app, "
            "a bundle, and one file buys nothing here: the mac build does not "
            "replace itself, so the only thing it would add is unpacking 200 MB "
            "on every launch. Build without --onefile."
        )

    # The two builds want opposite answers here, so the default depends on
    # which one is being made -- and saying it out loud beats letting them
    # drift apart by accident.
    #
    # A onedir build is unpacked already, so 2GB of CUDA costs disk and
    # nothing else. A onefile build re-extracts its entire payload to a temp
    # directory on EVERY launch, so the same 2GB is paid, as startup latency,
    # by every user on every run -- including the majority with no NVIDIA card
    # who cannot use it at all.
    use_cuda = (not args.onefile) if args.cuda is None else args.cuda
    if MAC and use_cuda:
        # No Mac has an NVIDIA card. Asking for CUDA here is not worth stopping
        # for, but quietly building something 2GB heavier that cannot work
        # would be.
        use_cuda = False
        if args.cuda:
            print("ignoring --cuda: no Mac has an NVIDIA card to run it on")
    if LINUX and use_cuda:
        # Linux cards are real; nothing here can reach them. The pip CUDA
        # wheels drop their .so files under site-packages/nvidia/*/lib, and
        # the dynamic linker reads LD_LIBRARY_PATH once, at exec -- so unlike
        # Windows, where add_dll_directory fixes this from inside the running
        # process, there is no moment at which those libraries can be made
        # findable. Bundling them adds 2GB that CTranslate2 still cannot load.
        use_cuda = False
        if args.cuda:
            print("ignoring --cuda: nothing can put bundled CUDA libraries on "
                  "the library path of a process that has already started")

    # The updater compares the running build's APP_VERSION against the newest
    # release tag. If the version resource says one thing and APP_VERSION says
    # another, a shipped build offers every user an "update" to the version
    # they are already running -- so the two are checked here rather than
    # discovered in the wild.
    _check_version_agreement()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Run:\n    pip install pyinstaller",
              file=sys.stderr)
        return 1

    if args.clean:
        for d in ("build", "dist"):
            shutil.rmtree(ROOT / d, ignore_errors=True)
        print(f"cleaned build/ and dist/")

    sep = _sep()
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", NAME,
        "--windowed",                      # no console window
        "--onefile" if args.onefile else "--onedir",

        # The UI files are read from disk at runtime, so they must ship.
        "--add-data", f"{ROOT / 'webapp' / 'static'}{sep}webapp/static",

        # YuNet, the face detector. faces.py looks for it under the bundle's
        # assets/models and falls back to Haar if it is missing, so a build
        # without it still works -- it just finds faces worse.
        "--add-data", f"{ROOT / 'assets' / 'models'}{sep}assets/models",

        # The growth playbook the packaging writer works from. playbook.py
        # can fetch a newer one, but a build must work offline from day one.
        "--add-data", f"{ROOT / 'assets' / 'playbook'}{sep}assets/playbook",

        # The native window. pywebview picks its backend at runtime, so
        # PyInstaller sees none of it without being told.
        "--hidden-import", "webview",
        "--collect-all", "webview",

        # Imported lazily inside functions, so PyInstaller can't see them.
        "--hidden-import", "faster_whisper",
        "--hidden-import", "ctranslate2",
        "--hidden-import", "google.genai",
        "--hidden-import", "openai",
        "--hidden-import", "cv2",
        "--hidden-import", "yt_dlp",
        "--hidden-import", "webapp.youtube_upload",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",

        # cv2's Haar cascades are data files the face detection loads by path.
        "--collect-data", "cv2",
        "--collect-data", "yt_dlp",

        # Torch is NOT what runs CUDA Whisper -- faster-whisper sits on
        # CTranslate2, which is collected above. Torch adds ~2GB and buys
        # nothing here, so it stays out.
        "--exclude-module", "torch",
        "--exclude-module", "matplotlib",
        "--exclude-module", "tkinter",
        "--exclude-module", "pytest",

        str(ROOT / "desktop.py"),
    ]

    # pywebview's backends are per-platform, and each drags in bindings the
    # other platform does not have. Naming the Windows ones on a Mac asks
    # PyInstaller to go and find pythonnet, which is not there and never
    # will be.
    if MAC:
        # The four frameworks webview/platforms/cocoa.py imports at module
        # level, named individually rather than trusted to the analyser:
        # PyObjC spreads them across separate distributions, and a backend
        # that half-imports does not fail at build time. It fails on the
        # user's Mac, as a window that never opens.
        platform_args = [
            "--hidden-import", "webview.platforms.cocoa",
            "--hidden-import", "objc",
            "--hidden-import", "AppKit",
            "--hidden-import", "Foundation",
            "--hidden-import", "WebKit",
            "--hidden-import", "PyObjCTools.AppHelper",
            "--collect-all", "objc",
            "--osx-bundle-identifier", BUNDLE_ID,
        ]
    elif LINUX:
        # Nothing. Windows and macOS each carry a webview in the operating
        # system; Linux carries neither, and the one pywebview would use here
        # is the one that cannot travel.
        #
        # WebKit2GTK is reached through PyGObject, whose typelibs and ~200
        # shared libraries would all have to come along -- and even bundled
        # they are only half of it, because WebKit renders pages in a separate
        # WebKitWebProcess that it finds by a path compiled into the library
        # at distribution build time. Get that wrong and the app opens a
        # window that stays blank, which is worse than not opening one.
        #
        # So the packaged build does not try. desktop.py already falls back to
        # the user's browser, which is a working app on every distribution
        # rather than a native window on the one this was built for. Anyone
        # running from source still gets a real window: there pywebview finds
        # the system's own python3-gi, and none of this applies.
        platform_args = []
        print("no bundled webview — this build opens the user's browser")
    else:
        platform_args = [
            "--hidden-import", "webview.platforms.edgechromium",
            "--hidden-import", "webview.platforms.winforms",
            "--hidden-import", "clr",
        ]
    cmd[-1:-1] = platform_args

    youtube_client = _youtube_client()
    if youtube_client is not None:
        cmd[-1:-1] = ["--add-data", f"{youtube_client}{sep}webapp"]

    bin_dir = ROOT / "bin"
    if bin_dir.is_dir() and any(bin_dir.iterdir()):
        cmd[-1:-1] = ["--add-data", f"{bin_dir}{sep}bin"]
        print(f"bundling binaries from {bin_dir}")
    else:
        print("no ./bin folder — ffmpeg will need to be on the user's PATH")

    if use_cuda:
        # CTranslate2 resolves cuBLAS and cuDNN through the DLL search path,
        # not by importing them, so PyInstaller never sees them unless told.
        # transcriber.py registers the directory at runtime.
        cmd[-1:-1] = ["--collect-binaries", "nvidia"]
        print("bundling the CUDA runtime — GPU transcription, ~2GB heavier")
    else:
        print("leaving the CUDA runtime out — transcription will run on the CPU")

    if MAC:
        icon = _mac_icon()
    elif LINUX:
        # --icon takes a .ico or an .icns and nothing else, and a Linux binary
        # carries no icon of its own regardless: that lives in a .desktop
        # file, which belongs to whoever installs the app, not to the build.
        icon = None
    else:
        # Author and copyright, compiled into the exe's version resource. This
        # is what Properties -> Details shows, so a copy that has travelled
        # away from this repo still says who wrote it and under what licence.
        # The format is a Windows resource; the mac equivalent is Info.plist,
        # written after the build instead.
        version_file = ROOT / "version_info.txt"
        if version_file.exists():
            cmd[-1:-1] = ["--version-file", str(version_file)]
        else:
            print("no version_info.txt — the exe will ship with no author metadata")

        ico = ROOT / "assets" / "icon.ico"
        icon = ico if ico.exists() else None

    if icon is not None:
        cmd[-1:-1] = ["--icon", str(icon)]

    print("running PyInstaller...\n")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    if MAC:
        out = ROOT / "dist" / f"{NAME}.app"
        if not out.is_dir():
            print(f"\nthe build produced no {out.name}", file=sys.stderr)
            return 1
        _stamp_bundle(out)
        rc = _finish_bundle(out)
        if rc != 0:
            return rc
    elif LINUX:
        # Neither shape has an extension, so both want the same name --
        # whichever one this run asked for is what PyInstaller wrote there.
        out = ROOT / "dist" / NAME
    else:
        out = ROOT / "dist" / (f"{NAME}.exe" if args.onefile else NAME)

    print(f"\nBuilt: {out}")
    if out.exists() and out.is_dir():
        size = sum(f.stat().st_size for f in out.rglob("*")
                   if f.is_file() and not f.is_symlink())
        noun = "Bundle" if MAC else "Folder"
        print(f"{noun} size: {_mb(size)} MB — zip this for a release.")
    elif out.exists():
        print(f"Size: {_mb(out.stat().st_size)} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
