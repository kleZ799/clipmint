# ClipMint privacy policy

_Last updated: 17 September 2026_

ClipMint is a free, open-source desktop app that turns long videos into Shorts.
It is made by Parth Bhadana. This policy covers the ClipMint app for Windows,
macOS and Linux, and the source code at
[github.com/kleZ799/clipmint](https://github.com/kleZ799/clipmint).

The short version: **ClipMint has no servers and no accounts.** Everything it
stores stays on your own computer. The developer never receives your videos,
your YouTube data or your sign-in.

## ClipMint uses YouTube API Services

If you choose to connect a YouTube channel, ClipMint uses **YouTube API
Services** to upload your clips to that channel. By connecting, you agree to
the [YouTube Terms of Service](https://www.youtube.com/t/terms). Google's use
of your information is covered by the
[Google Privacy Policy](http://www.google.com/policies/privacy).

Connecting YouTube is optional. ClipMint works fully without it.

## What ClipMint gets from your Google account, and why

When you press **Connect YouTube**, your browser opens Google's own sign-in
page. ClipMint never sees your Google password. Google asks you to allow two
permissions:

| Permission | What ClipMint does with it |
| --- | --- |
| Manage your YouTube videos (`youtube.upload`) | Uploads a clip when you press **Upload to YouTube**, with the title, description, tags, category, audience setting and schedule you can see and edit in the app. It never uploads anything by itself. |
| View your YouTube account (`youtube.readonly`) | Shows the name of the channel you connected, so you can check that clips go to the right one. After an upload, reads back that one video's privacy status to tell you if YouTube kept it private. |

ClipMint does not delete, edit or comment on anything on your channel. It does
not read your watch history, subscriptions, analytics or any other videos.

## What ClipMint stores, and where

Everything below is stored **only on your computer**, in ClipMint's settings
folder (`%APPDATA%\ClipMint` on Windows, `~/Library/Application Support/ClipMint`
on macOS, `~/.config/ClipMint` on Linux) or beside your clips:

- **Your Google sign-in token** (`youtube_token.json`), which lets ClipMint
  upload without asking you to sign in every time. It stays until you
  disconnect.
- **Your channel's name and ID.** ClipMint refreshes these from YouTube at
  least every 30 days, or deletes them.
- **A record of each upload**: the video's ID, when it was uploaded and its
  privacy setting, saved with that clip so the app can show "On YouTube" and
  open it in Studio. ClipMint deletes these once they are 30 days old, the
  next time it starts.
- **Your upload choices** (privacy, category, made-for-kids), kept in the app
  window's local storage so the form remembers them.
- **Your caption style and edit choices**, kept the same way.
- **Your Pexels key**, if you add one for B-roll, in the settings file. It
  stays until you remove it in Settings.
- **Your logo**, if you upload one, in the settings folder. It stays until
  you remove it in Settings.

ClipMint does not use cookies or any tracking or analytics. The YouTube sign-in
cookies feature, used only to *download* videos when YouTube asks for proof
you're not a bot, reads cookies on your own computer and passes them only to
YouTube.

## Who your information is shared with

- **The developer:** nobody. ClipMint has no server, and nothing is sent to
  the developer.
- **Google and YouTube:** your clips and their details go to YouTube when you
  upload, and to Google when you sign in.
- **The AI provider you choose** (Google Gemini, Groq or OpenAI), using your
  own API key: the clip's transcript text, a few still frames from each clip,
  and the source video's title and description, so it can pick moments and
  write titles. Nothing from your Google account or YouTube channel is ever
  sent to an AI provider.
- **Pexels**, only if you add a Pexels key and tick **B-roll**: a search of
  one to three words for each cutaway (for example "city at night"), sent
  with your own key, and the stock video it finds downloaded to your
  computer. Nothing you said, no frame of your video and nothing from your
  Google account is sent.
- **GitHub:** the app checks GitHub for updates and for a newer copy of its
  writing guide. These requests carry no personal data.

ClipMint does not sell or rent anything to anyone.

## How to disconnect and delete your data

- **In the app:** Settings → **Post to your channel** → **Disconnect**.
  ClipMint gives its permission back to Google and immediately deletes the
  sign-in token, the channel details and every upload record.
- **At Google:** remove ClipMint at
  [security.google.com/settings/security/permissions](https://security.google.com/settings/security/permissions).
  The next time ClipMint tries to use the permission, it finds it withdrawn and
  deletes the token and channel details. Upload records are deleted once they
  are 30 days old, the next time ClipMint starts.
- **By hand:** delete the ClipMint settings folder listed above. That removes
  everything ClipMint has stored.

Videos you uploaded stay on YouTube until you delete them in YouTube Studio,
because they are yours.

## Children

ClipMint is not directed at children under 13. When you upload, the app asks
you to answer YouTube's "made for kids" question for each video.

## Changes

If this policy changes, the new version is published in this file, and the
change is visible in the repository's history.

## Contact

Questions about privacy: **parthbhadana57@gmail.com**, or open an issue at
[github.com/kleZ799/clipmint/issues](https://github.com/kleZ799/clipmint/issues).
