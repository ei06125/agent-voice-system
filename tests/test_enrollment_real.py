"""`ears enroll` with the real default model on public clips (opt-in assets)."""

from __future__ import annotations

from donna_ears import cli, enroll, speaker, speaker_models, voiceprint


def test_enrolling_one_public_speaker_then_scoring_held_out_clips(
    speaker_assets, monkeypatch, capsys
):
    monkeypatch.setattr(speaker_models, "ensure", speaker_assets.model_file)
    clips = [str(speaker_assets.clip_file(f"leijun-sr-{i}")) for i in (1, 2)]
    assert cli.main(["enroll", "--device", "test-mic", *clips]) == 0
    assert "enrolled 'test-mic': 2 clip(s)" in capsys.readouterr().out

    profile = voiceprint.load()
    centre = profile.device("test-mic").centroid
    embedder = enroll.make_embedder(speaker_assets.model_file(speaker_models.DEFAULT))
    profile.check(
        voiceprint.ModelId(
            speaker_models.DEFAULT,
            speaker_models.MODELS[speaker_models.DEFAULT].sha256,
            embedder.preprocessing,
        )
    )

    def score(name):
        from donna_ears import audio

        samples = audio.decode_file(str(speaker_assets.clip_file(name)))
        return speaker.score(embedder.embed(samples), centre)

    same = [score(f"leijun-test-sr-{i}") for i in (1, 2, 3)]
    others = [score(name) for name in speaker_assets.CLIPS if not name.startswith("leijun")]
    assert min(same) > max(others)
