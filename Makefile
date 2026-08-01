.PHONY: install test run demo backup install-services uninstall-services status

install:
	python3 -m venv .venv
	.venv/bin/pip install -e '.[dev]'

test:
	.venv/bin/pytest -v

run:
	.venv/bin/uvicorn hermes_fleet.app:app --app-dir src --host 127.0.0.1 --port 8876

demo:
	.venv/bin/python scripts/demo.py

backup:
	mkdir -p backups
	@out=backups/fleet-$$(date +%Y%m%d-%H%M%S).db; sqlite3 data/fleet.db ".backup '$$out'"; chmod 600 "$$out"; echo "$$out"

install-services:
	./scripts/install-services.sh

uninstall-services:
	./scripts/uninstall-services.sh

status:
	@systemctl --user is-active hermes-fleet-api.service hermes-fleet-worker.service hermes-fleet-node.service
	@curl -fsS http://127.0.0.1:8876/healthz
