import pytest

from tubecast.chapters import after_cuts, merge, shift_timestamps, to_json

# The sponsor segments cut from a GamersNexus video (yt-dlp's sponsorblock_chapters)
CUTS = merge([(342.681, 395.609), (1038.24, 1050.64), (1394.441, 1433.487),
              (2259.191, 2260.012), (2260.011, 2303)])


def test_merge_overlapping_and_unsorted():
    assert merge([(50, 60), (10, 20), (15, 30), (60, 61), (70, 70)]) == [(10, 30), (50, 61)]
    assert CUTS[-1] == (2259.191, 2303)


@pytest.mark.parametrize("t, expected", [
    (0, 0),
    (342, 342),
    (370, 342.681),  # inside the sponsor: lands where it was cut
    (395.609, 342.681),
    (402, 402 - 52.928),
    (1100, 1100 - 52.928 - 12.4),
])
def test_after_cuts(t, expected):
    assert after_cuts(t, CUTS) == pytest.approx(expected)


def test_shift_timestamps():
    text = ("Watch our review.\n\nTimestamps:\n00:00 - The RTX 5090 Problem\n06:35 - 5090 ID Verification\n"
            "12:02 - The Timeline\n(24:10) Prices\n- 1:02:03 Long one\nDoors open at 10:30 for the event.")
    assert shift_timestamps(text, CUTS) == (
        "Watch our review.\n\nTimestamps:\n00:00 - The RTX 5090 Problem\n05:42 - 5090 ID Verification\n"
        "11:09 - The Timeline\n(22:25) Prices\n- 0:59:34 Long one\nDoors open at 10:30 for the event.")


def test_shift_timestamps_gains_hours_only_when_needed():
    assert shift_timestamps("1:00:30 End", [(0, 60)]) == "0:59:30 End"
    assert shift_timestamps("59:30 x", [(0, 1)]) == "59:29 x"
    assert shift_timestamps("06:35 x", []) == "06:35 x"


def test_to_json():
    assert to_json([{"start": 0.0, "title": "Intro"}, {"start": 342.6814, "title": "ID"}]) == {
        "version": "1.2.0", "chapters": [{"startTime": 0.0, "title": "Intro"}, {"startTime": 342.681, "title": "ID"}]}
