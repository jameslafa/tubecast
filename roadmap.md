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

- `tubecast/config.py` reads the YAML; `ytdlp.py` runs and updates yt-dlp; `sync.py` lists
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
- **Feeds are one-way.** Played status and positions stay in the app; the server only sees which
  files are fetched. Deleting episodes once played would need a sync protocol such as gPodder,
  and an app that speaks it.

## Next

- [ ] Re-check SponsorBlock for a day or two after download and re-cut a file if new segments
  appear (same GUID), as an alternative to waiting before downloading.
- [ ] Try Downcast for chapters.
- [ ] Maybe a small web UI to add channels.
