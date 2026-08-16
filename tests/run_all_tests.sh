#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_root=${script_dir:h}

cd "$repo_root/stt-local"
uv run --with pytest pytest -q tests
