# tubecast roadmap

Design notes and plans: why tubecast exists, the decisions behind it, what was learned on the
way and what may come next. For setup and configuration, see the [README](README.md).

## Why

Some people publish only on YouTube, with no podcast feed. tubecast makes one: it follows a list
of channels, downloads their new videos as audio, cuts out sponsor segments, keeps the newest few
episodes and serves one podcast feed per channel, to listen to in a podcast app without ads or
sponsor reads. It's meant to run at home, on a small server reachable only by your own devices.

## Decisions

- **A config file, no web UI.** Adding a channel is one line. tubecast re-reads the file when it
  changes.
- **Regular videos only by default.** No Shorts and no live streams; `include_shorts` and
  `include_live` add them per channel. `min_duration` is optional.
- **Keep the newest 5 episodes per channel** (configurable). A new channel gets its newest 5
  right away, so the feed is full as soon as you subscribe.
- **No ads, no sponsor reads.** SponsorBlock cuts `sponsor`, `selfpromo` and `interaction`
  ("like and subscribe") by default. YouTube's own ads are never in the downloaded file.
- **Wait before downloading.** SponsorBlock segments are marked by viewers after a video comes
  out, so tubecast only downloads videos older than `download_delay` (6 hours by default).
- **YouTube's own audio.** `bestaudio[ext=m4a]` is AAC, which podcast apps play: no re-encoding,
  no quality loss, little CPU. About 60 MB per hour.
- **No notifications.** The podcast app announces new episodes.
- **Python and yt-dlp, in Docker.** Multi-arch image (amd64 and arm64), only PyYAML besides
  yt-dlp.
- **yt-dlp stays up to date by itself.** YouTube changes break old versions within days, so
  tubecast runs `pip install -U yt-dlp` (stable releases) at start and every 24 hours. A failed
  update is logged and the current version kept. yt-dlp runs as a subprocess, not imported, so an
  update takes effect without a restart.
- **Back up the config and state, not the audio.** Episodes can be downloaded again.

## Tools looked at first

- **Pinchflat**: web UI, audio-only, per-channel RSS, SponsorBlock, retention, no API key. But
  barely maintained by late 2026 (last release September 2025, 200+ open issues), and its stable
  image was amd64-only. It updates yt-dlp on start, but wouldn't follow a YouTube change that
  needs a new tool in the image, as Deno did in September 2025.
- **Podsync**: needs a YouTube Data API key, and has no SponsorBlock option.
- **ytdl-sub**: very active, but makes media-server libraries, not podcast feeds.
- **Audiobookshelf**: subscribes only to real podcast feeds; YouTube's channel feeds have no audio.

## How it's built

- `tubecast/config.py` reads the YAML; `later.py` is the Listen later feed; `ytdlp.py` runs and updates yt-dlp; `sync.py` lists
  channels, picks what to download, downloads and deletes; `feed.py` writes the RSS;
  `chapters.py` handles chapters and timestamps; `server.py` serves feeds and audio;
  `__main__.py` runs the loop.
- **Listing:** yt-dlp's flat listing of a channel tab (`/videos`, plus `/streams` and `/shorts`
  when included) gives IDs and durations but no upload time, so each new video's full metadata is
  fetched once and cached in `state/<slug>.json`. Shorts and live streams are filtered by tab, not
  by `live_status`: premieres are on `/videos` and may be marked `was_live`.
- **Picking:** a video is downloaded only if it would be among the newest `keep` of episodes and
  eligible videos together, so nothing is downloaded and then deleted right away. Failed videos
  are tried again after 24 hours.
- **Feeds** are built from the state on each request, so a new `base_url` applies at once. The
  episode GUID is the YouTube video ID, so apps keep the same episode if a file is downloaded
  again. Feeds carry `<itunes:block>Yes</itunes:block>` so directories don't list them.
- **The HTTP server** answers Range requests: podcast apps check the size with `bytes=0-1`, then
  stream and seek with ranges.
- **Image:** `python:3.13-slim`, Deno (yt-dlp's `yt-dlp-ejs` needs a JavaScript runtime for
  YouTube's challenges since late 2025) and a static ffmpeg build. Debian's ffmpeg package made
  the image 1.02 GB; the static build brings it to about 765 MB. yt-dlp lives in a virtualenv the
  non-root `tubecast` user owns, so it can update itself.
- An optional `cookies.txt` in the data folder is passed to yt-dlp, for when YouTube asks to sign
  in.

## What we learned about podcast apps

- **Many apps fetch feeds on their own servers, not on the phone.** A feed that only your own
  devices can reach (on a home network or a VPN like Tailscale) works only in apps that fetch on
  the phone. Tested in November 2026 with a tailnet-only feed:
  - **Apple Podcasts** (Library > ··· > Follow a Show by URL): fetches on the phone, works.
  - **Podverse**: fetches on the phone, works.
  - The Podcast App and PodPlayer: their requests never arrived, so they fetch on their servers.
  - Castria: refused the URL because it only accepts ports 80 and 443, another sign of fetching
    on a server.
- **Chapters:** tubecast's files contain YouTube's chapters (a QuickTime chapter track and a Nero
  `chpl` list, moved by yt-dlp to match the cut audio), each episode links a
  `<podcast:chapters>` JSON file built from them, and the chapter timestamps in the episode notes
  are moved by the SponsorBlock cuts (yt-dlp reports them as `sponsorblock_chapters`). Still,
  **Apple Podcasts shows no chapters** for a show followed by URL: it never requests the JSON and
  ignores the file's own chapters, so for shows outside its catalog chapters seem to be processed
  only on Apple's servers. Podverse didn't request the JSON either. Downcast (paid, fetches on the
  phone, supports chapters) is untested.
- **YouTube sometimes refuses the https audio streams** with a 403 (seen 2026-11-18 on a
  video out for 15 hours, whose https streams also needed a PO token), while its HLS (m3u8)
  streams of the same audio still work. On a 403, tubecast downloads again from the HLS stream.
  Videos with auto-dubbed audio tracks list one stream per language; yt-dlp's default order
  picks the original language first, also among the HLS streams.
- **Feeds are one-way.** Played status and positions stay in the app; the server only sees which
  files are fetched. Deleting episodes once played would need a sync protocol such as gPodder,
  and an app that speaks it.

## The "Listen later" feed

One personal feed, filled by hand, next to the channel feeds: you watch a video on the iPad,
share it to tubecast, and it shows up as an episode on the phone's podcast app, downloaded for
offline listening. (Design agreed 2026-11-18.)

- **Adding:** `POST /later` with the video URL as JSON (`{"url": "…"}`) or plain text. It answers
  `202 Accepted` right away, with the video's ID; the download runs in the background, not in the
  hourly check. Any URL yt-dlp handles works (YouTube, X, Instagram, …); SponsorBlock only applies to
  YouTube. Sending a video that is still in the feed moves it to the top (and restarts its 14
  days) without downloading it again. URLs not downloaded yet are kept in the state, so a restart
  doesn't lose them.
- **No login**, like the feeds: tubecast is meant to be reachable only by your own devices.
- **Start time:** a `t=` in the URL (`t=754`, `t=12m34s`) puts "(from 12:34)" at the end of the
  episode title. Podcast apps can't be told where to start, so you skip ahead once.
- **Downloading:** right away, no `download_delay`: you share videos you've already been watching,
  which usually have their SponsorBlock segments. Same audio, cuts and chapters as channel
  episodes. One download at a time, in a queue; a failure is retried once after a minute.
- **Feed:** `/feeds/later.xml`, title "Listen later", listed first on the index page. The
  `pubDate` is when you added the video (so it's at the top of the app), the channel's name and
  the video's upload date go in the notes. GUID: yt-dlp's `extractor:id` (`youtube:abc123`).
  `later` is a reserved slug; a channel can't use it.
- **State:** `state/later.json` (same layout as a channel's, plus `added` and `start` per episode),
  audio in `audio/later/`. Backed up like the rest of `state/`.
- **Retention:** episodes are deleted 14 days after they were added (`later.keep_days`), and the
  oldest go first while the folder is over 5 GB (`later.max_size`). The feed can't know what was
  played (see "Feeds are one-way"), so there's no deleting on download or play.
- **Failure push:** when a download fails twice, tubecast posts "Failed: <title or URL> (<reason>)"
  to `TUBECAST_NTFY_URL` (a full ntfy topic URL, in `.env`; unset = no push). Nothing is sent on
  success: you refresh the show in the app.
- **`/stats`:** adds `later_episodes`.
- **Non-YouTube sites** have no audio-only stream (X, Instagram), so the format falls back to
  `best` and yt-dlp takes the audio out of the video.
- **Config:**

  ```yaml
  later:
    keep_days: 14
    max_size: 5GB
  ```

  Without a `later:` section the feed is off and `/later` answers 404.
- **iOS Shortcut "Listen later"** (share sheet, accepts URLs and Safari pages): POST the URL to
  `/later`, show "Added" or the error. Documented in the README.

## Next

- [ ] Re-check SponsorBlock for a day or two after download and re-cut a file if new segments
  appear (same GUID), as an alternative to waiting before downloading.
- [ ] Try Downcast for chapters.
- [ ] Maybe a small web UI to add channels.
