"""Steps for features/enrollment.feature: `ears enroll` with a stand-in model."""

from __future__ import annotations

import stat

from pytest_bdd import given, parsers, scenarios, then, when

from donna_ears import voiceprint
from enroll_helpers import run, silent_clips, speech_clips

scenarios("features/enrollment.feature")


@given("no voiceprint exists")
def no_voiceprint():
    voiceprint.delete()


@given(parsers.parse('the owner has enrolled "{device}"'))
def enrolled(cli_world, device):
    run(cli_world, "enroll", "--device", device, *speech_clips(cli_world, 3, start=100))
    assert cli_world["code"] == 0


@given(parsers.parse('the owner has enrolled "{device}" with another model'))
def enrolled_with_another_model(cli_world, device):
    profile = voiceprint.Profile.new(voiceprint.ModelId("old-model", "f" * 64, "old-features"))
    profile.enroll(device, [[1.0, 0.0]], seconds=30.0, now=1.0)
    voiceprint.save(profile)
    cli_world["before"] = voiceprint.path().read_text()


@when(parsers.parse('the owner enrolls "{device}" from {count:d} clips of their speech'))
def enroll_speech(cli_world, device, count):
    cli_world["clips"] = speech_clips(cli_world, count)
    run(cli_world, "enroll", "--device", device, *cli_world["clips"])


@when(
    parsers.re(
        r'the owner enrolls "(?P<device>[^"]+)" from (?P<count>\d+) clips of their speech '
        r"and (?P<silent>\d+) silent clips?"
    )
)
def enroll_mixed(cli_world, device, count, silent):
    cli_world["silent"] = silent_clips(cli_world, int(silent))
    run(
        cli_world,
        "enroll",
        "--device",
        device,
        *speech_clips(cli_world, int(count)),
        *cli_world["silent"],
    )


@when("the owner deletes their voiceprint")
def delete(cli_world):
    run(cli_world, "enroll", "--delete")


@then(parsers.parse('a voiceprint exists for "{device}" made from {count:d} clips'))
def voiceprint_exists(device, count):
    entry = voiceprint.load().device(device)
    assert entry is not None and entry.clips == count


@then("only the owner can read the voiceprint file")
def private_file():
    assert stat.S_IMODE(voiceprint.path().stat().st_mode) == 0o600


@then("the command does not print the voiceprint itself")
def no_vector_printed(cli_world):
    centroid = voiceprint.load().device("Logitech BRIO").centroid
    assert f"{centroid[0]:.4f}" not in cli_world["out"]
    assert "[" not in cli_world["out"]


@then("the command names the silent clip as skipped")
def names_silent(cli_world):
    assert "silent-0.wav" in cli_world["out"] and "skipped" in cli_world["out"]


@then("the command fails")
def fails(cli_world):
    assert cli_world["code"] != 0


@then("the command fails and says to re-enroll")
def fails_re_enroll(cli_world):
    assert cli_world["code"] != 0 and "re-enroll" in cli_world["out"]


@then("no voiceprint exists")
def no_voiceprint_now():
    assert voiceprint.load() is None


@then(parsers.parse('the voiceprint for "{device}" is unchanged'))
def unchanged(cli_world, device):  # noqa: ARG001
    assert voiceprint.path().read_text() == cli_world["before"]
