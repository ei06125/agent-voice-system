"""The owner's voiceprint: one centroid per input device, stored privately."""

from __future__ import annotations

import json
import stat

import pytest

from donna_ears import voiceprint

MODEL = voiceprint.ModelId(name="m", sha256="a" * 64, preprocessing="p1")


def unit(*values):
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


def test_there_is_no_profile_until_someone_enrolls():
    assert voiceprint.load() is None


def test_enrolling_a_device_stores_a_private_profile(donna_home):
    profile = voiceprint.Profile.new(MODEL)
    profile.enroll("Logitech BRIO", [unit(1, 0), unit(1, 0.2)], seconds=42.0, now=100.0)
    voiceprint.save(profile)

    path = donna_home / "voiceprint.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = voiceprint.load()
    brio = loaded.device("Logitech BRIO")
    assert brio.clips == 2 and brio.seconds == 42.0 and brio.enrolled_at == 100.0
    assert abs(sum(v * v for v in brio.centroid) - 1) < 1e-6
    assert brio.accept is None and brio.reject is None


def test_a_second_device_keeps_the_first():
    profile = voiceprint.Profile.new(MODEL)
    profile.enroll("Logitech BRIO", [unit(1, 0)], seconds=30.0)
    profile.enroll("AirPods", [unit(0, 1)], seconds=30.0)
    voiceprint.save(profile)
    assert sorted(voiceprint.load().devices) == ["AirPods", "Logitech BRIO"]


def test_re_enrolling_a_device_replaces_it_and_forgets_its_thresholds():
    profile = voiceprint.Profile.new(MODEL)
    profile.enroll("AirPods", [unit(1, 0)], seconds=30.0)
    profile.calibrate("AirPods", accept=0.6, reject=0.3)
    profile.enroll("AirPods", [unit(0, 1)], seconds=20.0)
    airpods = profile.device("AirPods")
    assert airpods.centroid == pytest.approx(unit(0, 1)) and airpods.accept is None


def test_thresholds_must_leave_room_for_unknown():
    profile = voiceprint.Profile.new(MODEL)
    profile.enroll("AirPods", [unit(1, 0)], seconds=30.0)
    with pytest.raises(ValueError):
        profile.calibrate("AirPods", accept=0.3, reject=0.5)
    with pytest.raises(KeyError):
        profile.calibrate("Brio", accept=0.6, reject=0.3)


def test_an_unknown_device_has_no_profile():
    profile = voiceprint.Profile.new(MODEL)
    profile.enroll("AirPods", [unit(1, 0)], seconds=30.0)
    assert profile.device("MacBook Pro Microphone") is None


def test_a_profile_made_with_another_model_or_preprocessing_is_refused():
    profile = voiceprint.Profile.new(MODEL)
    profile.check(MODEL)
    other_model = voiceprint.ModelId(name="m", sha256="b" * 64, preprocessing="p1")
    other_features = voiceprint.ModelId(name="m", sha256="a" * 64, preprocessing="p2")
    for model in (other_model, other_features):
        with pytest.raises(voiceprint.ProfileError, match="re-enroll"):
            profile.check(model)


def test_a_corrupt_profile_is_an_error_not_an_empty_profile(donna_home):
    (donna_home / "voiceprint.json").write_text("{not json")
    with pytest.raises(voiceprint.ProfileError):
        voiceprint.load()
    (donna_home / "voiceprint.json").write_text(json.dumps({"version": 99}))
    with pytest.raises(voiceprint.ProfileError):
        voiceprint.load()


def test_delete_removes_the_voiceprint(donna_home):
    profile = voiceprint.Profile.new(MODEL)
    profile.enroll("AirPods", [unit(1, 0)], seconds=30.0)
    voiceprint.save(profile)
    assert voiceprint.delete() is True
    assert not (donna_home / "voiceprint.json").exists()
    assert voiceprint.delete() is False


def test_enrolling_needs_at_least_one_embedding():
    with pytest.raises(ValueError):
        voiceprint.Profile.new(MODEL).enroll("AirPods", [], seconds=0.0)
