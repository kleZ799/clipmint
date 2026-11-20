# Turn GTA 6 streams into YouTube Shorts, free

**ClipMint is a free, open-source app that turns a GTA 6 (GTA VI) stream or
gameplay recording into captioned vertical Shorts, Reels and TikToks on your own
PC.** Give it the VOD. It finds the moments worth posting, stacks your webcam
over the gameplay at 9:16, burns in captions, cuts the dead air, writes the
titles and can upload the clips to your YouTube channel. It has no subscription,
no per-clip credits and no watermark, and your video stays on your computer.

It works the same on any game: Fortnite, Minecraft, Valorant, Warzone,
Roblox, horror games, speedruns. GTA VI is just the one everybody is
streaming this week.

[Download ClipMint](https://klez799.github.io/clipmint/#download) ·
[Source code on GitHub](https://github.com/kleZ799/clipmint)

## The short answer

1. Stream or record your GTA VI session as you normally would.
2. Open ClipMint, then paste the YouTube link to the stream or drag the
   recording file in.
3. Choose **Stream or gaming** under *Kind of video*, or let it work that out.
4. Ask for what you want in plain words, like *"5 clips, around 30 sec each"*.
5. Check the ranked clips, fix any you don't like, and post them or schedule
   them to your channel.

## What it does with a game stream

- **It looks for your reaction, not the game.** On a stream it ranks the
  moments where you react: the chase that goes wrong, the jump-scare, the
  argument with chat. It skips long stretches of game with you nowhere in it.
- **Your webcam is found for you.** The vertical clip puts your webcam on top
  and the gameplay underneath. It finds the overlay in whichever corner you
  keep it, and the faces of in-game characters don't fool it, because they
  never stay in one place across the stream.
- **No webcam? Say so.** *"gameplay only, no webcam"* gives you a plain centre
  crop of the game.
- **The game's sound stays in.** Silent pauses and "um"s are cut, but a pause
  filled with gunfire, an explosion or the beat right before the payoff is
  kept.
- **It reads the chat replay.** On a YouTube stream it uses the chat replay to
  find the moments your live audience reacted to. Floods you asked for, like
  "everybody type W", don't count. A stream with no chat skips this signal
  and ranks the moments from what is said.
- **It writes the packaging.** You get titles, descriptions and tags for
  Shorts, plus captions for Reels and TikTok, written from what is actually
  on screen. You can edit every word.
- **It schedules the uploads.** Upload a whole run to YouTube with the
  *Gaming* category, one clip a day starting at a time you pick.

## Clip it while the game is new

Searches for a new release are at their peak in the first few days, so
clips posted the same day are seen by far more people than ones posted a month
later.

A four-hour stream takes about half an hour on a PC with an NVIDIA GPU (about
50 minutes on the CPU alone) for ten clips. Most of that time is downloading
and transcribing the stream, and that work is saved, so asking again for
different clips from the same stream starts at the ranking.

## Prompts that work

| Type this | It does |
|---|---|
| `5 clips, around 30 sec each` | five clips of about that length |
| `2 funny ones and 2 where I ask chat a question` | fills both groups |
| `my webcam is bottom right` | looks for your overlay in that corner only |
| `gameplay only, no webcam` | a centre crop of the game, no camera panel |
| `cut 14:45 to 15:30` | that exact span, with no ranking |
| `comic captions, keep the pauses` | caption style and edit settings |

## Where it falls short

- **It needs you to talk.** Moments are found from what is said, so a
  silent playthrough with no commentary gives it very little to rank. Clips
  from a session where you talk through what happens come out best.
- **Recordings made on a console need moving to a PC first.** It runs on
  Windows (tested), macOS (beta) and Linux (untested), not on the console
  itself. A stream already on YouTube needs only its link.
- **It posts to YouTube only.** Reels and TikToks come out ready to upload, with
  their captions written, but you post those yourself.

A full comparison with Opus Clip, Klap, Vizard and the open-source clippers,
including where they do more, is [here](compare.md).

## Questions

**Is there a free GTA 6 clip maker?**
ClipMint is free and open source (MIT). It has no credits, no watermark and
no account. You need an AI key for the ranking, and a
[Gemini key is free](https://aistudio.google.com/apikey). Or run a model
on your own PC through [Ollama](https://ollama.com) with no key at all.

**Does it upload my stream anywhere?**
No. Transcription runs on your PC. Only the transcript text goes to the AI
provider you choose, and a clip goes to YouTube only when you press Upload.

**Does it work for Twitch or Kick streamers?**
It works on the recording file, from OBS or anything else that records to
your PC. Chat replay is read only from YouTube streams.

**Does it need a good PC?**
It runs on the CPU. An NVIDIA GPU makes transcription more than three times
faster.

---

*GTA VI and Grand Theft Auto are trademarks of Take-Two Interactive and
Rockstar Games. ClipMint is an independent project and is not affiliated with
or endorsed by them.*
