#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "numpy>=1.26",
#     "onnxruntime>=1.18",
#     "silero-vad-notorch>=6.2",
#     "sounddevice>=0.5",
# ]
# ///
"""Entry point for donna ears.

Run through ``bin/ears``, which picks the interpreter: uv with the audio
dependencies above for ``listen`` and ``doctor``, a bare ``python3`` for
everything else.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from donna_ears.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
