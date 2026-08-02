#!/usr/bin/env bash
# One-shot setup for the TrendLive real-money bot on a fresh Ubuntu VPS (22.04/24.04).
# Run as root:   bash deploy_vps.sh
set -euo pipefail

APP=/opt/trend-live
REPO="${TRENDLIVE_REPO:-https://github.com/maxneedsart/Trading.git}"

echo "==> installing python + git"
apt-get update -y
apt-get install -y python3 python3-pip git curl

echo "==> fetching code into $APP"
if [ -d "$APP/.git" ]; then git -C "$APP" pull; else git clone "$REPO" "$APP"; fi

echo "==> python deps (optional: gspread/redis for Sheets logging + state)"
pip3 install --break-system-packages --quiet gspread google-auth redis || \
  echo "   (deps optional — bot still runs; Sheets/Redis just disabled if missing)"

echo "==> this server's OUTBOUND IP (whitelist THIS on your Binance key):"
curl -s https://api.ipify.org || true; echo

if [ ! -f "$APP/.env" ]; then
  cp "$APP/trend-live.env.example" "$APP/.env"
  echo "==> created $APP/.env  —  EDIT IT (add BINANCE keys) before starting:  nano $APP/.env"
fi

echo "==> installing systemd service (auto-restart)"
cp "$APP/trend-live.service" /etc/systemd/system/trend-live.service
systemctl daemon-reload
systemctl enable trend-live

echo "==> installing auto-deploy (server pulls new commits + restarts, every minute)"
chmod +x "$APP/autodeploy.sh" || true
systemctl enable --now cron 2>/dev/null || true
( crontab -l 2>/dev/null | grep -v 'autodeploy.sh' || true ; echo "* * * * * /opt/trend-live/autodeploy.sh" ) | crontab - || true
echo "   auto-deploy on. push from PyCharm -> lands here within ~1 min. log: /var/log/trend-autodeploy.log"

cat <<'DONE'

==============================================================
 Setup complete. Next:
   1) nano /opt/trend-live/.env      # paste BINANCE_API_KEY / SECRET
                                     # keep LIVE_DRY_RUN=true for now
   2) systemctl start trend-live
   3) journalctl -u trend-live -f    # watch logs; find the OUTBOUND IP line,
                                     # whitelist it on the Binance key (+Enable Futures)
   4) confirm the [DRY] order sizes look right, then:
        nano /opt/trend-live/.env    # set LIVE_DRY_RUN=false
        systemctl restart trend-live
 Stop anytime:  systemctl stop trend-live
 Update code:   git -C /opt/trend-live pull && systemctl restart trend-live
==============================================================
DONE
