import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

import httpx

from .security import sign_payload


def _probe(command: list[str], timeout: int = 3) -> str:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        return (result.stdout or result.stderr).strip()[:4000] if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _profiles() -> list[str]:
    output = _probe(["hermes", "profile", "list", "--json"])
    if output:
        try:
            data = json.loads(output)
            records = data.get("profiles", data) if isinstance(data, dict) else data
            return sorted({"default", *{str(item.get("id") or item.get("name")) for item in records if isinstance(item, dict) and (item.get("id") or item.get("name"))}})
        except (json.JSONDecodeError, TypeError):
            pass
    profile_root = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / "profiles"
    return sorted({"default", *(path.name for path in profile_root.iterdir() if path.is_dir())}) if profile_root.is_dir() else ["default"]


def _gpu() -> dict:
    output = _probe(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    if not output:
        return {"gpu": False, "gpu_devices": [], "vram_gb": 0}
    devices, total = [], 0.0
    for line in output.splitlines():
        try:
            name, memory = line.rsplit(",", 1)
            devices.append({"name": name.strip(), "vram_mb": int(memory.strip())})
            total += int(memory.strip()) / 1024
        except ValueError:
            continue
    return {"gpu": bool(devices), "gpu_devices": devices, "vram_gb": round(total, 2)}


def capability_manifest() -> dict:
    hermes_version = _probe(["hermes", "--version"]).splitlines()
    tools = [name for name in ("git", "docker", "node", "python3", "ssh", "rg") if shutil.which(name)]
    home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
    kanban_help = _probe(["hermes", "kanban", "--help"])
    fr3k_binary = shutil.which("hey-fr3k")
    fr3k_repositories = [str(path) for path in (Path.home() / "repos", Path.home() / "src") if path.is_dir() and any(child.is_dir() and "fr3k" in child.name.lower() for child in path.iterdir())]
    return {
        "os": platform.system().lower(), "hostname": platform.node(), "architecture": platform.machine(),
        "cpu_count": os.cpu_count(), "python_version": platform.python_version(), "hermes_version": hermes_version[0] if hermes_version else "",
        "profiles": _profiles(), "kanban": bool(kanban_help), "browser": bool((home / "browser").exists() or shutil.which("google-chrome") or shutil.which("chromium")),
        "gui": bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY") or platform.system() in {"Darwin", "Windows"}),
        "tools": tools, "terminal_backends": ["local"] + (["docker"] if "docker" in tools else []) + (["ssh"] if "ssh" in tools else []),
        "skills": sorted(path.name for path in (home / "skills").glob("*") if path.is_dir()) if (home / "skills").is_dir() else [],
        "mcp": [], "local_inference": bool(shutil.which("ollama") or shutil.which("llama-server")),
        "fr3k": {"available": bool(fr3k_binary), "stdio_mcp": bool(fr3k_binary), "repository_roots": fr3k_repositories, "enabled": False},
        "connector_version": "2.0.0", "protocol_versions": ["1.0", "2.0"], **_gpu(),
    }


def enroll_and_heartbeat(base_url: str, token: str, node_name: str, state_path: Path) -> dict:
    with httpx.Client(base_url=base_url, timeout=20) as client:
        enrolled = client.post("/api/v1/nodes/enroll", json={"token": token, "node_name": node_name, "capabilities": capability_manifest()})
        enrolled.raise_for_status()
        credentials = enrolled.json()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(credentials))
        state_path.chmod(0o600)
        body = {"status": "online", "metrics": {}, "capabilities": capability_manifest()}
        heartbeat = client.post(f"/api/v1/nodes/{credentials['node_id']}/heartbeat", json=body, headers={"X-Node-Signature": sign_payload(credentials["node_secret"], body)})
        heartbeat.raise_for_status()
        return {"enrollment": credentials, "heartbeat": heartbeat.json()}
