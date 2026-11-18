import pytest

from tubecast.ytdlp import FORMAT, HLS_FORMAT, YtDlp, YtDlpError


def fake_run(errors):
    """A YtDlp._run that fails with the next error for each call, then reports a file."""
    calls = []

    def run(self, args, timeout):
        calls.append(args[args.index("-f") + 1])
        if errors:
            raise YtDlpError(errors.pop(0))
        return '{"filepath": "/data/audio/x/abc.m4a", "sponsorblock_chapters": null}\n'
    return run, calls


def test_download_falls_back_to_hls_on_403(tmp_path, monkeypatch):
    run, calls = fake_run(["ERROR: unable to download video data: HTTP Error 403: Forbidden"])
    monkeypatch.setattr(YtDlp, "_run", run)
    path, segments = YtDlp(tmp_path).download("https://youtu.be/abc", tmp_path, ())
    assert calls == [FORMAT, HLS_FORMAT]
    assert path.name == "abc.m4a" and segments == []


def test_download_other_errors_are_not_retried(tmp_path, monkeypatch):
    run, calls = fake_run(["ERROR: Video unavailable"])
    monkeypatch.setattr(YtDlp, "_run", run)
    with pytest.raises(YtDlpError, match="unavailable"):
        YtDlp(tmp_path).download("https://youtu.be/abc", tmp_path, ())
    assert calls == [FORMAT]
