"""Persistent JSONL worker for offline Kokoro speech."""

import json
import sys

from donna.text_to_speech.kokoro_mcp_server import get_pipeline, speak


def main() -> None:
    get_pipeline("a")
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        paths = speak(
            request["text"],
            voice=request["voice"],
            play=True,
            output_dir=request["output_dir"],
        )
        print(json.dumps({"paths": paths}), flush=True)


if __name__ == "__main__":
    main()
