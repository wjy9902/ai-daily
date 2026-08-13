#!/usr/bin/env bash
# Deploy a pinned revision of the digest to the server.
#
# A bare `git pull` is not a deployment: it can change code while a run is in
# flight, and it leaves dependencies stale. This takes the publication lock,
# moves to an explicit revision, syncs the frozen lockfile and refuses to
# finish unless the configuration still loads.
#
# Usage:  ops/deploy.sh <git-ref>
#         ops/deploy.sh --rollback        # back to the previously deployed ref
set -euo pipefail

APP_DIR=${AI_DAILY_APP_DIR:-/www/wwwroot/ai-daily/app}
SITE_ROOT=${AI_DAILY_SITE_ROOT:-/www/wwwroot/ai-daily}
UV=${UV_BIN:-/home/ai-daily/.local/bin/uv}
LOCK_FILE="$SITE_ROOT/.publish.lock"
DEPLOYED_REF_FILE="$SITE_ROOT/.deployed-ref"
PREVIOUS_REF_FILE="$SITE_ROOT/.previous-ref"

if [ $# -ne 1 ]; then
    echo "usage: $0 <git-ref> | --rollback" >&2
    exit 2
fi

if [ "$1" = "--rollback" ]; then
    if [ ! -f "$PREVIOUS_REF_FILE" ]; then
        echo "no previous revision recorded; nothing to roll back to" >&2
        exit 1
    fi
    TARGET_REF=$(cat "$PREVIOUS_REF_FILE")
    echo "rolling back to $TARGET_REF"
else
    TARGET_REF="$1"
fi

# Hold the same lock the publisher uses, so a deploy can never land mid-run.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "a publication run holds the lock; deploy aborted" >&2
    exit 1
fi

cd "$APP_DIR"

CURRENT_REF=$(git rev-parse HEAD)
echo "current: $CURRENT_REF"

git fetch --tags --prune origin
git checkout --detach "$TARGET_REF"
NEW_REF=$(git rev-parse HEAD)
echo "deploying: $NEW_REF"

"$UV" sync --frozen --no-dev

# Refuse to leave the server on a revision whose config does not load: that
# failure would otherwise surface at 04:20 as a missing issue.
if ! "$UV" run --frozen python -c "
from pathlib import Path
from ai_daily.config import load_config
config = load_config(Path('config'))
print(f'config ok: {len(config.sources)} sources')
"; then
    echo "config validation failed; rolling back to $CURRENT_REF" >&2
    git checkout --detach "$CURRENT_REF"
    "$UV" sync --frozen --no-dev
    exit 1
fi

echo "$CURRENT_REF" >"$PREVIOUS_REF_FILE"
echo "$NEW_REF" >"$DEPLOYED_REF_FILE"
echo "deployed $NEW_REF"
