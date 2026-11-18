# tubecast

Turns YouTube channels into podcast feeds. tubecast follows a list of channels, downloads their
new videos as audio, cuts out sponsor segments, keeps the newest few episodes and serves one
podcast feed per channel, for any podcast app.

- Regular videos only by default: Shorts and live streams can be added per channel
- Audio only, in YouTube's own AAC audio (m4a): no re-encoding, about 60 MB per hour
- Sponsor reads, self-promotion and "like and subscribe" cut out with
  [SponsorBlock](https://sponsor.ajay.app). YouTube's own ads are never in the download.
- Chapters from YouTube, in the file and as a `<podcast:chapters>` file in the feed, and the
  timestamps in the episode notes moved to match the audio after the sponsor cuts
- Keeps the newest 5 episodes per channel (configurable) and deletes older ones
- One config file; adding a channel is one line. No YouTube API key.
- A "Listen later" feed for single videos you send from your phone's share sheet, from YouTube
  or any other site yt-dlp supports
- Keeps yt-dlp up to date on its own, since YouTube changes break old versions

## Setup

tubecast runs in Docker. Its data folder (`/data` in the container) holds the config, the state
of each channel and the audio files.

```sh
git clone https://github.com/jameslafa/tubecast && cd tubecast
docker compose up -d --build
```

Then create `config.yaml` in the data folder, from [config.example.yaml](config.example.yaml).
With the default named volume:

```sh
docker compose cp config.example.yaml tubecast:/data/config.yaml
```

To keep the data in a folder on the host instead, mount it in a `compose.override.yaml`:

```yaml
services:
  tubecast:
    volumes:
      - ./data:/data
```

tubecast listens on `127.0.0.1:8091` by default. Set `TUBECAST_PUBLISH` in `.env` to publish it
elsewhere, e.g. `TUBECAST_PUBLISH=0.0.0.0:8091`.

## Config

`config.yaml` in the data folder. tubecast reads it again when it changes, and checks a new
channel within 30 seconds.

```yaml
base_url: https://tubecast.example.com
channels:
  - https://www.youtube.com/@GamersNexus
  - url: "@SomeChannel"
    keep: 3
    include_live: true
```

| Setting | Default | |
|---|---|---|
| `base_url` | required | Where your podcast app reaches tubecast. Feed and audio URLs start with it. |
| `check_every` | `4h` | How often to look for new videos. |
| `download_delay` | `6h` | How long a video must be online before it's downloaded. SponsorBlock segments are marked by viewers after a video comes out; the delay gives them time. |
| `sponsorblock` | `[sponsor, selfpromo, interaction]` | [Categories](https://wiki.sponsor.ajay.app/w/Segment_Categories) to cut out. `[]` cuts nothing. |
| `later` | off | The "Listen later" feed: `true`, or a mapping with `keep_days` (default `14`) and `max_size` (default `5GB`). See below. |
| `channels` | | The channels to follow: a handle (`"@name"`, in quotes), a channel URL or a channel ID, or a mapping with `url` and the options below. |

Channel options. Each can also be set at the top level, as the default for every channel.

| Option | Default | |
|---|---|---|
| `keep` | `5` | Newest episodes to keep. |
| `min_duration` | none | Skip videos shorter than this, e.g. `10m`. |
| `include_shorts` | `false` | Also download YouTube Shorts. |
| `include_live` | `false` | Also download live streams once they're finished (never while live). |
| `slug` | from the handle | The channel's name in its feed URL. |

Durations are a number of seconds or a string like `6h`, `30m` or `1h30m`.

When a channel is added, tubecast downloads its newest `keep` episodes right away. Removing a
channel from the config stops its feed; its files stay in `audio/<slug>` and `state/<slug>.json`
in the data folder until you delete them.

### If YouTube asks to sign in

If downloads fail with "Sign in to confirm you're not a bot", export your YouTube cookies to
`cookies.txt` in the data folder ([how](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)).
tubecast passes them to yt-dlp when the file exists.

## Listen later

A feed for single videos: send one, and it's downloaded right away (no `download_delay`) and
shows up at the top of the "Listen later" feed, at `/feeds/later.xml`. Turn it on in the config:

```yaml
later:
  keep_days: 14   # delete episodes this many days after they were sent
  max_size: 5GB   # and the oldest ones while the feed takes more than this
```

Send a video with `POST /later`, the URL as JSON (`{"url": "…"}`), a form field `url` or plain
text (the first URL in it is used):

```sh
curl -d 'https://www.youtube.com/watch?v=…' https://tubecast.example/later
```

- Any site yt-dlp supports works. Sponsor segments are cut only on YouTube.
- A `t=` in the URL (`t=754`, `t=12m34s`) adds "(from 12:34)" to the episode title: podcast
  apps can't be told where to start, so you skip there yourself.
- Sending a video that's still in the feed moves it back to the top, without downloading it
  again; its `keep_days` start over.
- The episode's date is when you sent it, so new ones come first. The notes say who made the
  video and when.
- A failed download is tried again after a minute. If it fails again and `TUBECAST_NTFY_URL` is
  set, tubecast posts the error to that [ntfy](https://ntfy.sh) topic. Nothing is sent on success.
- There's no login, as for the feeds: only expose tubecast to your own devices.

### From the iPhone or iPad share sheet

Make a Shortcut in the Shortcuts app:

1. In its settings, turn on **Show in Share Sheet** and accept **URLs** and **Safari web pages**.
2. **Get Contents of URL**: `https://tubecast.example/later`, method **POST**, request body
   **JSON**, one field `url` set to **Shortcut Input**.
3. **Show Notification**: "Sent to Listen later".

Then in YouTube (or Safari), Share > your Shortcut. Shortcuts sync over iCloud, so it's on all
your devices.

## Adding a feed to your podcast app

Open `base_url` in a browser: the page lists each channel's feed URL. In your podcast app, add a
podcast by URL (often under "Add by URL" or "Follow a show by URL") and paste the feed URL.

Feeds are private: they ask podcast directories not to list them. The podcast app must be able
to reach `base_url`. Apps that fetch feeds through their own servers need it reachable from the
internet; apps that fetch on the phone only need the phone to reach it.

## Status

`/stats` returns JSON for dashboards and update checks: `version`, `yt_dlp`, `channels`,
`episodes`, `later_episodes` (null when the Listen later feed is off), `disk_bytes` (size of all audio files), `last_check` (ISO time of the last finished check), `last_check_ago` (the same as short text,
like `2 min ago` or `3h ago`) and `config_error`. `/healthz`
answers `ok`.

## Backups

Back up `config.yaml` and the `state` folder. The audio files are not worth backing up: tubecast
downloads missing episodes again.

## Settings for the container

| Variable | Default | |
|---|---|---|
| `TUBECAST_UPDATE_YTDLP` | `1` | Update yt-dlp at start and every 24 hours. `0` keeps the version in the image. |
| `TUBECAST_NTFY_URL` | none | An ntfy topic URL (`https://ntfy.sh/<topic>`) for Listen later downloads that failed twice. |
| `TUBECAST_LOG_LEVEL` | `INFO` | `DEBUG` also logs each HTTP request, with its range and client. With compose, set it in `.env`. |

## Development

The tests need Python 3.13 and don't need yt-dlp or network access:

```sh
pip install -e ".[test]"
pytest
```

Or in Docker:

```sh
docker run --rm -v "$PWD":/src -w /src python:3.13-slim sh -c 'pip install -q -e ".[test]" && pytest'
```

## License

MIT
