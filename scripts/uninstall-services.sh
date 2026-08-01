#!/usr/bin/env bash
set -euo pipefail
systemctl --user disable --now hermes-fleet-node.service hermes-fleet-worker.service hermes-fleet-api.service || true
rm -f "$HOME/.config/systemd/user/hermes-fleet-api.service" "$HOME/.config/systemd/user/hermes-fleet-worker.service" "$HOME/.config/systemd/user/hermes-fleet-node.service"
systemctl --user daemon-reload
printf 'Services removed. Data and secrets were preserved.\n'
