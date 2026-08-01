from pydantic import BaseModel, Field, model_validator


class WorkflowStep(BaseModel):
    id: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    prompt: str = ""
    needs: list[str] = Field(default_factory=list)
    mutates_host: bool = False
    approval_required: bool = False

    @model_validator(mode="after")
    def mutations_require_approval(self):
        if self.mutates_host and not self.approval_required:
            raise ValueError(f"step {self.id}: host mutations require approval")
        return self


class WorkflowDefinition(BaseModel):
    name: str = Field(min_length=1)
    steps: list[WorkflowStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self):
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step id")
        known = set(ids)
        for step in self.steps:
            missing = set(step.needs) - known
            if missing:
                raise ValueError(f"step {step.id}: unknown dependencies {sorted(missing)}")
        self.execution_order()
        return self

    def execution_order(self) -> list[WorkflowStep]:
        remaining = {step.id: step for step in self.steps}
        complete: set[str] = set()
        ordered: list[WorkflowStep] = []
        while remaining:
            ready = [step for step in self.steps if step.id in remaining and set(step.needs) <= complete]
            if not ready:
                raise ValueError("workflow dependency cycle")
            for step in ready:
                ordered.append(step)
                complete.add(step.id)
                remaining.pop(step.id)
        return ordered
