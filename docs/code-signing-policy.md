# Code signing policy

**The Windows build is not code-signed yet.** This page is the policy it will
be signed under, written before applying to
[SignPath Foundation](https://signpath.org/) for the free certificate it issues
to open-source projects. Nothing here claims a signature that does not exist:
until the application is accepted and a signed release is out, `ClipMint.exe`
is unsigned, and Windows will say so. Progress is tracked in
[#19](https://github.com/kleZ799/clipmint/issues/19).

<!-- Once the application is accepted, replace the paragraph above with:
Free code signing provided by [SignPath.io](https://about.signpath.io/),
certificate by [SignPath Foundation](https://signpath.org/).
-->

## What gets signed

One file: `ClipMint.exe`, the Windows download.

- It is built from this repository by the
  [release workflow](../.github/workflows/release.yml), on a GitHub-hosted
  Windows runner, from the commit a version tag points at. The
  [build logs](https://github.com/kleZ799/clipmint/actions/workflows/release.yml)
  are public.
- Nothing built on anyone's own computer is ever signed, and no file is signed
  that the workflow did not build from this repository.
- Every signing request is approved by hand, one release at a time.
- The file's product name is `ClipMint` and its version is the release's
  version. Both are set in [`version_info.txt`](../version_info.txt), and the
  workflow refuses to build when the tag and the code disagree.

The macOS and Linux downloads are outside this policy. The macOS app is ad-hoc
signed by the build so that it can launch on Apple Silicon; it is not
notarised by Apple.

## What is inside the exe that this project did not write

`ClipMint.exe` is a self-contained file. Besides this project's own code it
carries software built by other people, which is not built from this
repository and is not signed by this project:

- **Python** and the Python packages named in the
  [requirements files](../requirements-local.txt), at the versions pinned in
  [`constraints.txt`](../constraints.txt).
- **ffmpeg and ffprobe**, from the [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)
  release-essentials build, licensed under the GPL.
- **The YuNet face detection model** (MIT), **caption fonts** (SIL Open Font
  License and Apache 2.0) and **Twemoji images** (CC-BY 4.0). Their licences
  are in [`assets/`](../assets).

One thing goes into the published build that is not in the repository: the
Google client ID used to connect a YouTube channel. YouTube's API policies do
not allow it in open source, so the workflow reads it from a repository
secret. A build made without it works the same, and asks you for your own.

## Who does what

ClipMint has one maintainer, so the three roles are one person.

| Role | Who | What it means here |
|---|---|---|
| Author | [Parth Bhadana](https://github.com/kleZ799) | Commits to `main` |
| Reviewer | [Parth Bhadana](https://github.com/kleZ799) | Reads every pull request from anyone else before it is merged |
| Approver | [Parth Bhadana](https://github.com/kleZ799) | Approves each signing request |

Changes from other people only arrive as pull requests, and nobody else can
push to the repository or start a release. Under this policy the maintainer's
GitHub and SignPath accounts must both use two-factor sign-in.

## Privacy

The full policy is [PRIVACY.md](../PRIVACY.md). In short, ClipMint has no
server, no account, no analytics and no telemetry. It contacts other systems
only for things you start or set up:

- **YouTube**, to download a video you gave it, and to upload a clip when you
  press Upload.
- **The AI provider you chose**, with your own API key: the transcript text
  and a few still frames, to pick moments and write titles. The video itself
  is never sent.
- **GitHub**, to see whether a newer release exists and to download it when
  you accept the update.
- **Pexels**, only if you add a Pexels key and switch on B-roll.

## Removing it

There is no installer, so there is nothing to uninstall: delete
`ClipMint.exe`. To remove everything it kept, also delete the settings folder
`%APPDATA%\ClipMint` and the clips folder, `Videos\ClipMint` unless you moved
it in Settings. ClipMint changes no system settings and adds nothing to
startup.

## If something signed looks wrong

If a file signed as ClipMint behaves in a way this page does not describe, or
you find one that did not come from the
[releases page](https://github.com/kleZ799/clipmint/releases), please
[open an issue](https://github.com/kleZ799/clipmint/issues/new/choose). A
signature says the file is the workflow's build of this repository. It is not
a promise that the code has no bugs.
