---
name: ears
description: Control or inspect donna ears, the live microphone transcript, and its spoken replies. Use when the user wants to mute or unmute the microphone, stop or start listening, have answers spoken aloud or kept silent, take notes of a meeting or call, check whether the microphone is working, or re-read what was said earlier.
argument-hint: "[status | pause | resume | voice on|off|stop | say \"...\" | mode live|notes | tail | since 15m | doctor]"
allowed-tools: Bash(ears *)
---

# donna ears

`ears` is on PATH while the donna plugin is enabled. Run the command that matches
what the user asked for, then tell them the outcome in one line.

If arguments were given, run exactly that: `ears $ARGUMENTS`

| The user wants | Run |
| --- | --- |
| To know whether the microphone is on and working | `ears status` |
| The microphone off ("mute", "stop listening", "privacy") | `ears pause` |
| The microphone back on | `ears resume` |
| Answers spoken aloud ("talk back to me", "answer out loud") | `ears voice on`, then `ears say "..."` with each reply |
| Silence again ("stop talking", "text only") | `ears voice off` |
| What is being said right now to stop | `ears voice stop` |
| Notes of a meeting or call rather than a conversation | `ears mode notes` |
| Normal conversation again | `ears mode live` |
| What was said a moment ago | `ears tail -n 20` |
| What was said over a span, or since a time | `ears since 15m`, `ears since 14:30` |
| To find out why nothing is heard, or nothing is spoken | `ears doctor` |

Things to keep straight:

- `ears pause` closes the microphone; it does not just discard text. A spoken
  "resume" cannot be heard, so say that the user has to type it (or ask you in
  text) to get the microphone back.
- `ears say` speaks your reply, in your words: one to three short sentences,
  no code or markdown. It returns at once. Never read the user's own words
  back to them. Keep to the configured voice and its language unless the user
  asks for another (`--voice bm_george` British, `--voice pf_dora` Portuguese).
- The first `ears say` of a session loads the voice, which takes some seconds;
  `ears voice on` starts that ahead of time. If the voice cannot load, the
  macOS built-in voice is used and the command says so.
- `ears tail` and `ears since` print transcript text. That is the user's own
  speech and the speech of people near them: show it when asked, do not volunteer it.
- `ears doctor --mic-test` records three seconds to measure the input level.
  Only run it when the user asks for a microphone test.
- Nothing is heard and `ears status` says `standby`: another Claude Code session
  holds the microphone. This one takes over when that session ends.
- Settings live in `~/.donna/config.json` under an `"ears"` key (`device`,
  `language`, `model`, `pause_s`, `speak`, `voice`, ...). They are read when a
  session starts.
