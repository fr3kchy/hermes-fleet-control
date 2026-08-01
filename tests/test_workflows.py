import pytest
import yaml
from pathlib import Path
from hermes_fleet.workflows import WorkflowDefinition


def test_workflow_orders_dependencies_and_requires_approval_for_mutation():
    workflow = WorkflowDefinition.model_validate({"name": "diagnostics", "steps": [
        {"id": "inspect", "profile": "default"},
        {"id": "repair", "profile": "default", "needs": ["inspect"], "mutates_host": True, "approval_required": True},
    ]})
    assert [step.id for step in workflow.execution_order()] == ["inspect", "repair"]


def test_workflow_rejects_cycles():
    with pytest.raises(ValueError, match="cycle"):
        WorkflowDefinition.model_validate({"name": "bad", "steps": [
            {"id": "a", "profile": "default", "needs": ["b"]},
            {"id": "b", "profile": "default", "needs": ["a"]},
        ]})


def test_shipped_templates_are_valid():
    root = Path(__file__).parents[1] / "workflows"
    files = sorted(root.glob("*.yaml"))
    assert len(files) == 5
    for path in files:
        WorkflowDefinition.model_validate(yaml.safe_load(path.read_text()))
