#!/bin/bash
# Install, upgrade or remove donna as a pinned release.
#
#   scripts/install.sh                       install the version of this checkout
#   scripts/install.sh --ref v1.0.1          install (or move to) another tag
#   scripts/install.sh --source <git url>    take the release from a remote
#   scripts/install.sh --uninstall           remove the plugin and its marketplace
#
# Why this is not just `claude plugin marketplace add <this directory>`: a
# marketplace added from a directory is loaded in place, so every later edit of
# the checkout would change what all sessions run. Here the plugin is cloned at
# a git tag into Claude Code's plugin cache, which is a snapshot: the checkout
# can be edited, or deleted, without touching the installed copy. The small
# marketplace that points at the tag lives in $DONNA_HOME/marketplace.
#
# Environment: DONNA_HOME (default ~/.donna) holds the marketplace, settings,
# transcripts and models. CLAUDE_CONFIG_DIR moves Claude Code's own state, which
# is how this script is rehearsed without touching the real one.

set -euo pipefail

MARKETPLACE=donna-release
PLUGIN=donna
SCOPE=user

repo="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
home="${DONNA_HOME:-$HOME/.donna}"
dir="$home/marketplace"
source_url=""
ref=""
uninstall=0

usage() { sed -n '2,/^$/{s/^# \{0,1\}//;p;}' "$0"; }
die() { echo "install: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) ref="${2:?--ref needs a tag}"; shift 2 ;;
    --source) source_url="${2:?--source needs a git url}"; shift 2 ;;
    --scope) SCOPE="${2:?--scope needs user, project or local}"; shift 2 ;;
    --uninstall) uninstall=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

command -v claude >/dev/null 2>&1 || die "claude is not on PATH"

installed() { claude plugin list 2>/dev/null | grep -q "${PLUGIN}@${MARKETPLACE}"; }
declared() { claude plugin marketplace list 2>/dev/null | grep -q "❯ ${MARKETPLACE}\$"; }

if [ "$uninstall" -eq 1 ]; then
  installed && claude plugin uninstall "${PLUGIN}@${MARKETPLACE}" --scope "$SCOPE"
  declared && claude plugin marketplace remove "$MARKETPLACE"
  [ -f "$dir/.claude-plugin/marketplace.json" ] && rm "$dir/.claude-plugin/marketplace.json"
  echo "Removed. Transcripts, settings and models are still in $home (delete it to remove them too)."
  exit 0
fi

# What the release needs from the machine. The speech model and the voice are
# downloaded on first use, so they are not checked here.
missing=""
command -v uv >/dev/null 2>&1 || missing="$missing uv"
command -v whisper-server >/dev/null 2>&1 || missing="$missing whisper-cpp"
[ -z "$missing" ] || die "missing:$missing   (brew install$missing)"

[ -n "$source_url" ] || source_url="file://$repo"
if [ -z "$ref" ]; then
  version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$repo/plugin/lib/donna_ears/__init__.py")"
  [ -n "$version" ] || die "cannot read the version from this checkout; pass --ref"
  ref="v$version"
fi

# Fail now, in words, rather than inside `claude plugin install`.
if [ "${source_url#file://}" != "$source_url" ]; then
  git -C "${source_url#file://}" rev-parse -q --verify "refs/tags/$ref" >/dev/null ||
    die "no tag $ref in ${source_url#file://} (git tag $ref <commit>)"
else
  git ls-remote --exit-code --tags "$source_url" "$ref" >/dev/null ||
    die "no tag $ref at $source_url"
fi

mkdir -p "$dir/.claude-plugin"
cat >"$dir/.claude-plugin/marketplace.json" <<EOF
{
  "name": "$MARKETPLACE",
  "owner": { "name": "Pedro Oliveira" },
  "metadata": { "description": "donna, pinned to a release tag" },
  "plugins": [
    {
      "name": "$PLUGIN",
      "source": { "source": "git-subdir", "url": "$source_url", "path": "plugin", "ref": "$ref" },
      "description": "Live microphone transcripts pushed into the Claude Code session while it runs, and replies spoken back."
    }
  ]
}
EOF

# Reinstalling is the upgrade path: the cache is keyed by version, and
# `claude plugin update` only follows a marketplace that has moved.
installed && claude plugin uninstall "${PLUGIN}@${MARKETPLACE}" --scope "$SCOPE" >/dev/null
declared && claude plugin marketplace remove "$MARKETPLACE" >/dev/null
claude plugin marketplace add "$dir" --scope "$SCOPE"
claude plugin install "${PLUGIN}@${MARKETPLACE}" --scope "$SCOPE"

echo
claude plugin list 2>/dev/null | grep -A4 "${PLUGIN}@${MARKETPLACE}" || true
echo
echo "Installed $PLUGIN $ref (scope: $SCOPE). Start a new Claude Code session: monitors are armed at session start."
echo "Every interactive session on this machine will then open the microphone; \`ears pause\` closes it."
echo "Check the installation: ears doctor   (on PATH inside a session, or run the installed bin/ears)"
