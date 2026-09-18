import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


CLASSIFICATION = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


@dataclass(frozen=True)
class RouteCandidate:
    node_id: str
    accepted: bool
    score: float
    reasons: tuple[str, ...]
    components: dict[str, float]


def _set(value: Any) -> set[str]:
    return {str(item).lower() for item in (value or [])}


def _age_seconds(timestamp: str | None) -> float:
    if not timestamp:
        return math.inf
    try:
        return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds())
    except ValueError:
        return math.inf


def evaluate_node(node: dict, requirements: dict, profile: str, risk_class: int, active_node: int = 0, active_profile: int = 0) -> RouteCandidate:
    caps, policy, metrics = node.get("capabilities") or {}, node.get("policy") or {}, node.get("metrics") or {}
    rejected: list[str] = []
    if node.get("status") not in {"online", "degraded"} or _age_seconds(node.get("last_seen")) > 90:
        rejected.append("NODE_STALE_OR_OFFLINE")
    if requirements.get("os") and str(caps.get("os", "")).lower() != requirements["os"].lower():
        rejected.append("OS_MISMATCH")
    if requirements.get("architecture") and str(caps.get("architecture", "")).lower() != requirements["architecture"].lower():
        rejected.append("ARCH_MISMATCH")
    for field, code in (("tools", "MISSING_TOOL"), ("skills", "MISSING_SKILL"), ("mcp", "MISSING_MCP")):
        if not _set(requirements.get(field)).issubset(_set(caps.get(field))):
            rejected.append(code)
    for field, code in (("browser", "BROWSER_UNAVAILABLE"), ("gui", "GUI_UNAVAILABLE"), ("gpu", "GPU_UNAVAILABLE")):
        if requirements.get(field) and not caps.get(field):
            rejected.append(code)
    if float(caps.get("vram_gb") or 0) < float(requirements.get("min_vram_gb") or 0):
        rejected.append("INSUFFICIENT_VRAM")
    if profile not in caps.get("profiles", []):
        rejected.append("PROFILE_UNAVAILABLE")
    repository = requirements.get("repository")
    if repository and repository not in (caps.get("repositories") or []):
        rejected.append("REPOSITORY_NOT_LOCAL")
    domain = requirements.get("credential_domain")
    if domain and domain not in (policy.get("credential_domains") or []):
        rejected.append("CREDENTIAL_DOMAIN_DENIED")
    requested_class = CLASSIFICATION.get(requirements.get("data_classification", "internal"), 99)
    allowed_class = CLASSIFICATION.get(policy.get("data_classification", "internal"), -1)
    if requested_class > allowed_class:
        rejected.append("DATA_CLASSIFICATION_DENIED")
    if risk_class > int(policy.get("max_risk", 1)):
        rejected.append("RISK_CEILING_EXCEEDED")
    if active_node >= int(policy.get("max_concurrency", 2)):
        rejected.append("NODE_CONCURRENCY_EXHAUSTED")
    if active_profile >= int(policy.get("profile_concurrency", {}).get(profile, 1)):
        rejected.append("PROFILE_CONCURRENCY_EXHAUSTED")
    if requirements.get("gpu") and policy.get("exclusive_gpu", True) and active_node:
        rejected.append("GPU_ALLOCATION_BUSY")
    if rejected:
        return RouteCandidate(node["id"], False, 0, tuple(sorted(set(rejected))), {})

    requested = sum(len(requirements.get(k) or []) for k in ("tools", "skills", "mcp")) + sum(bool(requirements.get(k)) for k in ("browser", "gui", "gpu"))
    capability = 25.0 if requested == 0 else min(25.0, 10 + requested * 3)
    availability = max(0.0, 20.0 - active_node * 8 - float(metrics.get("load_1m") or 0))
    trust = 20.0 if policy.get("trust_zone") in {"trusted", "owner"} else 12.0
    locality = 15.0 if repository or domain else 8.0
    performance = min(10.0, float(metrics.get("benchmark_score") or 5))
    success = 5.0 * float(metrics.get("success_rate") or 1)
    energy = min(5.0, float(metrics.get("energy_score") or 3))
    network_penalty = min(10.0, float(metrics.get("network_penalty") or 0))
    risk_penalty = max(0.0, risk_class - 1) * 2
    components = {"capability": capability, "availability": availability, "trust": trust, "locality": locality, "performance": performance, "historical_success": success, "cost_energy": energy, "penalties": -(network_penalty + risk_penalty)}
    return RouteCandidate(node["id"], True, round(sum(components.values()), 3), (), components)


def route(nodes: list[dict], requirements: dict, profile: str, risk_class: int, active: dict[tuple[str, str], int] | None = None) -> tuple[str | None, dict]:
    active = active or {}
    candidates = [evaluate_node(node, requirements, profile, risk_class, sum(count for (node_id, _), count in active.items() if node_id == node["id"]), active.get((node["id"], profile), 0)) for node in nodes]
    candidates.sort(key=lambda item: (-int(item.accepted), -item.score, item.node_id))
    selected = next((candidate.node_id for candidate in candidates if candidate.accepted), None)
    return selected, {"selected_node_id": selected, "candidates": [{"node_id": c.node_id, "accepted": c.accepted, "score": c.score, "reason_codes": list(c.reasons), "components": c.components} for c in candidates]}
