"""Command-line entry point for offline Kokoro speech."""

import argparse
import json
from pathlib import Path

from donna.text_to_speech.kokoro_mcp_server import speak


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("text")
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    paths = speak(
        args.text,
        voice=args.voice,
        play=True,
        output_dir=str(args.output_dir),
    )
    print(json.dumps(paths))


if __name__ == "__main__":
    main()
