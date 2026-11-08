import pytest

from tubecast.config import ConfigError, normalize_channel_url, parse, parse_duration

BASE = {"base_url": "https://tube.example.ts.net:8451/"}


def test_defaults():
    config = parse({**BASE, "channels": ["@GamersNexus"]})
    assert config.base_url == "https://tube.example.ts.net:8451"
    assert config.check_every == 4 * 3600
    assert config.download_delay == 6 * 3600
    assert config.sponsorblock == ("sponsor", "selfpromo", "interaction")
    [channel] = config.channels
    assert channel.url == "https://www.youtube.com/@GamersNexus"
    assert channel.slug == "gamersnexus"
    assert (channel.keep, channel.min_duration, channel.include_shorts, channel.include_live) == (5, 0, False, False)


def test_channel_options_and_global_defaults():
    config = parse({**BASE, "keep": 3, "include_shorts": True, "channels": [
        "@One",
        {"url": "https://www.youtube.com/@Two/videos", "keep": 8, "include_shorts": False,
         "include_live": True, "min_duration": "10m", "slug": "second"},
    ]})
    one, two = config.channels
    assert (one.keep, one.include_shorts) == (3, True)
    assert (two.url, two.slug, two.keep, two.include_shorts, two.include_live, two.min_duration) == (
        "https://www.youtube.com/@Two", "second", 8, False, True, 600)


def test_empty_sponsorblock_and_no_channels():
    config = parse({**BASE, "sponsorblock": [], "channels": None})
    assert config.sponsorblock == ()
    assert config.channels == ()


@pytest.mark.parametrize("value, seconds", [
    (90, 90), ("90", 90), ("6h", 21600), ("30m", 1800), ("1h30m", 5400), ("45s", 45), ("2H", 7200),
])
def test_parse_duration(value, seconds):
    assert parse_duration(value, "x") == seconds


@pytest.mark.parametrize("value", ["", "soon", "1d", -5, True, "h"])
def test_parse_duration_rejects(value):
    with pytest.raises(ConfigError):
        parse_duration(value, "x")


@pytest.mark.parametrize("value, url", [
    ("@GamersNexus", "https://www.youtube.com/@GamersNexus"),
    ("https://www.youtube.com/@GamersNexus", "https://www.youtube.com/@GamersNexus"),
    ("youtube.com/@GamersNexus/streams/", "https://www.youtube.com/@GamersNexus"),
    ("https://m.youtube.com/@GamersNexus?si=abc", "https://www.youtube.com/@GamersNexus"),
    ("UChIs72whgZI9w6d6FhwGGHA", "https://www.youtube.com/channel/UChIs72whgZI9w6d6FhwGGHA"),
    ("https://www.youtube.com/channel/UChIs72whgZI9w6d6FhwGGHA/videos",
     "https://www.youtube.com/channel/UChIs72whgZI9w6d6FhwGGHA"),
    ("https://www.youtube.com/c/Someone", "https://www.youtube.com/c/Someone"),
])
def test_normalize_channel_url(value, url):
    assert normalize_channel_url(value) == url


@pytest.mark.parametrize("value", [
    "https://vimeo.com/@someone", "https://www.youtube.com/watch?v=abc", "https://www.youtube.com/",
])
def test_normalize_channel_url_rejects(value):
    with pytest.raises(ConfigError):
        normalize_channel_url(value)


@pytest.mark.parametrize("data, message", [
    ({"channels": []}, "base_url"),
    ({**BASE, "chanels": []}, "unknown setting"),
    ({**BASE, "channels": [{"url": "@A", "kep": 3}]}, "unknown option"),
    ({**BASE, "channels": [{"keep": 3}]}, "mapping with a url"),
    ({**BASE, "channels": [{"url": "@A", "keep": 0}]}, "keep"),
    ({**BASE, "channels": [{"url": "@A", "include_shorts": "yes"}]}, "include_shorts"),
    ({**BASE, "channels": [{"url": "@A", "slug": "Not OK"}]}, "slug"),
    ({**BASE, "skip_shorts": True}, "unknown setting"),
    ({**BASE, "channels": ["@Same", "youtube.com/@same"]}, "share the slug"),
    ({**BASE, "check_every": "10s"}, "check_every"),
    ({**BASE, "sponsorblock": "sponsor"}, "sponsorblock"),
    (["not", "a", "mapping"], "mapping"),
])
def test_errors(data, message):
    with pytest.raises(ConfigError, match=message):
        parse(data)
