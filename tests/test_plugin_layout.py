"""The plugin is mostly wiring. These tests pin the wiring that would fail
silently: a monitor that never starts, a hook that kills the microphone on
/clear, a listener that runs with different dependencies than the tests."""

import json
import os
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"


def load(relative):
    return json.loads((REPO / relative).read_text())


def test_the_marketplace_lists_the_plugin_by_relative_path():
    # A relative-path plugin in a marketplace added from a local directory is
    # loaded in place, so edits here take effect at the next session start.
    market = load(".claude-plugin/marketplace.json")
    (entry,) = market["plugins"]
    assert entry["source"] == "./plugin"
    assert entry["name"] == load("plugin/.claude-plugin/plugin.json")["name"] == "donna"


def test_the_monitor_runs_the_listener_from_the_plugin_root():
    (monitor,) = load("plugin/monitors/monitors.json")
    assert monitor["name"] == "ears"
    assert monitor["command"] == '"${CLAUDE_PLUGIN_ROOT}"/bin/ears listen'
    assert monitor.get("when", "always") == "always"
    assert "${user_config" not in monitor["command"]  # Claude Code refuses those
    assert set(monitor) <= {"name", "command", "description", "when"}  # strict schema


def test_the_launcher_and_entry_point_are_executable():
    assert os.access(PLUGIN / "bin" / "ears", os.X_OK)
    assert (PLUGIN / "lib" / "ears.py").exists()


def hook_commands(event):
    hooks = load("plugin/hooks/hooks.json")["hooks"]
    return [(group.get("matcher"), hook) for group in hooks[event] for hook in group["hooks"]]


def test_session_start_injects_the_protocol_on_every_kind_of_start():
    ((matcher, hook),) = hook_commands("SessionStart")
    # No matcher: startup, resume, clear and compact all need the protocol again.
    assert matcher is None
    assert hook["command"] == '"${CLAUDE_PLUGIN_ROOT}"/bin/ears context'


def test_session_end_stops_the_listener_only_when_the_process_is_really_exiting():
    ((matcher, hook),) = hook_commands("SessionEnd")
    reasons = set(matcher.split("|"))
    # /clear and an in-app /resume end a conversation but not the process. The
    # monitor is armed once per process, so stopping it there would leave the
    # rest of the session deaf.
    assert reasons == {"logout", "prompt_input_exit", "other"}
    assert hook["command"].endswith("/bin/ears stop --quiet")
    assert hook["timeout"] <= 2  # SessionEnd hooks get about 1.5 s


def test_the_listener_runs_with_the_dependencies_the_tests_run_with():
    script = (PLUGIN / "lib" / "ears.py").read_text()
    block = re.search(r"^# /// script\n(.*?)^# ///$", script, re.S | re.M).group(1)
    inline = tomllib.loads("\n".join(line[2:] for line in block.splitlines()))
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert sorted(inline["dependencies"]) == sorted(project["dependencies"])
    assert inline["requires-python"] == project["requires-python"]


def inline_dependencies(script):
    text = (PLUGIN / "lib" / script).read_text()
    block = re.search(r"^# /// script\n(.*?)^# ///$", text, re.S | re.M).group(1)
    inline = tomllib.loads("\n".join(line[2:] for line in block.splitlines()))
    return {re.split(r"[ @<>=~!\[]", item, maxsplit=1)[0] for item in inline["dependencies"]}


def test_the_voice_server_has_its_own_heavier_environment():
    """Kokoro brings PyTorch with it. The listener must not have to wait for
    that at session start, and the test suite must not need it at all."""
    voice = inline_dependencies("speak.py")
    assert {"kokoro", "sounddevice", "numpy"} <= voice
    # Without the spaCy model in the list, Kokoro's text front end tries to
    # `pip install` it at run time, and a uv environment has no pip.
    assert "en-core-web-sm" in voice
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert "kokoro" not in inline_dependencies("ears.py")
    assert not any(item.startswith(("kokoro", "torch")) for item in project["dependencies"])


def test_light_commands_import_nothing_outside_the_standard_library():
    """status, pause, say, the hooks: they must start under a bare python3."""
    heavy = re.compile(
        r"^(?:import|from)\s+(numpy|sounddevice|silero_vad_notorch|onnxruntime|torch|kokoro)\b"
    )
    for module in sorted((PLUGIN / "lib" / "donna_ears").glob("*.py")):
        for number, line in enumerate(module.read_text().splitlines(), 1):
            assert not heavy.match(line), (
                f"{module.name}:{number} imports an audio dependency at module level"
            )
