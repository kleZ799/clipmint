# Release notes

The install instructions that ship *inside* the downloads live in
`docs/install/`: `macos.txt` goes into the zip as `READ ME FIRST.txt`, and
`linux.txt` is published beside the binary as
`StreamToShorts-linux-README.txt`. They repeat what the README says, for the
person holding the download and not the page — so a change to how either
platform is installed or used belongs in both.

One file per tag, named exactly after it: `v1.6.0.md` for tag `v1.6.0`.

The release workflow uses the matching file as the release body. When there
isn't one it falls back to GitHub's generated commit list, which is accurate
but reads like a changelog rather than something written for the person
downloading the exe — so write the file when the release is worth explaining.

Cutting a release:

```bash
# 1. bump both, they must agree or the build refuses
#      shorts_generator/version.py   APP_VERSION
#      version_info.txt              filevers / prodvers / File / ProductVersion
# 2. write docs/release-notes/v1.6.0.md
# 3. commit, then:
git tag -a v1.6.0 -m "Stream to Shorts v1.6.0"
git push origin main
git push origin v1.6.0
```

The workflow builds the exe on a GitHub runner and publishes the release.
Every installed copy from v1.5.0 onward will offer it to its user.

It also builds `StreamToShorts-macOS-arm64.zip` on a macOS runner. **Say in the
notes that the mac build is a beta and has not been run on a Mac** — it is
built and checked by CI, and that is all. Someone downloading it should know
that before they do, not after.

And `StreamToShorts-linux-x86_64`, built inside an Ubuntu 22.04 container so
the glibc floor is chosen rather than inherited. **Say in the notes that it
has not been tested on a desktop distribution.** The workflow does more for
this one than for either of the others — it starts the binary, fetches the
interface out of it, and measures what glibc the unpacked payload actually
needs — but a container is not a desktop, and nobody has yet made clips with
it on one.

## Updating the growth playbook — no release needed

What the packaging writer knows about how YouTube Shorts, Instagram Reels and
TikTok rank videos lives in `assets/playbook/short-form-algorithms.md`. Every
copy of the app from v1.14.0 on checks `main` for a newer one once a day, so
when a platform changes its algorithm:

1. edit the file, and raise the `reviewed: YYYY-MM-DD` date on its first line —
   a copy is only taken when its date is newer than the one the app already has;
2. commit and push to `main`.

That is the whole release. Leave the date alone and nobody gets the change
until the next build bundles it. The next tag bundles whatever is on `main`, so
a fresh download starts current too.

## Trying a build without releasing it

Run the **Release** workflow by hand from the Actions tab, tick **dry run**,
and leave the tag empty. It builds all three apps from the branch you picked,
checks them, publishes nothing, and attaches the results to the run for a
week.

Worth doing before a tag when anything about packaging changed — especially
for the mac build, which cannot be tried here first. What it proves: that it
builds, that the version and signature are right, that ffmpeg survived
bundling, and — on Linux only — that the app starts and serves its interface.
What it cannot prove: that the app opens on a real Mac, or that a real Linux
desktop behaves like the container it was built in.

## Rebuilding a tag

When one platform fails after the others have published — usually a download
host having a bad minute — rebuild the tag rather than cutting a new version.

**Re-running the failed job is only enough if the workflow itself is fine.** A
re-run of a tag-push run uses the workflow file *as it was at that tag*, so a
fix pushed to `main` since is ignored. To build a tag with the current
workflow, run Release by hand from `main`, give it the tag, and leave dry run
unticked:

```bash
gh workflow run Release -R kleZ799/stream-to-shorts --ref main -f tag=v1.11.1
```

It checks out the tag's code, builds all three, and replaces the release's
files in place (`--clobber`) — the same version, so nobody is offered an update
twice. (`-R` matters: without it `gh` reaches for the fork parent this repo was
forked from.)

Every ffmpeg download in the workflow is bounded, checked to be a real archive
and retried five times, so the usual cause — a host serving an error page, or a
transfer that stalls — is normally retried away before a job fails at all.
