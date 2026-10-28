"""Governance contract: high-risk actions that need Owner approval (BR-07, spec §13)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RiskAction(StrEnum):
    DEPLOY_PRODUCTION = "deploy_production"
    PAYMENT = "payment"
    DELETE_DATA = "delete_data"
    EXPORT_SENSITIVE_DATA = "export_sensitive_data"


class ApprovalScope(BaseModel):
    """What one approval authorizes: one run of one task, by one agent, with
    exactly these instructions. Any change needs a new approval."""

    model_config = ConfigDict(extra="forbid", title="ApprovalScope")

    task_id: str
    actions: list[RiskAction] = Field(min_length=1)
    agent_id: str = Field(description="Worker that will perform the actions.")
    instructions_sha256: str = Field(description="Hash of the task instructions approved.")
    summary: str = Field(description="Human-readable description shown to the approver.")
