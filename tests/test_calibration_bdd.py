"""Steps for features/calibration.feature: `ears calibrate` with a stand-in model."""

from __future__ import annotations

from pytest_bdd import given, parsers, scenarios, then, when

from donna_ears import voiceprint
from enroll_helpers import run, speech_clips, write_wav
from synthetic import signal

scenarios("features/calibration.feature")


def other_clips(cli_world, count):
    return [
        write_wav(cli_world["dir"] / f"other-{i}.wav", signal("chirp", 5.0 + i))
        for i in range(count)
    ]


@given(parsers.parse('the owner has enrolled "{device}"'))
def enrolled(cli_world, device):
    run(cli_world, "enroll", "--device", device, *speech_clips(cli_world, 3, start=100))
    assert cli_world["code"] == 0


@when(
    parsers.re(
        r'the owner calibrates "(?P<device>[^"]+)" from (?P<mine>\d+) clips of their speech '
        r"and (?P<theirs>\d+) clips of other voices(?P<saving>, saving)?"
    )
)
def calibrates(cli_world, device, mine, theirs, saving):
    argv = ["calibrate", "--device", device]
    argv += ["--owner", *speech_clips(cli_world, int(mine), start=200)]
    argv += ["--others", *other_clips(cli_world, int(theirs))]
    if saving:
        argv.append("--write")
    run(cli_world, *argv)


@then(parsers.parse('"{device}" is calibrated'))
def is_calibrated(device):
    entry = voiceprint.load().device(device)
    assert entry.calibrated and entry.reject < entry.accept


@then(parsers.parse('"{device}" is not calibrated'))
def is_not_calibrated(device):
    assert not voiceprint.load().device(device).calibrated


@then("the report gives the equal error rate and a bound on false accepts")
def report_numbers(cli_world):
    assert cli_world["code"] == 0
    assert "equal error rate" in cli_world["out"] and "false accepts" in cli_world["out"]


@then("the report says the thresholds were not saved")
def report_dry_run(cli_world):
    assert cli_world["code"] == 0 and "not saved" in cli_world["out"]


@then("the command fails and says how many trials it needs")
def fails_trials(cli_world):
    assert cli_world["code"] != 0 and "at least" in cli_world["out"]


@then("the command fails and says to enroll first")
def fails_enroll(cli_world):
    assert cli_world["code"] != 0 and "enroll" in cli_world["out"]
