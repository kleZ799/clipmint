"""The app's version, in one place.

The updater compares this against the newest GitHub release, so it has to be
the same number the exe reports in its own properties. build_exe.py checks
version_info.txt against it and refuses to build if they have drifted -- a
build that ships as 1.4.0 while telling the updater it is 1.3.2 would offer
every user an update to the version they are already running.
"""

APP_VERSION = "1.29.5"

# Where updates come from. Hardcoded on purpose: the page must never be able
# to point the updater at a different repository.
UPDATE_REPO = "kleZ799/clipmint"

# The public site: home page and privacy policy. Google's consent screen links
# here, and the link in the app must match it. site/build.py reads this too, so
# moving the site to a new domain is this one line (plus the Cloud Console).
SITE_URL = "https://klez799.github.io/clipmint/"
