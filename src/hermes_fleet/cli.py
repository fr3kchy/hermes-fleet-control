import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import yaml


ROOT = Path(__file__).resolve().parents[2]
ROLE_PROFILES = {
    "coordinator": ("chief-of-staff", "fleet-dispatcher", "reviewer"),
    "worker": ("researcher", "engineer", "reviewer"),
    "operator": ("operator",),
}


def _merge(base: dict, owned: dict) -> dict:
    result = dict(base)
    for key, value in owned.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        elif isinstance(value, list) and isinstance(result.get(key), list):
            result[key] = list(dict.fromkeys([*result[key], *value]))
        else:
            result[key] = value
    return result


def _profile_exists(hermes_home: Path, name: str) -> bool:
    return (hermes_home / "profiles" / name).is_dir()


def bootstrap(role: str, apply: bool = False, replace_owned: bool = False, hermes_home: Path | None = None) -> dict:
    hermes_home = hermes_home or Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
    actions: list[dict] = []
    profiles = ROLE_PROFILES[role]
    plugin_target = hermes_home / "plugins" / "fleet-control"
    skill_target = hermes_home / "skills" / "autonomous-ai-agents" / "fleet-supervisor"
    for name in profiles:
        profile_dir = hermes_home / "profiles" / name
        if not _profile_exists(hermes_home, name):
            actions.append({"action": "create_profile", "profile": name, "command": ["hermes", "profile", "create", name, "--no-skills", "--no-alias"]})
        actions.append({"action": "merge_owned_config", "profile": name, "source": str(ROOT / "agent-package" / "profiles" / f"{name}.yaml"), "target": str(profile_dir / "config.yaml")})
        soul = ROOT / "agent-package" / name / "SOUL.md"
        if soul.exists():
            actions.append({"action": "install_owned_soul", "profile": name, "source": str(soul), "target": str(profile_dir / "SOUL.md")})
    actions.extend((
        {"action": "install_plugin", "source": str(ROOT / "hermes-plugin" / "fleet-control"), "target": str(plugin_target)},
        {"action": "install_skill", "source": str(ROOT / "agent-package" / "fleet-supervisor"), "target": str(skill_target)},
        {"action": "validate", "checks": ["hermes --version", "hermes kanban --help", "hermes plugins doctor", "profile toolsets", "fleet health"]},
    ))
    result = {"role": role, "apply": apply, "hermes_home": str(hermes_home), "actions": actions, "warnings": ["No credentials, .env files, browser state, node enrollment, or service state will be copied or changed."]}
    if not apply:
        return result
    hermes_home.mkdir(parents=True, exist_ok=True)
    subprocess_env = {**os.environ, "HERMES_HOME": str(hermes_home)}
    created_profiles: set[str] = set()
    for name in profiles:
        if not _profile_exists(hermes_home, name):
            subprocess.run(["hermes", "profile", "create", name, "--no-skills", "--no-alias"], check=True, env=subprocess_env)
            created_profiles.add(name)
        profile_dir = hermes_home / "profiles" / name
        config_path = profile_dir / "config.yaml"
        base = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
        owned = yaml.safe_load((ROOT / "agent-package" / "profiles" / f"{name}.yaml").read_text())
        merged = _merge(base or {}, owned or {})
        rendered = yaml.safe_dump(merged, sort_keys=False)
        if config_path.exists() and config_path.read_text() != rendered:
            shutil.copy2(config_path, config_path.with_suffix(f".yaml.bak.{int(time.time())}"))
        config_path.write_text(rendered)
        config_path.chmod(0o600)
        soul_source = ROOT / "agent-package" / name / "SOUL.md"
        soul_target = profile_dir / "SOUL.md"
        if soul_source.exists():
            if soul_target.exists() and soul_target.read_bytes() != soul_source.read_bytes() and name not in created_profiles:
                if not replace_owned:
                    raise RuntimeError(f"Refusing to replace existing {soul_target}; use --replace-owned")
                shutil.copy2(soul_target, soul_target.with_suffix(f".md.bak.{int(time.time())}"))
            shutil.copy2(soul_source, soul_target)
    for source, target in ((ROOT / "hermes-plugin" / "fleet-control", plugin_target), (ROOT / "agent-package" / "fleet-supervisor", skill_target)):
        if target.exists():
            source_files = [item for item in source.rglob("*") if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc"]
            same = all((target / item.relative_to(source)).exists() and (target / item.relative_to(source)).read_bytes() == item.read_bytes() for item in source_files)
            if not same and not replace_owned:
                raise RuntimeError(f"Refusing to replace existing {target}; use --replace-owned")
            if not same:
                backup = target.with_name(f"{target.name}.bak.{int(time.time())}")
                shutil.copytree(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in profiles:
        if name not in {"chief-of-staff", "fleet-dispatcher"}:
            continue
        source = ROOT / "agent-package" / "fleet-supervisor"
        target = hermes_home / "profiles" / name / "skills" / "fleet-supervisor"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    result["validation"] = validate(hermes_home)
    return result


def validate(hermes_home: Path | None = None) -> dict:
    hermes_home = hermes_home or Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
    probes = {}
    subprocess_env = {**os.environ, "HERMES_HOME": str(hermes_home)}
    for name, command in (("hermes", ["hermes", "--version"]), ("kanban", ["hermes", "kanban", "--help"]), ("plugin", ["hermes", "plugins", "doctor", "--ci", str(hermes_home / "plugins" / "fleet-control")])):
        result = subprocess.run(command, text=True, capture_output=True, check=False, env=subprocess_env)
        probes[name] = {"ok": result.returncode == 0, "detail": (result.stdout or result.stderr)[-2000:]}
    base, token = os.getenv("HFC_BASE_URL", "http://127.0.0.1:8876"), os.getenv("HFC_OPERATOR_TOKEN", "")
    if token:
        try:
            response = httpx.get(base.rstrip("/") + "/api/v2/health", headers={"Authorization": f"Bearer {token}"}, timeout=3)
            probes["fleet_service"] = {"ok": response.status_code == 200, "detail": response.text[:1000]}
        except httpx.HTTPError as exc:
            probes["fleet_service"] = {"ok": False, "detail": str(exc)}
    else:
        probes["fleet_service"] = {"ok": False, "detail": "HFC_OPERATOR_TOKEN not set"}
    toolset_errors = []
    for config_path in sorted((hermes_home / "profiles").glob("*/config.yaml")):
        config = yaml.safe_load(config_path.read_text()) or {}
        cli_toolsets = ((config.get("platform_toolsets") or {}).get("cli") or [])
        if not isinstance(cli_toolsets, list) or not all(isinstance(item, str) for item in cli_toolsets):
            toolset_errors.append(f"{config_path.parent.name}: platform_toolsets.cli must be a string list")
        if "fleet" in cli_toolsets and config_path.parent.name not in {"chief-of-staff", "fleet-dispatcher"}:
            toolset_errors.append(f"{config_path.parent.name}: fleet model tool is restricted to Chief/dispatcher")
    probes["profile_toolsets"] = {"ok": not toolset_errors, "detail": "; ".join(toolset_errors) or "profile toolset boundaries valid"}
    return {"ok": all(value["ok"] for value in probes.values()), "probes": probes}


def main() -> None:
    parser = argparse.ArgumentParser(prog="hermes-fleet")
    commands = parser.add_subparsers(dest="command", required=True)
    boot = commands.add_parser("bootstrap")
    boot.add_argument("--role", choices=ROLE_PROFILES, required=True)
    boot.add_argument("--apply", action="store_true")
    boot.add_argument("--replace-owned", action="store_true")
    check = commands.add_parser("validate")
    check.add_argument("--hermes-home", type=Path)
    args = parser.parse_args()
    output = bootstrap(args.role, args.apply, args.replace_owned) if args.command == "bootstrap" else validate(args.hermes_home)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
