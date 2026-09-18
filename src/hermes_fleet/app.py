import hashlib
import json
import secrets
import time
import uuid
import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse

from .config import Settings
from .auth import require_operator
from .db import Database, now
from .events import EventBroker
from .hermes_adapter import HermesCliAdapterV1
from .schemas import AssignmentAck, BenchmarkSample, CancelRequest, EnrollRequest, EnrollmentTokenRequest, FleetTaskCreate, HeartbeatRequest, NodeLifecycleEvent, NodePolicy, ReconcileRequest, TaskRequest
from .security import fresh_timestamp, hash_secret, random_token, sign_request, verify_request_signature, verify_signature
from .routing import route
from .supervisor import FleetSupervisor


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    db = Database(cfg.database_path)
    broker = EventBroker()
    adapter = HermesCliAdapterV1(cfg.hermes_binary, cfg.task_timeout_seconds)
    operator_auth = require_operator(cfg.operator_token)
    supervisor = FleetSupervisor(db, cfg.reconciliation_seconds, cfg.hermes_wake_url, cfg.hermes_wake_key)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.supervisor_task = asyncio.create_task(supervisor.run())
        try:
            yield
        finally:
            supervisor.stop()
            await application.state.supervisor_task

    app = FastAPI(title="Hermes Fleet Control", version="0.2.0", lifespan=lifespan)
    app.state.settings = cfg
    app.state.db = db
    app.state.supervisor = supervisor
    app.state.supervisor_task = None

    async def authenticate_node(request: Request, node_id: str) -> dict:
        node = db.get_node(node_id, include_secret=True)
        if not node:
            raise HTTPException(404, "Node not found")
        timestamp = request.headers.get("X-Node-Timestamp", "")
        nonce = request.headers.get("X-Node-Nonce", "")
        signature = request.headers.get("X-Node-Signature", "")
        if not nonce or len(nonce) > 256 or not fresh_timestamp(timestamp, cfg.signature_max_age_seconds):
            raise HTTPException(401, "Node request timestamp is expired or nonce is missing")
        body = await request.body()
        if not verify_request_signature(node["secret"], request.method, request.url.path, timestamp, nonce, body, signature):
            raise HTTPException(401, "Node request signature is invalid")
        if not db.use_nonce(node_id, nonce, time.time(), time.time() - cfg.signature_max_age_seconds * 2):
            raise HTTPException(409, "Node request nonce was already used")
        return node

    def signed_json(node: dict, path: str, payload: dict, status_code: int = 200) -> JSONResponse:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        timestamp, nonce = str(int(time.time())), secrets.token_urlsafe(18)
        headers = {
            "X-Coordinator-Timestamp": timestamp,
            "X-Coordinator-Nonce": nonce,
            "X-Coordinator-Signature": sign_request(node["secret"], "RESPONSE", path, timestamp, nonce, body),
        }
        return JSONResponse(content=payload, status_code=status_code, headers=headers)

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    @app.get("/ui", response_class=HTMLResponse)
    def dashboard() -> str:
        return """<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Hermes Fleet Control</title><style>body{font-family:system-ui;background:#0b1020;color:#e6edf3;max-width:1100px;margin:auto;padding:2rem}input,button{padding:.7rem;background:#161f35;color:#fff;border:1px solid #40527a}table{width:100%;border-collapse:collapse;margin-top:1rem}td,th{padding:.7rem;border-bottom:1px solid #28344f;text-align:left}.error{color:#ff7b72}.online{color:#3fb950}</style></head><body><h1>Fleet Overview</h1><p>Backend-confirmed node state. Enter the operator token to load live data.</p><input id='token' type='password' aria-label='Operator token'><button onclick='loadFleet()'>Connect</button><div id='state'>Not connected.</div><table><thead><tr><th>Node</th><th>Status</th><th>Last seen</th></tr></thead><tbody id='nodes'></tbody></table><script>async function loadFleet(){const state=document.getElementById('state');state.textContent='Loading backend state…';try{const response=await fetch('/api/v1/fleet',{headers:{Authorization:'Bearer '+document.getElementById('token').value}});if(!response.ok){const body=await response.json();throw new Error(body.detail||('HTTP '+response.status));}const data=await response.json();document.getElementById('nodes').innerHTML=data.nodes.map(n=>`<tr><td>${escapeHtml(n.name)}</td><td class='${n.status==='online'?'online':''}'>${escapeHtml(n.status)}</td><td>${escapeHtml(n.last_seen||'never')}</td></tr>`).join('');state.textContent=`Backend confirmed ${data.nodes.length} node(s).`;}catch(error){state.className='error';state.textContent='Fleet load failed: '+error.message+' Check the operator token and API service status.';}}function escapeHtml(value){const d=document.createElement('div');d.textContent=String(value);return d.innerHTML;}</script></body></html>"""

    @app.get("/healthz")
    def health() -> dict:
        with db.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(_: None = Depends(operator_auth)) -> str:
        with db.connect() as connection:
            nodes = connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            tasks = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            queued = connection.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
        return f"hermes_fleet_nodes {nodes}\nhermes_fleet_tasks {tasks}\nhermes_fleet_tasks_queued {queued}\n"

    @app.post("/api/v1/enrollment/tokens", status_code=status.HTTP_201_CREATED)
    def issue_token(request: EnrollmentTokenRequest, x_enrollment_secret: str = Header(default="")) -> dict:
        if not secrets.compare_digest(x_enrollment_secret, cfg.enrollment_secret):
            raise HTTPException(401, "Enrollment authorisation failed. Supply the configured X-Enrollment-Secret over a trusted local or TLS connection.")
        token = random_token()
        expires_at = time.time() + cfg.enrollment_ttl_seconds
        with db.connect() as connection:
            connection.execute("INSERT INTO enrollment_tokens(token_hash,node_name,expires_at) VALUES(?,?,?)", (hash_secret(token), request.node_name, expires_at))
        return {"token": token, "node_name": request.node_name, "expires_at": expires_at}

    @app.post("/api/v1/nodes/enroll", status_code=status.HTTP_201_CREATED)
    async def enroll(request: EnrollRequest) -> dict:
        token_hash = hash_secret(request.token)
        with db.connect() as connection:
            row = connection.execute("SELECT * FROM enrollment_tokens WHERE token_hash=?", (token_hash,)).fetchone()
            if not row or row["used_at"] or row["expires_at"] < time.time() or row["node_name"] != request.node_name:
                raise HTTPException(403, "Enrollment token is invalid, expired, used, or bound to another node. Issue a new node-bound token.")
            node_id, node_secret, created = str(uuid.uuid4()), random_token(), now()
            try:
                connection.execute("INSERT INTO nodes(id,name,secret,status,capabilities,metrics,created_at,policy) VALUES(?,?,?,?,?,?,?,?)", (node_id, request.node_name, node_secret, "enrolled", json.dumps(request.capabilities), "{}", created, json.dumps({"trust_zone": "trusted", "data_classification": "internal", "credential_domains": [], "max_risk": 1, "max_concurrency": 2, "profile_concurrency": {}})))
            except Exception as exc:
                raise HTTPException(409, "Node name already exists. Revoke or rename the existing node before enrolling.") from exc
            connection.execute("UPDATE enrollment_tokens SET used_at=? WHERE token_hash=?", (created, token_hash))
        correlation_id = str(uuid.uuid4())
        event = db.event("node.enrolled", correlation_id, {"node_id": node_id, "node_name": request.node_name})
        await broker.publish(event)
        return {"node_id": node_id, "node_secret": node_secret, "status": "enrolled", "correlation_id": correlation_id}

    @app.post("/api/v1/nodes/{node_id}/heartbeat")
    async def heartbeat(node_id: str, request: HeartbeatRequest, x_node_signature: str = Header(default="")) -> dict:
        payload = request.model_dump()
        with db.connect() as connection:
            row = connection.execute("SELECT secret,capabilities FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Node not found. Enrol the node before sending heartbeats.")
            if not verify_signature(row["secret"], payload, x_node_signature):
                raise HTTPException(401, "Heartbeat signature is invalid. Verify the node secret and canonical JSON signing.")
            seen, previous_status = now(), connection.execute("SELECT status FROM nodes WHERE id=?", (node_id,)).fetchone()[0]
            capabilities = {**json.loads(row["capabilities"]), **request.capabilities}
            for operator_owned in ("trust_zone", "data_classification", "credential_domains", "max_risk", "max_concurrency", "profile_concurrency"):
                capabilities.pop(operator_owned, None)
            old_capability_hash = hashlib.sha256(json.dumps(json.loads(row["capabilities"]), sort_keys=True).encode()).hexdigest()
            new_capability_hash = hashlib.sha256(json.dumps(capabilities, sort_keys=True).encode()).hexdigest()
            connection.execute("UPDATE nodes SET status=?,metrics=?,capabilities=?,last_seen=? WHERE id=?", (request.status, json.dumps(request.metrics), json.dumps(capabilities), seen, node_id))
        correlation_id = str(uuid.uuid4())
        if previous_status != request.status or old_capability_hash != new_capability_hash:
            event_type = "node.transition" if previous_status != request.status else "node.capabilities_changed"
            await broker.publish(db.event(event_type, correlation_id, {"node_id": node_id, "status": request.status, "last_seen": seen}))
        return {"accepted": True, "node_id": node_id, "status": request.status, "correlation_id": correlation_id}

    @app.get("/api/v1/fleet")
    def fleet(_: None = Depends(operator_auth)) -> dict:
        return {"nodes": db.fleet()}

    @app.post("/api/v1/tasks", status_code=status.HTTP_202_ACCEPTED)
    async def dispatch(request: TaskRequest, _: None = Depends(operator_auth), idempotency_key: str | None = Header(default=None)) -> dict:
        if not any(node["id"] == request.node_id for node in db.fleet()):
            raise HTTPException(404, "Target node not found. Enrol it and confirm a heartbeat before dispatch.")
        task_id, correlation_id, created = str(uuid.uuid4()), str(uuid.uuid4()), now()
        task = db.enqueue_task({"id": task_id, "node_id": request.node_id, "profile": request.profile, "prompt": request.prompt, "correlation_id": correlation_id, "adapter_version": adapter.adapter_version, "created_at": created, "idempotency_key": idempotency_key})
        if task["id"] == task_id:
            await broker.publish(db.event("task.queued", correlation_id, {"task_id": task_id, "node_id": request.node_id, "profile": request.profile}))
        return task

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str, _: None = Depends(operator_auth)) -> dict:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found. Verify the task ID or event correlation ID.")
        return task

    @app.get("/api/v1/events")
    def list_events(after: int = 0, _: None = Depends(operator_auth)) -> dict:
        return {"events": db.events(after)}

    @app.get("/api/v2/fleet")
    def fleet_v2(_: None = Depends(operator_auth)) -> dict:
        return {"protocol_version": "2.0", "nodes": db.fleet()}

    @app.get("/api/v2/nodes")
    def nodes_v2(_: None = Depends(operator_auth)) -> dict:
        return {"protocol_version": "2.0", "nodes": db.fleet()}

    @app.get("/api/v2/nodes/{node_id}")
    def node_v2(node_id: str, _: None = Depends(operator_auth)) -> dict:
        node = db.get_node(node_id)
        if not node:
            raise HTTPException(404, "Node not found")
        return {"protocol_version": "2.0", "node": node}

    @app.put("/api/v2/nodes/{node_id}/policy")
    async def node_policy_v2(node_id: str, policy: NodePolicy, _: None = Depends(operator_auth)) -> dict:
        if not db.get_node(node_id):
            raise HTTPException(404, "Node not found")
        with db.connect() as connection:
            connection.execute("UPDATE nodes SET policy=? WHERE id=?", (json.dumps(policy.model_dump()), node_id))
        await broker.publish(db.event("node.policy_changed", node_id, {"node_id": node_id, "policy": policy.model_dump(exclude={"credential_domains"}), "credential_domain_count": len(policy.credential_domains)}))
        return {"protocol_version": "2.0", "node": db.get_node(node_id)}

    @app.post("/api/v2/tasks", status_code=status.HTTP_202_ACCEPTED)
    async def create_task_v2(request: FleetTaskCreate, _: None = Depends(operator_auth), idempotency_key: str | None = Header(default=None)) -> dict:
        payload = request.model_dump(mode="json")
        key = idempotency_key or request.idempotency_key or f"fleet:{request.task_id or uuid.uuid4()}"
        candidates = db.fleet()
        if request.destination_node_id:
            candidates = [node for node in candidates if node["id"] == request.destination_node_id]
            if not candidates:
                raise HTTPException(404, "Target node not found")
        with db.connect() as connection:
            active_rows = connection.execute("SELECT node_id,profile,COUNT(*) count FROM tasks WHERE status IN ('assigned','accepted','running','review','verifying') GROUP BY node_id,profile").fetchall()
        if sum(row["count"] for row in active_rows) >= cfg.global_concurrency:
            raise HTTPException(409, {"code": "GLOBAL_CONCURRENCY_EXHAUSTED", "limit": cfg.global_concurrency})
        if request.delegation_depth > 3:
            raise HTTPException(409, {"code": "DELEGATION_DEPTH_EXCEEDED", "limit": 3})
        selected, explanation = route(candidates, payload["requirements"], request.profile, request.risk_class, {(row["node_id"], row["profile"]): row["count"] for row in active_rows})
        if not selected:
            raise HTTPException(409, {"code": "NO_COMPATIBLE_NODE", "route": explanation})
        task_id, correlation_id, created = str(request.task_id or uuid.uuid4()), str(request.event_id or uuid.uuid4()), now()
        task, inserted = db.create_fleet_task({
            "id": task_id, "node_id": selected, "profile": request.profile, "prompt": request.objective,
            "status": "requested", "output": "", "error": "", "correlation_id": correlation_id,
            "adapter_version": "hermes-kanban-v2", "created_at": created, "updated_at": created,
            "idempotency_key": key, "origin_node_id": request.origin_node_id, "origin_profile": request.origin_profile,
            "origin_session_id": request.origin_session_id, "parent_fleet_task_id": str(request.parent_fleet_task_id) if request.parent_fleet_task_id else None,
            "objective": request.objective, "acceptance_criteria": request.acceptance_criteria,
            "requirements": payload["requirements"], "risk_class": request.risk_class, "protocol_version": "2.0",
            "payload": {"workspace": request.workspace, "goal": request.goal, "max_retries": request.max_retries, "model": request.model, "provider": request.provider, **request.payload},
            "route_explanation": explanation,
        })
        if inserted:
            await broker.publish(db.event("task.requested", correlation_id, {"task_id": task_id, "node_id": selected, "profile": request.profile}))
        return task

    @app.get("/api/v2/tasks")
    def tasks_v2(limit: int = 100, _: None = Depends(operator_auth)) -> dict:
        return {"protocol_version": "2.0", "tasks": db.list_tasks(min(max(limit, 1), 500))}

    @app.get("/api/v2/tasks/{task_id}")
    def task_v2(task_id: str, _: None = Depends(operator_auth)) -> dict:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return {"protocol_version": "2.0", "task": task, "normalized_status": task["status"]}

    @app.get("/api/v2/tasks/{task_id}/route")
    def task_route_v2(task_id: str, _: None = Depends(operator_auth)) -> dict:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return {"protocol_version": "2.0", "task_id": task_id, "route": task.get("route_explanation", {})}

    @app.get("/api/v2/nodes/{node_id}/assignments/next")
    async def next_assignment_v2(node_id: str, http_request: Request, wait: int = 20):
        node = await authenticate_node(http_request, node_id)
        deadline = time.monotonic() + min(max(wait, 0), 20)
        assignment = db.next_assignment(node_id)
        while assignment is None and time.monotonic() < deadline:
            await asyncio.sleep(min(0.25, deadline - time.monotonic()))
            assignment = db.next_assignment(node_id)
        if assignment:
            assignment = {
                "protocol_version": "2.0", "task_id": assignment["id"], "id": assignment["id"],
                "event_id": assignment["correlation_id"], "idempotency_key": assignment["idempotency_key"],
                "origin": {"node_id": assignment["origin_node_id"], "profile": assignment["origin_profile"], "session_id": assignment["origin_session_id"]},
                "destination": {"node_id": assignment["node_id"], "profile": assignment["profile"]},
                "destination_node_id": assignment["node_id"], "profile": assignment["profile"],
                "timestamp": assignment["created_at"], "sequence": 1, "objective": assignment["objective"],
                "acceptance_criteria": assignment["acceptance_criteria"], "requirements": assignment["requirements"],
                "risk_class": assignment["risk_class"], "parent_fleet_task_id": assignment["parent_fleet_task_id"],
                "payload": assignment["payload"],
            }
        payload = {"protocol_version": "2.0", "assignment": assignment}
        return signed_json(node, http_request.url.path, payload)

    @app.post("/api/v2/nodes/{node_id}/assignments/{task_id}/ack")
    async def assignment_ack_v2(node_id: str, task_id: str, ack: AssignmentAck, http_request: Request):
        node = await authenticate_node(http_request, node_id)
        task = db.get_task(task_id)
        if not task or task["node_id"] != node_id:
            raise HTTPException(404, "Assignment not found for node")
        if ack.idempotency_key != task["idempotency_key"]:
            raise HTTPException(409, "Assignment idempotency key does not match")
        if ack.disposition in {"accepted", "duplicate"} and not ack.remote_card_id:
            raise HTTPException(422, "Accepted assignments require a persisted remote card mapping")
        updated = db.acknowledge(task_id, node_id, {**ack.model_dump(mode="json"), "event_id": str(ack.event_id)})
        await broker.publish(db.event(f"assignment.{ack.disposition}", task["correlation_id"], {"task_id": task_id, "node_id": node_id, "reason_code": ack.reason_code}))
        return signed_json(node, http_request.url.path, {"protocol_version": "2.0", "task": updated})

    @app.post("/api/v2/nodes/{node_id}/events")
    async def node_event_v2(node_id: str, event: NodeLifecycleEvent, http_request: Request):
        node = await authenticate_node(http_request, node_id)
        task, disposition = db.apply_remote_event(node_id, {**event.model_dump(mode="json"), "event_id": str(event.event_id), "fleet_task_id": str(event.fleet_task_id)})
        if disposition == "not_found":
            raise HTTPException(404, "Fleet task not found for node")
        if disposition == "out_of_order":
            raise HTTPException(409, "Event sequence is not newer than the last applied event")
        if disposition == "idempotency_mismatch":
            raise HTTPException(409, "Event idempotency key does not match the fleet task")
        if disposition == "applied":
            await broker.publish(db.event(f"kanban.{event.event_type}", task["correlation_id"], {"task_id": str(event.fleet_task_id), "node_id": node_id, "sequence": event.sequence, "status": task["status"]}))
            if event.event_type in {"done", "blocked", "approval_required", "failed"}:
                await supervisor.wake_origin(task, event.event_type)
        return signed_json(node, http_request.url.path, {"protocol_version": "2.0", "disposition": disposition, "task": task})

    @app.post("/api/v2/tasks/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    async def cancel_v2(task_id: str, request: CancelRequest, _: None = Depends(operator_auth)) -> dict:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        stamp = now()
        with db.connect() as connection:
            connection.execute("UPDATE tasks SET cancel_requested_at=?,updated_at=? WHERE id=?", (stamp, stamp, task_id))
        await broker.publish(db.event("task.cancel_requested", task["correlation_id"], {"task_id": task_id, "reason": request.reason}))
        return {"protocol_version": "2.0", "task": db.get_task(task_id), "local_action_confirmed": False}

    @app.post("/api/v2/tasks/{task_id}/reconcile")
    async def reconcile_v2(task_id: str, request: ReconcileRequest, _: None = Depends(operator_auth)) -> dict:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        stamp = now()
        status_value = request.remote_status or task["status"]
        if status_value == "done":
            status_value = "complete" if request.evidence else "verifying"
        with db.connect() as connection:
            connection.execute("UPDATE tasks SET status=?,evidence=CASE WHEN ?='[]' THEN evidence ELSE ? END,last_remote_sequence=MAX(last_remote_sequence,?),reconciled_at=?,verified_at=CASE WHEN ?='complete' THEN ? ELSE verified_at END,updated_at=? WHERE id=?", (status_value, json.dumps(request.evidence), json.dumps(request.evidence), request.remote_sequence or 0, stamp, status_value, stamp, stamp, task_id))
        await broker.publish(db.event("task.reconciled", task["correlation_id"], {"task_id": task_id, "status": status_value}))
        return {"protocol_version": "2.0", "task": db.get_task(task_id)}

    @app.get("/api/v2/health")
    def health_v2(_: None = Depends(operator_auth)) -> dict:
        return {"protocol_version": "2.0", "status": "ok", "database": db.integrity_check(), "legacy_worker_mode": cfg.legacy_worker_mode}

    @app.get("/api/v2/benchmarks")
    def benchmarks_v2(_: None = Depends(operator_auth)) -> dict:
        with db.connect() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM benchmarks ORDER BY id DESC LIMIT 100")]
        return {"protocol_version": "2.0", "benchmarks": rows}

    @app.post("/api/v2/benchmarks", status_code=status.HTTP_201_CREATED)
    def record_benchmark_v2(sample: BenchmarkSample, _: None = Depends(operator_auth)) -> dict:
        if not sample.opt_in:
            raise HTTPException(409, "Benchmarks are opt-in; set opt_in=true after operator approval")
        if not db.get_node(sample.node_id):
            raise HTTPException(404, "Node not found")
        stamp = now()
        with db.connect() as connection:
            cursor = connection.execute("INSERT INTO benchmarks(node_id,name,value,unit,recorded_at,opt_in) VALUES(?,?,?,?,?,1)", (sample.node_id, sample.name, sample.value, sample.unit, stamp))
            old = connection.execute("SELECT id FROM benchmarks WHERE node_id=? AND name=? ORDER BY id DESC LIMIT -1 OFFSET 10", (sample.node_id, sample.name)).fetchall()
            if old:
                connection.executemany("DELETE FROM benchmarks WHERE id=?", [(row["id"],) for row in old])
            values = [row[0] for row in connection.execute("SELECT value FROM benchmarks WHERE node_id=? AND name=? ORDER BY id", (sample.node_id, sample.name))]
        ewma = 0.0
        for value in values:
            ewma = value if ewma == 0 else 0.3 * value + 0.7 * ewma
        return {"protocol_version": "2.0", "sample_id": cursor.lastrowid, "retained": len(values), "ewma": ewma, "dependencies_installed": False}

    @app.websocket("/api/v1/events/ws")
    async def event_stream(websocket: WebSocket) -> None:
        if not secrets.compare_digest(websocket.query_params.get("token", ""), cfg.operator_token):
            await websocket.close(code=1008, reason="Operator authentication required")
            return
        await broker.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            broker.disconnect(websocket)

    return app


app = create_app()
