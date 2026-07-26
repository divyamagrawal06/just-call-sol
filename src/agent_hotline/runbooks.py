"""Typed, allowlisted operational runbooks.

The default registry contains deterministic demo actions only.  A voice model may
select a registered runbook and parameters, but it cannot provide a shell command or
replace the executor.  Real executors are additionally blocked unless the registry
is constructed with an explicit real-execution gate.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_hotline.security import action_hash

_RUNBOOK_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")


class RunbookError(RuntimeError):
    """Base class for runbook lookup, validation, or execution failures."""


class RunbookNotFoundError(RunbookError):
    """The requested ID is not present in the registry allowlist."""


class RunbookParameterError(RunbookError):
    """Parameters do not match the registered strict input schema."""

    def __init__(self, runbook_id: str, issues: tuple[Mapping[str, Any], ...]) -> None:
        super().__init__(f"parameters are invalid for registered runbook {runbook_id}")
        self.runbook_id = runbook_id
        self.issues = issues


class RunbookConfirmationRequiredError(RunbookError):
    """The exact previewed action hash was not supplied at execution."""


class RealExecutionDisabledError(RunbookError):
    """A real executor was reached while the real-action gate is disabled."""


class RunbookVerificationError(RunbookError):
    """A runbook executor returned a result that failed its verifier."""


class RunbookRisk(StrEnum):
    """Risk tier used by the deterministic authorization policy."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConfirmationRequirement(StrEnum):
    """Identity and confirmation needed before an action may execute."""

    ALLOWLIST = "caller_allowlist"
    SPOKEN = "explicit_spoken_confirmation"
    READBACK_AND_PIN = "exact_readback_plus_pin"


class ExecutionMode(StrEnum):
    """Whether an executor is a side-effect-free mock or touches a real system."""

    MOCK = "mock"
    REAL = "real"


class StrictRunbookParameters(BaseModel):
    """Base model that rejects coercion and every unregistered input field."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class IncreaseDbRuParameters(StrictRunbookParameters):
    """Parameters for the bounded mock database RU increase."""

    environment: Literal["demo"] = "demo"
    database: Literal["demo-orders"] = "demo-orders"
    current_ru: int = Field(default=400, ge=100, le=10_000)
    target_ru: int = Field(ge=200, le=10_000)

    @model_validator(mode="after")
    def target_must_increase(self) -> IncreaseDbRuParameters:
        if self.target_ru <= self.current_ru:
            raise ValueError("target_ru must be greater than current_ru")
        return self


class PauseDeploymentParameters(StrictRunbookParameters):
    """Parameters for pausing the single allowlisted mock deployment."""

    environment: Literal["demo"] = "demo"
    deployment: Literal["demo-api"] = "demo-api"
    reason: Literal["owner-request", "incident-response", "demo"] = "owner-request"


class TerminateBatchRunsParameters(StrictRunbookParameters):
    """Parameters for terminating active jobs in the mock training batch."""

    environment: Literal["demo"] = "demo"
    batch_group: Literal["demo-training"] = "demo-training"
    selection: Literal["all-active"] = "all-active"


class RunbookPlan(BaseModel):
    """Internal deterministic preview returned by a registered preview function."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    impact: str
    expected_changes: dict[str, Any] = Field(default_factory=dict)


class ExecutorResult(BaseModel):
    """Internal structured output returned by an executor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    changed: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    """Result of deterministic post-execution verification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verified: bool
    message: str


class RunbookSummary(BaseModel):
    """Safe metadata exposed when listing allowed actions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runbook_id: str
    revision: int
    description: str
    risk: RunbookRisk
    confirmation: ConfirmationRequirement
    execution_mode: ExecutionMode
    allowed_environments: tuple[str, ...]
    allowed_resources: tuple[str, ...]
    maximum_execution_seconds: int


class RunbookPreview(BaseModel):
    """Exact action presented to the human before confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runbook_id: str
    revision: int
    description: str
    normalized_parameters: dict[str, Any]
    impact: str
    expected_changes: dict[str, Any]
    risk: RunbookRisk
    confirmation: ConfirmationRequirement
    execution_mode: ExecutionMode
    rollback_guidance: str
    maximum_execution_seconds: int
    action_hash: str


class RunbookExecution(BaseModel):
    """Auditable deterministic result from one registered execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runbook_id: str
    action_hash: str
    operation_id: str
    status: Literal["mock_succeeded", "succeeded"]
    changed: bool
    message: str
    details: dict[str, Any]
    verified: bool
    verification_message: str


PreviewFunction = Callable[[StrictRunbookParameters], RunbookPlan]
ExecutorFunction = Callable[[StrictRunbookParameters], ExecutorResult]
VerifierFunction = Callable[
    [StrictRunbookParameters, ExecutorResult],
    VerificationResult,
]


@dataclass(frozen=True, slots=True)
class RunbookDefinition:
    """Administrator-registered typed action definition."""

    runbook_id: str
    description: str
    parameter_model: type[StrictRunbookParameters]
    previewer: PreviewFunction
    executor: ExecutorFunction
    verifier: VerifierFunction
    risk: RunbookRisk
    confirmation: ConfirmationRequirement
    execution_mode: ExecutionMode
    allowed_environments: tuple[str, ...]
    allowed_resources: tuple[str, ...]
    rollback_guidance: str
    revision: int = 1
    maximum_execution_seconds: int = 30

    def __post_init__(self) -> None:
        if not _RUNBOOK_ID_RE.fullmatch(self.runbook_id):
            raise ValueError("runbook_id must be a dotted lowercase identifier")
        if not self.description.strip():
            raise ValueError("description cannot be empty")
        if not issubclass(self.parameter_model, StrictRunbookParameters):
            raise TypeError("parameter_model must inherit StrictRunbookParameters")
        if not self.allowed_environments or not self.allowed_resources:
            raise ValueError("allowed environments and resources cannot be empty")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.maximum_execution_seconds < 1 or self.maximum_execution_seconds > 300:
            raise ValueError("maximum_execution_seconds must be between 1 and 300")

    def summary(self) -> RunbookSummary:
        return RunbookSummary(
            runbook_id=self.runbook_id,
            revision=self.revision,
            description=self.description,
            risk=self.risk,
            confirmation=self.confirmation,
            execution_mode=self.execution_mode,
            allowed_environments=self.allowed_environments,
            allowed_resources=self.allowed_resources,
            maximum_execution_seconds=self.maximum_execution_seconds,
        )


class RunbookRegistry:
    """Immutable-at-use registry of strict runbook definitions."""

    def __init__(
        self,
        definitions: tuple[RunbookDefinition, ...] = (),
        *,
        allow_real_execution: bool = False,
    ) -> None:
        registered: dict[str, RunbookDefinition] = {}
        for definition in definitions:
            if definition.runbook_id in registered:
                raise ValueError(f"duplicate runbook id: {definition.runbook_id}")
            registered[definition.runbook_id] = definition
        self._definitions = MappingProxyType(registered)
        self._allow_real_execution = allow_real_execution

    @property
    def allow_real_execution(self) -> bool:
        """Whether this registry was explicitly constructed to permit real actions."""

        return self._allow_real_execution

    def list(self) -> tuple[RunbookSummary, ...]:
        """List only safe metadata for the registered allowlist."""

        return tuple(definition.summary() for _, definition in sorted(self._definitions.items()))

    def get(self, runbook_id: str) -> RunbookDefinition:
        """Resolve an exact registered ID; partial IDs and commands are never accepted."""

        try:
            return self._definitions[runbook_id]
        except (KeyError, TypeError) as exc:
            raise RunbookNotFoundError(f"unknown runbook: {runbook_id!r}") from exc

    def validate(
        self,
        runbook_id: str,
        parameters: Mapping[str, Any],
    ) -> StrictRunbookParameters:
        """Strictly validate and normalize parameters for a registered runbook."""

        definition = self.get(runbook_id)
        if not isinstance(parameters, Mapping):
            raise RunbookParameterError(
                runbook_id,
                ({"location": (), "message": "parameters must be an object", "type": "mapping"},),
            )
        try:
            return definition.parameter_model.model_validate(dict(parameters), strict=True)
        except ValidationError as exc:
            # Do not include rejected input values; they can contain credentials or
            # prompt-injection text supplied by a public voice tool.
            issues = tuple(
                {
                    "location": tuple(str(part) for part in issue["loc"]),
                    "message": issue["msg"],
                    "type": issue["type"],
                }
                for issue in exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            )
            raise RunbookParameterError(runbook_id, issues) from exc

    validate_parameters = validate

    def preview(
        self,
        runbook_id: str,
        parameters: Mapping[str, Any],
    ) -> RunbookPreview:
        """Validate and produce the exact deterministic action to read back."""

        definition = self.get(runbook_id)
        validated = self.validate(runbook_id, parameters)
        normalized = validated.model_dump(mode="json")
        plan = definition.previewer(validated)
        digest = _runbook_action_hash(definition, normalized)
        return RunbookPreview(
            runbook_id=definition.runbook_id,
            revision=definition.revision,
            description=definition.description,
            normalized_parameters=normalized,
            impact=plan.impact,
            expected_changes=plan.expected_changes,
            risk=definition.risk,
            confirmation=definition.confirmation,
            execution_mode=definition.execution_mode,
            rollback_guidance=definition.rollback_guidance,
            maximum_execution_seconds=definition.maximum_execution_seconds,
            action_hash=digest,
        )

    def execute(
        self,
        runbook_id: str,
        parameters: Mapping[str, Any],
        *,
        confirmed_action_hash: str,
    ) -> RunbookExecution:
        """Execute only the exact previewed, confirmed registered action."""

        definition = self.get(runbook_id)
        validated = self.validate(runbook_id, parameters)
        normalized = validated.model_dump(mode="json")
        digest = _runbook_action_hash(definition, normalized)
        if not isinstance(confirmed_action_hash, str) or not hmac.compare_digest(
            digest.encode(),
            confirmed_action_hash.encode(),
        ):
            raise RunbookConfirmationRequiredError(
                "confirmed action hash does not match the current runbook and parameters"
            )
        if definition.execution_mode is ExecutionMode.REAL and not self.allow_real_execution:
            raise RealExecutionDisabledError(
                "real runbook execution is disabled; use a mock or enable the explicit gate"
            )
        executor_result = definition.executor(validated)
        verification = definition.verifier(validated, executor_result)
        if not verification.verified:
            raise RunbookVerificationError(verification.message)
        mode_status: Literal["mock_succeeded", "succeeded"] = (
            "mock_succeeded" if definition.execution_mode is ExecutionMode.MOCK else "succeeded"
        )
        return RunbookExecution(
            runbook_id=definition.runbook_id,
            action_hash=digest,
            operation_id=f"runbook-{digest[:16]}",
            status=mode_status,
            changed=executor_result.changed,
            message=executor_result.message,
            details=executor_result.details,
            verified=True,
            verification_message=verification.message,
        )


def _runbook_action_hash(
    definition: RunbookDefinition,
    normalized_parameters: Mapping[str, Any],
) -> str:
    return action_hash(
        "runbook.execute",
        {
            "parameters": normalized_parameters,
            "runbook_id": definition.runbook_id,
        },
        bindings={
            "allowed_environments": definition.allowed_environments,
            "allowed_resources": definition.allowed_resources,
            "confirmation": definition.confirmation.value,
            "execution_mode": definition.execution_mode.value,
            "risk": definition.risk.value,
            "runbook_revision": definition.revision,
        },
    )


def _preview_increase_db_ru(parameters: IncreaseDbRuParameters) -> RunbookPlan:
    return RunbookPlan(
        impact=(
            f"Mock increase {parameters.database} capacity from {parameters.current_ru} "
            f"to {parameters.target_ru} RU in the demo environment."
        ),
        expected_changes={
            "database": parameters.database,
            "from_ru": parameters.current_ru,
            "to_ru": parameters.target_ru,
            "monthly_cost_change": "none (mock)",
        },
    )


def _execute_increase_db_ru(parameters: IncreaseDbRuParameters) -> ExecutorResult:
    return ExecutorResult(
        changed=True,
        message=(f"Mock database {parameters.database} now reports {parameters.target_ru} RU."),
        details={
            "database": parameters.database,
            "environment": parameters.environment,
            "mock": True,
            "reported_ru": parameters.target_ru,
        },
    )


def _verify_increase_db_ru(
    parameters: IncreaseDbRuParameters,
    result: ExecutorResult,
) -> VerificationResult:
    verified = (
        result.details.get("mock") is True
        and result.details.get("database") == parameters.database
        and result.details.get("reported_ru") == parameters.target_ru
    )
    return VerificationResult(
        verified=verified,
        message=(
            "Mock database capacity matches the requested RU target."
            if verified
            else "Mock database capacity did not match the requested RU target."
        ),
    )


def _preview_pause_deployment(parameters: PauseDeploymentParameters) -> RunbookPlan:
    return RunbookPlan(
        impact=(
            f"Mock pause traffic for {parameters.deployment} in the demo environment "
            f"for reason {parameters.reason}."
        ),
        expected_changes={
            "deployment": parameters.deployment,
            "desired_traffic_state": "paused",
            "environment": parameters.environment,
        },
    )


def _execute_pause_deployment(parameters: PauseDeploymentParameters) -> ExecutorResult:
    return ExecutorResult(
        changed=True,
        message=f"Mock deployment {parameters.deployment} is paused.",
        details={
            "deployment": parameters.deployment,
            "environment": parameters.environment,
            "mock": True,
            "reason": parameters.reason,
            "traffic_state": "paused",
        },
    )


def _verify_pause_deployment(
    parameters: PauseDeploymentParameters,
    result: ExecutorResult,
) -> VerificationResult:
    verified = (
        result.details.get("mock") is True
        and result.details.get("deployment") == parameters.deployment
        and result.details.get("traffic_state") == "paused"
    )
    return VerificationResult(
        verified=verified,
        message=(
            "Mock deployment traffic state is paused."
            if verified
            else "Mock deployment traffic state was not paused."
        ),
    )


def _preview_terminate_batch(parameters: TerminateBatchRunsParameters) -> RunbookPlan:
    return RunbookPlan(
        impact=(
            f"Mock terminate all active runs in {parameters.batch_group} in the demo environment."
        ),
        expected_changes={
            "batch_group": parameters.batch_group,
            "expected_active_runs": 3,
            "target_state": "terminated",
        },
    )


def _execute_terminate_batch(parameters: TerminateBatchRunsParameters) -> ExecutorResult:
    terminated_runs = (
        "demo-training-017",
        "demo-training-018",
        "demo-training-019",
    )
    return ExecutorResult(
        changed=True,
        message=f"Mock terminated {len(terminated_runs)} active batch runs.",
        details={
            "batch_group": parameters.batch_group,
            "environment": parameters.environment,
            "mock": True,
            "terminated_run_ids": terminated_runs,
        },
    )


def _verify_terminate_batch(
    parameters: TerminateBatchRunsParameters,
    result: ExecutorResult,
) -> VerificationResult:
    run_ids = result.details.get("terminated_run_ids")
    verified = (
        result.details.get("mock") is True
        and result.details.get("batch_group") == parameters.batch_group
        and isinstance(run_ids, tuple)
        and len(run_ids) == 3
    )
    return VerificationResult(
        verified=verified,
        message=(
            "All three mock active batch runs report terminated."
            if verified
            else "The mock batch termination result was incomplete."
        ),
    )


DEFAULT_RUNBOOK_DEFINITIONS: tuple[RunbookDefinition, ...] = (
    RunbookDefinition(
        runbook_id="demo.increase_db_ru_limit",
        description="Increase capacity for the allowlisted mock database.",
        parameter_model=IncreaseDbRuParameters,
        previewer=_preview_increase_db_ru,
        executor=_execute_increase_db_ru,
        verifier=_verify_increase_db_ru,
        risk=RunbookRisk.HIGH,
        confirmation=ConfirmationRequirement.READBACK_AND_PIN,
        execution_mode=ExecutionMode.MOCK,
        allowed_environments=("demo",),
        allowed_resources=("demo-orders",),
        rollback_guidance="Reset the deterministic demo scenario; no real resource changes.",
        maximum_execution_seconds=5,
    ),
    RunbookDefinition(
        runbook_id="demo.pause_deployment",
        description="Pause traffic for the allowlisted mock deployment.",
        parameter_model=PauseDeploymentParameters,
        previewer=_preview_pause_deployment,
        executor=_execute_pause_deployment,
        verifier=_verify_pause_deployment,
        risk=RunbookRisk.HIGH,
        confirmation=ConfirmationRequirement.READBACK_AND_PIN,
        execution_mode=ExecutionMode.MOCK,
        allowed_environments=("demo",),
        allowed_resources=("demo-api",),
        rollback_guidance="Resume the mock deployment; no real traffic is affected.",
        maximum_execution_seconds=5,
    ),
    RunbookDefinition(
        runbook_id="demo.terminate_batch_runs",
        description="Terminate active runs in the allowlisted mock training batch.",
        parameter_model=TerminateBatchRunsParameters,
        previewer=_preview_terminate_batch,
        executor=_execute_terminate_batch,
        verifier=_verify_terminate_batch,
        risk=RunbookRisk.HIGH,
        confirmation=ConfirmationRequirement.READBACK_AND_PIN,
        execution_mode=ExecutionMode.MOCK,
        allowed_environments=("demo",),
        allowed_resources=("demo-training",),
        rollback_guidance="Restart the deterministic demo runs from their mock checkpoints.",
        maximum_execution_seconds=5,
    ),
)

DEFAULT_RUNBOOK_REGISTRY = RunbookRegistry(DEFAULT_RUNBOOK_DEFINITIONS)


def create_default_registry(*, allow_real_execution: bool = False) -> RunbookRegistry:
    """Create an isolated registry containing only the deterministic demo runbooks."""

    return RunbookRegistry(
        DEFAULT_RUNBOOK_DEFINITIONS,
        allow_real_execution=allow_real_execution,
    )


def get_default_registry() -> RunbookRegistry:
    """Return the process-wide immutable default runbook registry."""

    return DEFAULT_RUNBOOK_REGISTRY


__all__ = [
    "DEFAULT_RUNBOOK_DEFINITIONS",
    "DEFAULT_RUNBOOK_REGISTRY",
    "ConfirmationRequirement",
    "ExecutionMode",
    "ExecutorResult",
    "IncreaseDbRuParameters",
    "PauseDeploymentParameters",
    "RealExecutionDisabledError",
    "RunbookConfirmationRequiredError",
    "RunbookDefinition",
    "RunbookError",
    "RunbookExecution",
    "RunbookNotFoundError",
    "RunbookParameterError",
    "RunbookPlan",
    "RunbookPreview",
    "RunbookRegistry",
    "RunbookRisk",
    "RunbookSummary",
    "RunbookVerificationError",
    "StrictRunbookParameters",
    "TerminateBatchRunsParameters",
    "VerificationResult",
    "create_default_registry",
    "get_default_registry",
]
