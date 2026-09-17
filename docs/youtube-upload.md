# Uploading to YouTube from ClipMint

ClipMint can post a clip straight to your channel with its title, description
and tags, either now or on a schedule. It uses **YouTube's official Data API**.
There's no bot clicking around YouTube Studio, and ClipMint never sees your
password.

This guide covers three things:

1. [Setting up the Google client](#1-set-up-the-google-client) (once, about 10 minutes)
2. [Putting it in ClipMint](#2-put-it-in-clipmint): for yourself, or for everyone who downloads the app
3. [Getting uploads out of private](#3-getting-uploads-out-of-private): Google's audit and app verification, and whether ClipMint can pass them

---

## Why uploads start out private

Google treats every Cloud project as unaudited until YouTube has reviewed it.
Its documentation says uploads from those projects *"will be restricted to
private viewing mode"*. So until the project behind your client passes the
**YouTube API audit** (section 3):

- every upload lands as **private**, whatever you picked
- a **schedule is dropped**, because a scheduled video is a private video that
  YouTube makes public later

The upload itself still works. ClipMint checks each video after uploading and
tells you when YouTube kept it private, and you can flip it to public in Studio
with one click. After the audit passes, public and scheduled uploads work as
chosen.

---

## 1. Set up the Google client

Do this signed in to the Google account you want to own the project. It doesn't
have to be the account that owns the channel.

### 1.1 Create a project

1. Open [console.cloud.google.com](https://console.cloud.google.com/).
2. Click the project picker at the top, then **New project**.
3. Name it `ClipMint` and click **Create**. Make sure it's selected afterwards.

Make **exactly one** project for ClipMint. YouTube's developer policies
(III.D.1) require one API project per app.

### 1.2 Turn on the YouTube Data API

1. Go to **APIs & Services → Library**.
2. Search for **YouTube Data API v3**, open it, and click **Enable**.

### 1.3 Set up the consent screen

Google calls this area **Google Auth Platform** (older consoles call it
**OAuth consent screen**).

1. **Branding**
   - App name: `ClipMint`
   - User support email: your email
   - App logo: optional (adding one means Google has to review your branding)
   - App home page: `https://github.com/kleZ799/clipmint`
   - Privacy policy: `https://github.com/kleZ799/clipmint/blob/main/PRIVACY.md`
   - Developer contact email: your email
2. **Audience**
   - User type: **External**
   - Add your own Google account under **Test users** for now.
3. **Data access** → **Add or remove scopes**, and add exactly these two:
   - `https://www.googleapis.com/auth/youtube.upload`
   - `https://www.googleapis.com/auth/youtube.readonly`

### 1.4 Create the Desktop client

1. Go to **Clients** (or **APIs & Services → Credentials**).
2. Click **Create client** (or **Create credentials → OAuth client ID**).
3. Application type: **Desktop app**. Name: `ClipMint desktop`.
4. Click **Create**, then **Download JSON**.

It has to be **Desktop app**. A "Web application" client is refused by ClipMint
with an explanation, because Google would reject its sign-in redirect anyway.

**Keep that JSON file out of the repository.** YouTube's policies forbid
*"embed[ding] your API Credentials in open source projects"*. It's already in
`.gitignore` under the name ClipMint uses.

### 1.5 Stop the weekly logout: publish the app

While the project is in **Testing**, Google cancels every sign-in after
**7 days**, so ClipMint would ask you to reconnect each week.

Go to **Audience** and click **Publish app** to move it to **In production**.
You don't need verification to do this. Until the app is verified, people who
connect see a *"Google hasn't verified this app"* screen (they click
**Advanced → Go to ClipMint**), and at most 100 people can connect. Section 3
covers removing both limits.

---

## 2. Put it in ClipMint

### For yourself: paste it into the app

1. Open ClipMint → **Settings** → **Post to your channel**.
2. Open **Use your own Google client**, paste the contents of the JSON file (or
   the path to it), and click **Save client**.
3. Click **Connect YouTube**. Your browser opens Google's sign-in.
4. Pick the **channel** clips should go to. If you have a brand channel, choose
   it here. Allow both permissions.
5. The browser says **Connected to *your channel***. Go back to ClipMint.

Then open any clip, press **Boost**, and scroll the **YouTube Shorts** tab to
**Post it to your channel**:

- **Who can see it**: Public, Scheduled, Unlisted or Private
- **Goes public at**: shown when Scheduled, in your local time
- **Category**, and **Made for kids** (YouTube's legal question: answer it
  honestly for each video)
- **Upload to YouTube**: sends the title, description and tags exactly as they
  are in the boxes

A progress bar follows the upload, even if you close the panel. When it
finishes, the clip shows **On YouTube** with **Open in Studio** and **Watch**.

### For everyone who downloads ClipMint: a GitHub secret

A release can carry your client, so downloaders only have to click **Connect
YouTube**. The client goes in through a secret, never a file in the repo:

1. On GitHub, open **kleZ799/clipmint → Settings → Secrets and variables →
   Actions → New repository secret**.
2. Name: `CLIPMINT_YOUTUBE_CLIENT`. Value: the **entire contents** of the JSON
   file. Save.
3. Cut a release as usual. The release workflow hands the secret to
   `build_exe.py`, which checks it and bundles it into the Windows, macOS and
   Linux builds.

If the secret isn't set, the build still succeeds and users paste their own
client. If it's set to something unusable, the build stops and says why.

Or from a terminal:

```bash
gh secret set CLIPMINT_YOUTUBE_CLIENT -R kleZ799/clipmint < client_secret.json
```

For local builds, putting the file at `webapp/youtube_client.json` (git
ignores it) does the same thing.

**Before you ship this to everyone, be clear about three things:**

- **Anyone can pull the client out of the app.** Every desktop app has this
  problem, and Google knows it. It means someone could use your project's
  upload allowance. You can delete and replace the client in Google Cloud at
  any time.
- **Every downloader shares your project's limits.** That includes the daily
  upload allowance and, until verification, the 100-user cap.
- **Until your project passes the audit, uploads are private for everyone.**

---

## 3. Getting uploads out of private

Two separate Google reviews apply here. Only the first one decides whether
uploads can be public.

| | YouTube API audit | Google OAuth app verification |
| --- | --- | --- |
| What it fixes | Uploads locked to **private**; schedules dropped | The *"unverified app"* warning; the **100-user** cap |
| Needed for just you? | **Yes**, for public uploads | No. Google exempts personal-use apps with *"fewer than 100 users"*. You just click past the warning. |
| Needed to ship to everyone? | Yes | Yes, once more than 100 people connect |
| Cost | Free | Free for these scopes |
| Where | [YouTube API Services – Audit and Quota Extension Form](https://support.google.com/youtube/contact/yt_api_form) | **Google Auth Platform → Verification Center** in your project |

### 3.1 The YouTube API audit

**Apply at:** <https://support.google.com/youtube/contact/yt_api_form>

**Have these ready:**

- The **project number** (Cloud console → Dashboard)
- The app's public page: `https://github.com/kleZ799/clipmint`
- The privacy policy: `https://github.com/kleZ799/clipmint/blob/main/PRIVACY.md`
- **How it uses the API**, in plain words. For example:
  > ClipMint is a free, open-source desktop app for creators. It cuts their own
  > long videos, such as stream VODs and podcasts, into vertical Shorts.
  > When a user presses "Upload to YouTube" on a clip, it calls videos.insert
  > once for that clip with the title, description, tags, category, audience
  > and optional publishAt that the user has reviewed and can edit on screen.
  > It uses youtube.readonly only to show which channel is connected, and to
  > read the privacy status of the video it just uploaded. It never uploads
  > without a click, never uploads in bulk, and stores nothing on a server:
  > tokens and upload records stay on the user's own computer, and channel
  > details and upload records are refreshed or deleted within 30 days.
- A **screen recording**: connecting, editing a title, uploading, and the
  video showing up in Studio. An unlisted YouTube video works well.
- **Expected volume.** Be realistic, for example "a few uploads a day per user".

**Timeline:** Google gives none. Reports from developers range from a couple of
weeks to a couple of months. YouTube may ask follow-up questions by email, and
policy III.H says they can ask for access to test the app.

### 3.2 Google OAuth app verification

Only needed to ship the client to more than 100 people. It asks for:

- **A homepage and privacy policy on a domain you own and have verified** in
  Google Search Console. A `github.com/...` URL can't be verified as yours.
  Options:
  - a GitHub Pages site on a **custom domain** (about $10 a year), with
    `PRIVACY.md` published there
  - `kleZ799.github.io`, which can be verified in Search Console, though it's
    less certain that Google's OAuth review accepts it
- **A demo video** (unlisted on YouTube) showing the whole sign-in, the consent
  screen with both scopes, and the features that use each scope
- **A written justification for each scope.** Use the same text as the audit.

The link in the app (`EXTERNAL_LINKS["privacy"]` in `webapp/server.py`) and the
one on the consent screen must match. Change both if the policy moves to your
domain.

---

## Can ClipMint pass?

This is an honest read, not a guarantee. Google decides, and it doesn't publish
a checklist.

### Already in place

| Policy requirement | Where ClipMint meets it |
| --- | --- |
| Uses the official API, never passwords or scraping (III.D.2.a) | OAuth on Google's page, PKCE, loopback redirect: `webapp/youtube_upload.py` |
| Asks only for scopes it uses (III.D.2.a.2) | `youtube.upload` plus `youtube.readonly`, and both are used and explained |
| No credentials in the open source repo (III.D.1) | Client comes from a GitHub secret at build time; the JSON is gitignored |
| One project per app (III.D.1.c) | Section 1.1 |
| Actions are user-started and clearly YouTube actions (III.C.2, III.I.2) | One clip at a time, from a red **Upload to YouTube** button |
| User keeps final control; values aren't altered (III.C.3) | Uploads exactly what's in the boxes. Anything YouTube would reject is refused with the reason, never trimmed |
| Links YouTube's Terms and Google's Privacy Policy (III.A.1, III.A.2.c) | Settings → Post to your channel |
| Privacy policy with the required contents (III.A.2) | `PRIVACY.md` |
| Easy revocation, with data deleted (III.D.2.b, III.E.4.g) | **Disconnect** revokes at Google and deletes the token, channel details and upload records; a link to Google's permissions page is in the app |
| Keeps API data no longer than 30 days (III.E.4) | Channel details refreshed or dropped at 30 days; upload records swept at startup |
| Identifies itself honestly (III.D.2.b.2) | App name `ClipMint` on the consent screen; the policy says who makes it |

### What you still have to do

1. Create the project and client (section 1), and **publish** it to production.
2. Make sure `PRIVACY.md` is live at the URL above. It is once this commit is
   pushed.
3. Record the demo video and submit the audit form (3.1).
4. Before shipping to more than 100 people: a verified domain and OAuth
   verification (3.2).

### The risks, most serious first

1. **ClipMint downloads other people's videos.** It clips any YouTube link, and
   a reviewer may see "download a video, clip it, re-upload it" as a tool for
   reposting content the user doesn't own. This is the most likely reason to be
   refused. What helps:
   - Describe it truthfully as a tool for creators cutting **their own** long
     videos.
   - Point to the in-app reminder to "only upload what you have the rights to".
   - Make the demo video use your own content.
2. **The client ships inside a public download.** Policy III.D.1.d also says
   not to *"allow access to or use of your API Credentials by any other third
   party"*. Every installed app has this issue, and Google's own desktop OAuth
   guidance assumes the secret isn't secret. Say so plainly if asked; don't
   hide it.
3. **AI-written titles.** The metadata is generated, but the person sees and
   can edit every word before a deliberate click, which is what III.C.3 asks
   for. Keep it that way: **don't add** "upload all clips automatically" or
   "post on a timer without review". Features like those are what would fail
   the audit.
4. **A personal-use project is easier to get approved than one shipped to
   everyone.** If the public audit is refused, the fallback still works: each
   user makes their own client (section 1), pastes it into ClipMint, and
   uploads from an unaudited project land as private.

---

## Troubleshooting

| ClipMint says | What to do |
| --- | --- |
| *No Google client is set up yet* | Paste your client JSON under **Use your own Google client** (section 2). |
| *That is a "Web application" client* | Create a **Desktop app** client instead (1.4). |
| *The YouTube Data API v3 isn't turned on* | Enable it (1.2), wait a minute, try again. |
| *Google was not given permission to upload videos* | Connect again and leave both boxes ticked on Google's page. |
| *YouTube's permission was withdrawn or has expired*, every week | The project is still in **Testing**. Publish it (1.5). |
| *That Google account has no YouTube channel* | Create a channel at youtube.com, then connect again. |
| *Uploaded, but YouTube kept it private* | The project hasn't passed the audit (3.1). Make the video public in Studio for now. |
| *This Google project has used up today's YouTube allowance* | Resets at midnight Pacific time. |
| *YouTube says this channel has uploaded as much as it can for today* | A per-channel YouTube limit, separate from the API. Try again in 24 hours. |
| *YouTube doesn't allow < or > …* | Remove those characters from the title, description or tags. |
