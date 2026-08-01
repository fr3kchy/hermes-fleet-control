import json
import os
import time
from pathlib import Path

import httpx

BASE = os.getenv("HFC_BASE_URL", "http://127.0.0.1:8876")
ENROL = os.environ["HFC_ENROLLMENT_SECRET"]
OPERATOR = os.environ["HFC_OPERATOR_TOKEN"]
headers = {"Authorization": f"Bearer {OPERATOR}"}

with httpx.Client(base_url=BASE, timeout=240, headers=headers) as client:
    fleet = client.get("/api/v1/fleet").raise_for_status().json()
    if not fleet["nodes"]:
        token = client.post("/api/v1/enrollment/tokens", json={"node_name": "parrot-local"}, headers={"X-Enrollment-Secret": ENROL}).raise_for_status().json()["token"]
        from hermes_fleet.connector import enroll_and_heartbeat
        enrolled = enroll_and_heartbeat(BASE, token, "parrot-local", Path("data/parrot-local.json"))
        node_id = enrolled["enrollment"]["node_id"]
    else:
        node_id = fleet["nodes"][0]["id"]
    task = client.post("/api/v1/tasks", headers={"Idempotency-Key": f"demo-{time.time_ns()}"}, json={"node_id": node_id, "profile": "default", "prompt": "Return only: HERMES_FLEET_OK"}).raise_for_status().json()
    for _ in range(120):
        task = client.get(f"/api/v1/tasks/{task['id']}").raise_for_status().json()
        if task["status"] in {"succeeded", "failed", "timed_out", "cancelled"}:
            break
        time.sleep(0.5)
    events = client.get("/api/v1/events").raise_for_status().json()
print(json.dumps({"task": task, "event_count": len(events["events"])}, indent=2))
if task["status"] != "succeeded" or task["output"].strip() != "HERMES_FLEET_OK":
    raise SystemExit(1)
