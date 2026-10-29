"""Default policies from the Owner's config file (spec §13, §18; ADR-0007).

Loaded once at startup from ``OFFICE_POLICY_FILE`` (default ``./office-policy.toml``);
restart the server after editing it. A missing file means the built-in defaults
below, which match the behaviour before the file existed. Unknown keys or bad
values stop the server at startup instead of being ignored.
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["owner", "observer"]
InterventionAction = Literal["pause", "resume", "cancel", "redirect"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TranscriptPolicy(_Section):
    # Roles that may read private (participants-only) DM transcripts; [] = nobody.
    private_readers: list[Role] = ["owner"]


class RetentionPolicy(_Section):
    # 0 = keep forever.
    content_days: int = Field(default=0, ge=0)
    workspace_days: int = Field(default=0, ge=0)
    sweep_interval_h: float = Field(default=6, gt=0)
    backups_keep: int = Field(default=14, ge=1)

    @property
    def enabled(self) -> bool:
        return bool(self.content_days or self.workspace_days)


class BudgetPolicy(_Section):
    # Applied to projects created without an explicit budget; None = unlimited.
    default_max_cost_usd: float | None = Field(default=None, ge=0)


class InterventionPolicy(_Section):
    # Owners can always intervene; these are the extra actions observers may request.
    observer_actions: list[InterventionAction] = []


class ApprovalPolicy(_Section):
    # A project can still override it with config["approval_ttl_s"].
    ttl_s: float = Field(default=3600, gt=0)


class Policy(_Section):
    transcripts: TranscriptPolicy = Field(default_factory=TranscriptPolicy)
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    interventions: InterventionPolicy = Field(default_factory=InterventionPolicy)
    approvals: ApprovalPolicy = Field(default_factory=ApprovalPolicy)

    def can_read_private(self, role: str) -> bool:
        return role in self.transcripts.private_readers

    def can_intervene(self, role: str, action: str) -> bool:
        return role == "owner" or action in self.interventions.observer_actions


def load_policy(path: str | None) -> Policy:
    """Empty path or a missing file → defaults."""
    if not path or not Path(path).is_file():
        return Policy()
    try:
        with open(path, "rb") as f:
            return Policy.model_validate(tomllib.load(f))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise SystemExit(f"invalid policy file {path}: {exc}") from None
