from pathlib import Path

from hermes_fleet.kanban_adapter import AssignmentMap, HermesKanbanAdapter


def test_assignment_mapping_is_durable_and_sequence_starts_after_ack(tmp_path: Path):
    mapping = AssignmentMap(tmp_path / "map.db")
    first = mapping.put("fleet-1", "global-1", "board", "card")
    duplicate = mapping.put("fleet-1", "global-1", "other", "other")
    assert duplicate["card_id"] == first["card_id"] == "card"
    mapping.ensure_sequence("fleet-1", 1)
    assert mapping.advance("fleet-1") == 2
    mapping.set_status("fleet-1", "running")
    assert mapping.get("fleet-1")["last_status"] == "running"


def test_kanban_command_uses_argv_without_shell(monkeypatch):
    captured = {}
    class Result:
        returncode = 0
        stdout = '{"id":"t_1"}'
        stderr = ""
    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Result()
    monkeypatch.setattr("subprocess.run", run)
    HermesKanbanAdapter("hermes").create({"id": "fleet-1", "objective": "title; touch /tmp/not-run", "acceptance_criteria": ["safe"], "profile": "engineer", "payload": {}, "risk_class": 1})
    assert captured["kwargs"]["check"] is False
    assert captured["command"][0:3] == ["hermes", "kanban", "create"]
    assert "title; touch /tmp/not-run" in captured["command"]
