#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#     "kokoro>=0.9.4",
#     "numpy>=1.26",
#     "sounddevice>=0.5",
#     "en-core-web-sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl",
# ]
# ///
"""Entry point for the donna voice server (``donna_ears/voice_server.py``).

``ears say`` starts this on demand, under uv, which builds the environment
above the first time. It is kept apart from ``ears.py`` because Kokoro brings
PyTorch with it, and the listener should not have to wait for that.

The spaCy model is listed by URL because Kokoro's text front end otherwise
tries to ``pip install`` it at run time, and a uv environment has no pip.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from donna_ears.voice_server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
