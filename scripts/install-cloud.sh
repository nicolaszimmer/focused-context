#!/bin/sh
# Install focused for every session of a Claude Code cloud environment, whatever the
# number of repositories in it. Run it from the environment's setup script (the cloud
# environment menu in a session's title bar, then Edit, then Setup script), from a
# checkout of focused-context's main:
#
#   U=https://github.com/nicolaszimmer/focused-context
#   REF=$(git ls-remote "$U" refs/heads/main | cut -f1); [ -n "$REF" ] || exit 1
#   D="$HOME/.cache/focused-context/$REF"
#   [ -f "$D/scripts/install-cloud.sh" ] || { git init -q "$D" &&
#     git -C "$D" fetch -q --depth 1 "$U" "$REF" &&
#     git -C "$D" -c advice.detachedHead=false checkout -q FETCH_HEAD; }
#   sh "$D/scripts/install-cloud.sh" --jev --follow main
#
# It merges focused's hooks into ~/.claude/settings.json and writes /focused-answers,
# /focused-find and /focused-context as personal skills; other settings stay as they are.
#   --jev        ask Jev in the Stop hook: the environment needs FOCUSED_OPENROUTER_API_KEY
#                or OPENROUTER_API_KEY among its variables, and access to openrouter.ai
#   --follow B   keep the install at branch B: each session checks it as it starts and, when
#                it has moved, installs that version for the sessions after (focused/update.py)
#   --uninstall  remove what it installed
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
exec python3 "$root/scripts/focused.py" install --root "$root" "$@"
