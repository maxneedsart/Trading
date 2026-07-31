#!/usr/bin/env bash
# Auto-deploy: pull the repo; if there are new commits, refresh the service and restart.
# Installed by deploy_vps.sh to run every minute via cron. The server is a pure MIRROR of the
# repo — never edit code on the server; edit + push from PyCharm and it lands here automatically.
# Secrets live only in /opt/trend-live/.env (git-ignored), so a hard reset never touches them.
APP=/opt/trend-live
cd "$APP" || exit 0

git fetch --quiet origin 2>/dev/null || exit 0
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
LOCAL="$(git rev-parse HEAD 2>/dev/null)"
REMOTE="$(git rev-parse "origin/$BRANCH" 2>/dev/null)"
[ -z "$REMOTE" ] && exit 0

if [ "$LOCAL" != "$REMOTE" ]; then
    git reset --hard "origin/$BRANCH" >/dev/null 2>&1
    cp "$APP/trend-live.service" /etc/systemd/system/trend-live.service 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart trend-live
    echo "$(date -u '+%Y-%m-%d %H:%M:%S') UTC  deployed $REMOTE" >> /var/log/trend-autodeploy.log
fi
