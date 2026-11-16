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
    cli_world["enrollment"] = speech_clips(cli_world, 3, start=100)
    run(cli_world, "enroll", "--device", device, *cli_world["enrollment"])
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


@when(
    parsers.re(
        r'the owner calibrates "(?P<device>[^"]+)" from (?P<mine>\d+) clips of their speech '
        r"and (?P<theirs>\d+) clips of other voices including their own voice, saving"
    )
)
def calibrates_contaminated(cli_world, device, mine, theirs):
    others = other_clips(cli_world, int(theirs) - 1) + speech_clips(cli_world, 1, start=300)
    argv = ["calibrate", "--device", device, "--write"]
    argv += ["--owner", *speech_clips(cli_world, int(mine), start=200), "--others", *others]
    run(cli_world, *argv)


@when(
    parsers.parse(
        'the owner calibrates "{device}" using the enrollment clips as their held-out speech'
    )
)
def calibrates_with_enrollment_clips(cli_world, device):
    argv = ["calibrate", "--device", device, "--owner", *cli_world["enrollment"]]
    run(cli_world, *argv, "--others", *other_clips(cli_world, 6))


@then("the command fails and says why the thresholds are unsafe")
def fails_unsafe(cli_world):
    assert cli_world["code"] != 0
    assert "not saved" in cli_world["out"] and "--force" in cli_world["out"]


@then("the command fails and says the clips were used for enrollment")
def fails_enrollment_clips(cli_world):
    assert cli_world["code"] != 0 and "enrollment" in cli_world["out"]
