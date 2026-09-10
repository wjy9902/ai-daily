#!/usr/bin/env bash
# Install the repository's systemd units and reload systemd. Run as root after
# ops/deploy.sh reports that the units differ (exit 3).
#
# It does not enable or disable timers: which timers run is an operator
# decision (docs/plan/COLLECT-THEN-PUBLISH.md §9.6 switches them in stages).
# It prints the timers whose files changed so that decision is not forgotten.
set -euo pipefail

APP_DIR=${AI_DAILY_APP_DIR:-/www/wwwroot/ai-daily/app}
UNIT_DIR=${SYSTEMD_UNIT_DIR:-/etc/systemd/system}

changed=()
for unit in "$APP_DIR"/ops/systemd/*.service "$APP_DIR"/ops/systemd/*.timer; do
    name=$(basename "$unit")
    if ! cmp -s "$unit" "$UNIT_DIR/$name"; then
        install -m 0644 "$unit" "$UNIT_DIR/$name"
        changed+=("$name")
    fi
done

if [ ${#changed[@]} -eq 0 ]; then
    echo "units already current"
    exit 0
fi

systemctl daemon-reload
echo "installed: ${changed[*]}"
for name in "${changed[@]}"; do
    case "$name" in
        *.timer)
            state=$(systemctl is-enabled "$name" 2>/dev/null || echo disabled)
            echo "timer $name is $state; enable or restart it deliberately: systemctl enable --now $name"
            ;;
    esac
done
