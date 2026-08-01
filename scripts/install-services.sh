#!/usr/bin/env bash
set -euo pipefail
repo=/home/parrot/repos/hermes-fleet-control
config="$HOME/.config/hermes-fleet-control"
units="$HOME/.config/systemd/user"
mkdir -p "$config" "$units"
if [[ ! -f "$config/env" ]]; then
  umask 077
  cat > "$config/env" <<EOF
HFC_ENVIRONMENT=production
HFC_DATABASE_PATH=$repo/data/fleet.db
HFC_ENROLLMENT_SECRET=$(openssl rand -hex 32)
HFC_OPERATOR_TOKEN=$(openssl rand -hex 32)
HFC_HERMES_BINARY=$HOME/.local/bin/hermes
HFC_BASE_URL=http://127.0.0.1:8876
HFC_NODE_STATE=$repo/data/parrot-local.json
HFC_HEARTBEAT_SECONDS=30
EOF
fi
chmod 600 "$config/env"
cp "$repo"/deploy/systemd/hermes-fleet-{api,worker,node}.service "$units/"
systemctl --user daemon-reload
systemctl --user enable --now hermes-fleet-api.service hermes-fleet-worker.service
[[ -f "$repo/data/parrot-local.json" ]] && systemctl --user enable --now hermes-fleet-node.service || true
curl -fsS http://127.0.0.1:8876/healthz
