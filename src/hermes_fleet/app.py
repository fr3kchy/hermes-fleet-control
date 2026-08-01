import hashlib
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, PlainTextResponse

from .config import Settings
from .auth import require_operator
from .db import Database, now
from .events import EventBroker
from .hermes_adapter import HermesCliAdapterV1
from .schemas import EnrollRequest, EnrollmentTokenRequest, HeartbeatRequest, TaskRequest
from .security import hash_secret, random_token, verify_signature


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    db = Database(cfg.database_path)
    broker = EventBroker()
    adapter = HermesCliAdapterV1(cfg.hermes_binary, cfg.task_timeout_seconds)
    operator_auth = require_operator(cfg.operator_token)

    app = FastAPI(title="Hermes Fleet Control", version="0.2.0")
    app.state.settings = cfg
    app.state.db = db

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
                connection.execute("INSERT INTO nodes(id,name,secret,status,capabilities,metrics,created_at) VALUES(?,?,?,?,?,?,?)", (node_id, request.node_name, node_secret, "enrolled", json.dumps(request.capabilities), "{}", created))
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
            seen = now()
            capabilities = {**json.loads(row["capabilities"]), **request.capabilities}
            connection.execute("UPDATE nodes SET status=?,metrics=?,capabilities=?,last_seen=? WHERE id=?", (request.status, json.dumps(request.metrics), json.dumps(capabilities), seen, node_id))
        correlation_id = str(uuid.uuid4())
        event = db.event("node.heartbeat", correlation_id, {"node_id": node_id, "status": request.status, "last_seen": seen})
        await broker.publish(event)
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
