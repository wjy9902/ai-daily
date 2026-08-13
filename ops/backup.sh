#!/usr/bin/env bash
# Push published records to the backup repository.
#
# Only `published/*.json` is backed up. That is the whole recoverable state:
# HTML is re-renderable from it, artifacts are for post-mortems and are not
# worth the space, and secrets are deliberately not backed up anywhere.
#
# Failure here must never fail a publication: the issue is already live, and a
# GitHub outage is not a reason to call the morning a loss. The exit code is
# reported so the caller can record it in status.json, but the caller ignores
# it for the purposes of deciding whether the day succeeded.
set -uo pipefail

SITE_ROOT=${AI_DAILY_SITE_ROOT:-/www/wwwroot/ai-daily}
BACKUP_DIR=${AI_DAILY_BACKUP_DIR:-/www/wwwroot/ai-daily/.backup}
BACKUP_REMOTE=${AI_DAILY_BACKUP_REMOTE:-github-backup:wjy9902/ai-daily-site-backup.git}

if [ ! -d "$SITE_ROOT/published" ]; then
    echo "no published directory at $SITE_ROOT/published" >&2
    exit 1
fi

if [ ! -d "$BACKUP_DIR/.git" ]; then
    rm -rf "$BACKUP_DIR"
    if ! git clone --quiet "$BACKUP_REMOTE" "$BACKUP_DIR" 2>/dev/null; then
        # An empty repository has no branch to clone; start one locally.
        mkdir -p "$BACKUP_DIR"
        git -C "$BACKUP_DIR" init --quiet --initial-branch=main
        git -C "$BACKUP_DIR" remote add origin "$BACKUP_REMOTE"
    fi
fi

git -C "$BACKUP_DIR" config user.email "ai-daily@jiayutool.cn"
git -C "$BACKUP_DIR" config user.name "ai-daily backup"

mkdir -p "$BACKUP_DIR/published"
cp -f "$SITE_ROOT"/published/*.json "$BACKUP_DIR/published/" 2>/dev/null || true
cp -f "$SITE_ROOT"/status/status.json "$BACKUP_DIR/status.json" 2>/dev/null || true

git -C "$BACKUP_DIR" add -A
if git -C "$BACKUP_DIR" diff --cached --quiet; then
    echo "backup: nothing changed"
    exit 0
fi

COUNT=$(find "$SITE_ROOT/published" -name '*.json' | wc -l | tr -d ' ')
git -C "$BACKUP_DIR" commit --quiet -m "backup: $COUNT published issues"
if git -C "$BACKUP_DIR" push --quiet origin HEAD:main 2>/dev/null; then
    echo "backup: pushed $COUNT issues"
    exit 0
fi
echo "backup: push failed (publication is unaffected)" >&2
exit 1
