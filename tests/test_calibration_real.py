"""`ears calibrate` with the real default model on public clips (opt-in assets)."""

from __future__ import annotations

from donna_ears import cli, speaker_models, voiceprint


def test_calibrating_a_public_speaker_against_the_others(speaker_assets, monkeypatch, capsys):
    monkeypatch.setattr(speaker_models, "ensure", speaker_assets.model_file)
    clip = speaker_assets.clip_file
    enrollment = [str(clip(f"leijun-sr-{i}")) for i in (1, 2)]
    held_out = [str(clip(f"leijun-test-sr-{i}")) for i in (1, 2, 3)]
    others = [str(clip(name)) for name in speaker_assets.CLIPS if not name.startswith("leijun")]
    assert cli.main(["enroll", "--device", "test-mic", *enrollment]) == 0
    capsys.readouterr()

    code = cli.main(
        ["calibrate", "--device", "test-mic", "--owner", *held_out, "--others", *others, "--write"]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    entry = voiceprint.load().device("test-mic")
    assert entry.calibrated and entry.reject < entry.accept
    assert "0% of your trials rejected" in out
