"""Typed GitHub Check Run transport models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GitHubCheckStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class GitHubCheckConclusion(StrEnum):
    ACTION_REQUIRED = "action_required"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    SUCCESS = "success"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class GitHubCheckOutput:
    title: str
    summary: str


@dataclass(frozen=True, slots=True)
class GitHubCheckRun:
    id: int
    name: str
    head_sha: str
    external_id: str
    status: GitHubCheckStatus
    conclusion: GitHubCheckConclusion | None


@dataclass(frozen=True, slots=True)
class GitHubCheckRunInput:
    name: str
    head_sha: str
    external_id: str
    status: GitHubCheckStatus
    output: GitHubCheckOutput
    conclusion: GitHubCheckConclusion | None = None
    details_url: str | None = None
