from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent_hotline.contracts import (
    BeginInboundSessionRequest,
    ConfirmActionRequest,
    EscalationContextRequest,
    ExecuteActionRequest,
    PrepareActionRequest,
    RecordInstructionRequest,
    SarvamRepositoryContextRequest,
    ThreadInspectRequest,
    ThreadListRequest,
)
from agent_hotline.sarvam import (
    CreateDeploymentRequest,
    DeploymentConnectionConfig,
    DeploymentDetails,
    DeploymentList,
    DeploymentSummary,
    InboundConfig,
    SarvamAPIError,
)
from agent_hotline.sarvam_setup import (
    LIVE_TOOL_NAMES,
    PUBLIC_BASE_URL_PLACEHOLDER,
    DeploymentConfigurationError,
    DeploymentConflictError,
    DeploymentSetupError,
    build_inbound_deployment_request,
    build_samvaad_tool_manifest,
    ensure_inbound_deployment,
    list_all_deployment_summaries,
    list_all_deployments,
    render_tool_manifest_markdown,
    safe_deployment_view,
)
from agent_hotline.settings import Settings


def configured_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "sarvam_api_key": SecretStr("never-print-this-api-key"),
        "sarvam_org_id": "org_test",
        "sarvam_workspace_id": "workspace_test",
        "sarvam_app_id": "app_test",
        "sarvam_app_version": 3,
        "sarvam_connection_id": "connection_test",
        "sarvam_agent_phone_number": "+918000000001",
        "sarvam_inbound_schedule": None,
        "owner_phone_number": SecretStr("+918000000002"),
        "owner_confirmation_pin": SecretStr("246810"),
        "hotline_tool_token": SecretStr("never-print-this-tool-token"),
        "hotline_public_tools_require_token": True,
        "public_base_url": "https://voice.example.test",
    }
    values.update(overrides)
    return Settings(**values)


def deployment(
    *,
    deployment_id: str = "deployment_1",
    name: str = "Agent Hotline",
    app_id: str = "app_test",
    app_version: int = 3,
    connection_id: str = "connection_test",
    phone: str = "+918000000001",
    direction: str = "inbound",
    status: str = "active",
    inbound_config: dict[str, object] | None = None,
) -> DeploymentDetails:
    return DeploymentDetails.model_validate(
        {
            "deployment_id": deployment_id,
            "name": name,
            "app_id": app_id,
            "app_version": app_version,
            "connection_configs": [
                {
                    "connection_id": connection_id,
                    "phone_numbers": [phone],
                }
            ],
            "channel_direction": direction,
            "status": status,
            "description": "test",
            "inbound_config": inbound_config,
            "created_by": "tester",
            "created_at": datetime.now(UTC),
        }
    )


class FakeDeploymentClient:
    def __init__(self, deployments: list[DeploymentDetails]) -> None:
        self.deployments = deployments
        self.create_calls: list[CreateDeploymentRequest] = []
        self.list_calls: list[tuple[int, int]] = []
        self.detail_calls: list[str] = []

    async def list_deployments(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        search: str | None = None,
    ) -> DeploymentList:
        del search
        self.list_calls.append((offset, limit))
        details = self.deployments[offset : offset + limit]
        items = [
            DeploymentSummary(
                deployment_id=item.deployment_id,
                name=item.name,
                app_id=item.app_id,
                app_version=item.app_version,
                phone_numbers=[
                    number
                    for connection in item.connection_configs
                    for number in connection.phone_numbers
                ],
                channel_direction=item.channel_direction,
                status=item.status,
                description=item.description,
                inbound_config=item.inbound_config,
                created_by=item.created_by,
                created_at=item.created_at,
                updated_by=item.updated_by,
                updated_at=item.updated_at,
            )
            for item in details
        ]
        next_page_uri = (
            f"/deployments?offset={offset + len(items)}"
            if offset + len(items) < len(self.deployments)
            else None
        )
        return DeploymentList(
            items=items,
            total=len(self.deployments),
            limit=limit,
            offset=offset,
            next_page_uri=next_page_uri,
        )

    async def get_deployment(self, deployment_id: str) -> DeploymentDetails:
        self.detail_calls.append(deployment_id)
        return next(item for item in self.deployments if item.deployment_id == deployment_id)

    async def create_inbound_deployment(
        self,
        request: CreateDeploymentRequest,
    ) -> DeploymentDetails:
        self.create_calls.append(request)
        created = deployment(
            deployment_id=f"deployment_{len(self.deployments) + 1}",
            name=request.name,
            app_id=request.app_id,
            app_version=request.app_version,
            connection_id=request.connection_configs[0].connection_id,
            phone=request.connection_configs[0].phone_numbers[0],
            inbound_config=(
                request.inbound_config.model_dump(mode="json") if request.inbound_config else None
            ),
        )
        self.deployments.append(created)
        return created


def test_build_inbound_request_uses_settings_and_omits_schedule() -> None:
    request = build_inbound_deployment_request(configured_settings())

    assert request.name == "Agent Hotline"
    assert request.app_id == "app_test"
    assert request.app_version == 3
    assert request.connection_configs == [
        DeploymentConnectionConfig(
            connection_id="connection_test",
            phone_numbers=["+918000000001"],
        )
    ]
    assert request.inbound_config is None


def test_build_inbound_request_uses_explicit_atomic_schedule() -> None:
    schedule = {
        "start_time": "09:00",
        "end_time": "18:00",
        "allowed_days": [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ],
        "timezone": "Asia/Kolkata",
    }

    request = build_inbound_deployment_request(
        configured_settings(sarvam_inbound_schedule=json.dumps(schedule))
    )

    assert request.inbound_config == InboundConfig.model_validate(schedule)


@pytest.mark.parametrize(
    "schedule",
    (
        "not-json",
        "[]",
        '{"start_time":"9am","end_time":"18:00","allowed_days":["Monday"]}',
        '{"start_time":"09:00","end_time":"18:00","allowed_days":[]}',
        (
            '{"start_time":"09:00","end_time":"18:00",'
            '"allowed_days":["Funday"],"timezone":"Asia/Kolkata"}'
        ),
    ),
)
def test_build_inbound_request_rejects_invalid_explicit_schedule(schedule: str) -> None:
    with pytest.raises(
        DeploymentConfigurationError,
        match="SARVAM_INBOUND_SCHEDULE",
    ) as error:
        build_inbound_deployment_request(configured_settings(sarvam_inbound_schedule=schedule))

    assert schedule not in str(error.value)


def test_schedule_can_be_loaded_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    schedule = (
        '{"start_time":"09:00","end_time":"18:00",'
        '"allowed_days":["Monday"],"timezone":"Asia/Kolkata"}'
    )
    monkeypatch.setenv("SARVAM_INBOUND_SCHEDULE", schedule)

    settings = Settings(_env_file=None)

    assert settings.sarvam_inbound_schedule == schedule
    assert settings.diagnostics()["sarvam_inbound_schedule_configured"] is True


def test_build_inbound_request_names_missing_configuration_without_secrets() -> None:
    settings = configured_settings(
        sarvam_app_id=None,
        sarvam_connection_id=None,
        sarvam_agent_phone_number=None,
    )

    with pytest.raises(DeploymentConfigurationError) as error:
        build_inbound_deployment_request(settings)

    message = str(error.value)
    assert "SARVAM_APP_ID" in message
    assert "SARVAM_CONNECTION_ID" in message
    assert "SARVAM_AGENT_PHONE_NUMBER" in message
    assert "never-print-this-api-key" not in message


@pytest.mark.asyncio
async def test_matching_deployment_is_idempotently_reused() -> None:
    client = FakeDeploymentClient([deployment()])

    first = await ensure_inbound_deployment(client, configured_settings(), apply=True)
    second = await ensure_inbound_deployment(client, configured_settings(), apply=True)

    assert first.state == second.state == "ready"
    assert first.app_version == second.app_version == 3
    assert first.matched_by == "name"
    assert not first.changed
    assert client.create_calls == []


@pytest.mark.asyncio
async def test_explicit_schedule_is_part_of_exact_reconciliation() -> None:
    desired_schedule = {
        "start_time": "09:00",
        "end_time": "18:00",
        "allowed_days": [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ],
        "timezone": "Asia/Kolkata",
    }
    live_schedule = {
        **desired_schedule,
        "start_time": "09:00:00",
        "end_time": "18:00:00",
    }
    settings = configured_settings(sarvam_inbound_schedule=json.dumps(desired_schedule))
    client = FakeDeploymentClient([deployment(inbound_config=live_schedule)])

    result = await ensure_inbound_deployment(client, settings, apply=True)

    assert result.state == "ready"
    assert result.matched_by == "name"
    assert client.create_calls == []


@pytest.mark.parametrize("existing_end_time", ("17:00", "18:00:01", "18:00:bogus"))
@pytest.mark.asyncio
async def test_explicit_schedule_mismatch_fails_closed(existing_end_time: str) -> None:
    desired = {
        "start_time": "09:00",
        "end_time": "18:00",
        "allowed_days": ["Monday"],
        "timezone": "Asia/Kolkata",
    }
    existing = {
        **desired,
        "start_time": "09:00:00",
        "end_time": existing_end_time,
    }
    settings = configured_settings(sarvam_inbound_schedule=json.dumps(desired))
    client = FakeDeploymentClient([deployment(inbound_config=existing)])

    with pytest.raises(DeploymentConflictError, match="different configuration"):
        await ensure_inbound_deployment(client, settings, apply=True)

    assert client.create_calls == []


@pytest.mark.asyncio
async def test_equivalent_differently_named_deployment_is_adopted() -> None:
    client = FakeDeploymentClient([deployment(name="Existing voice ingress")])

    result = await ensure_inbound_deployment(client, configured_settings(), apply=True)

    assert result.state == "ready"
    assert result.matched_by == "equivalent_config"
    assert result.deployment_name == "Existing voice ingress"
    assert client.create_calls == []


@pytest.mark.asyncio
async def test_paused_match_is_reported_without_implicit_resume() -> None:
    client = FakeDeploymentClient([deployment(status="paused")])

    result = await ensure_inbound_deployment(client, configured_settings(), apply=True)

    assert result.state == "paused"
    assert not result.changed
    assert "not resumed" in result.message
    assert client.create_calls == []


@pytest.mark.asyncio
async def test_same_name_mismatch_fails_closed() -> None:
    client = FakeDeploymentClient([deployment(app_version=2)])

    with pytest.raises(
        DeploymentConflictError,
        match="Configured app version is 3; existing version\\(s\\): 2",
    ) as error:
        await ensure_inbound_deployment(client, configured_settings(), apply=True)

    assert "app_test" not in str(error.value)
    assert client.create_calls == []


@pytest.mark.asyncio
async def test_competing_phone_binding_fails_closed() -> None:
    client = FakeDeploymentClient([deployment(name="Other", app_id="different_app")])

    with pytest.raises(DeploymentConflictError, match="competing binding"):
        await ensure_inbound_deployment(client, configured_settings(), apply=True)

    assert client.create_calls == []


@pytest.mark.asyncio
async def test_missing_deployment_requires_apply_then_becomes_idempotent() -> None:
    client = FakeDeploymentClient([])

    plan = await ensure_inbound_deployment(client, configured_settings())
    assert plan.state == "missing"
    assert plan.app_version == 3
    assert not plan.changed
    assert client.create_calls == []

    applied = await ensure_inbound_deployment(client, configured_settings(), apply=True)
    assert applied.state == "created"
    assert applied.changed
    assert len(client.create_calls) == 1

    repeated = await ensure_inbound_deployment(client, configured_settings(), apply=True)
    assert repeated.state == "ready"
    assert not repeated.changed
    assert len(client.create_calls) == 1


@pytest.mark.asyncio
async def test_list_all_deployments_paginates_and_deduplicates() -> None:
    client = FakeDeploymentClient(
        [
            deployment(deployment_id="deployment_1"),
            deployment(deployment_id="deployment_2", name="Second"),
            deployment(deployment_id="deployment_3", name="Third"),
        ]
    )

    result = await list_all_deployments(client, page_size=2)

    assert [item.deployment_id for item in result] == [
        "deployment_1",
        "deployment_2",
        "deployment_3",
    ]
    assert client.list_calls == [(0, 2), (2, 2)]
    assert client.detail_calls == [
        "deployment_1",
        "deployment_2",
        "deployment_3",
    ]


@pytest.mark.asyncio
async def test_list_summaries_uses_live_flattened_phone_shape_without_detail_gets() -> None:
    client = FakeDeploymentClient([deployment()])

    summaries = await list_all_deployment_summaries(client)

    assert summaries[0].phone_numbers == ["+918000000001"]
    assert not hasattr(summaries[0], "connection_configs")
    assert client.detail_calls == []


@pytest.mark.asyncio
async def test_reconciliation_fails_closed_when_list_detail_cannot_be_fetched() -> None:
    class UnavailableDetailClient(FakeDeploymentClient):
        async def get_deployment(self, deployment_id: str) -> DeploymentDetails:
            self.detail_calls.append(deployment_id)
            raise SarvamAPIError("detail unavailable", status_code=404)

    client = UnavailableDetailClient([deployment()])

    with pytest.raises(
        DeploymentSetupError,
        match="refusing to create or reconcile a binding",
    ):
        await ensure_inbound_deployment(client, configured_settings(), apply=True)

    assert client.create_calls == []


def test_tool_manifest_has_all_endpoints_but_no_configured_secrets() -> None:
    settings = configured_settings()

    manifest = build_samvaad_tool_manifest(settings)
    rendered = manifest.model_dump_json()
    markdown = render_tool_manifest_markdown(manifest)

    assert len(manifest.tools) == 9
    assert tuple(tool.name for tool in manifest.tools) == LIVE_TOOL_NAMES
    assert all(tool.url.startswith("https://voice.example.test/") for tool in manifest.tools)
    assert "/v1/sarvam/tools/confirm-action" in rendered
    assert "${HOTLINE_TOOL_TOKEN}" in rendered
    assert all(
        tool.headers["Authorization"] == "Bearer ${HOTLINE_TOOL_TOKEN}" for tool in manifest.tools
    )
    assert all(
        "Authorization: Bearer ${HOTLINE_TOOL_TOKEN}" in tool.curl for tool in manifest.tools
    )
    assert "never-print-this-tool-token" not in rendered
    assert "never-print-this-api-key" not in rendered
    assert "246810" not in rendered
    assert "{{ephemeral_confirmation_pin}}" in rendered
    prepare_action = next(tool for tool in manifest.tools if tool.name == "prepare_action")
    assert prepare_action.request_body_example == {
        "event_id": "{{event_id}}",
        "action_type": "{{action_type}}",
        "action_reference": "{{action_reference}}",
        "action_instruction": "{{action_instruction}}",
        "action_turn_id": "{{action_turn_id}}",
        "action_task": "{{action_task}}",
        "action_cwd": ".",
        "action_confirmed_thread_id": "{{action_confirmed_thread_id}}",
        "action_target_ru": 0,
        "action_pause_reason": "{{action_pause_reason}}",
    }
    assert set(prepare_action.input_bindings) == set(prepare_action.request_body_example)
    assert prepare_action.input_bindings["event_id"].source == "Agent variable"
    assert prepare_action.input_bindings["action_type"].source == "Let the agent decide"
    assert prepare_action.input_bindings["action_cwd"].source == "Fixed value"
    assert prepare_action.input_bindings["action_cwd"].fixed_value == "."
    assert prepare_action.input_bindings["action_target_ru"].json_type == "integer"
    for field in (
        "action_reference",
        "action_instruction",
        "action_turn_id",
        "action_task",
        "action_confirmed_thread_id",
        "action_target_ru",
        "action_pause_reason",
    ):
        assert prepare_action.input_bindings[field].source == "Let the agent decide"
    for undeclared_scope_field in (
        "parameters",
        "workspace_ref",
        "thread_id",
        "commit_or_state_hash",
    ):
        assert undeclared_scope_field not in prepare_action.request_body_example
        assert undeclared_scope_field not in prepare_action.input_bindings
    action_description = prepare_action.input_bindings["action_type"].description
    for action_type in (
        "thread.instruct",
        "thread.interrupt",
        "thread.spawn_root",
        "thread.archive",
        "demo.increase_db_ru_limit",
        "demo.pause_deployment",
        "demo.terminate_batch_runs",
    ):
        assert action_type in action_description
    record_decision = next(tool for tool in manifest.tools if tool.name == "record_decision")
    assert record_decision.request_body_example["outcome"] == "{{decision_outcome}}"
    assert record_decision.request_body_example == {
        "event_id": "{{event_id}}",
        "outcome": "{{decision_outcome}}",
        "instruction": "{{confirmed_instruction}}",
        "constraints": [],
        "approved_action_ids": [],
        "confirmation_method": "spoken_plus_dtmf",
        "confirmation_pin": "{{ephemeral_confirmation_pin}}",
    }
    assert "confirmation_pin" not in record_decision.response_variables
    assert "approved_action_ids" not in record_decision.response_variables
    assert "record_decision never grants a registered action" in rendered
    repo_context = next(tool for tool in manifest.tools if tool.name == "repo_context")
    assert repo_context.request_body_example["confirmation_pin"] == (
        "{{ephemeral_confirmation_pin}}"
    )
    assert repo_context.request_body_example["operation"] == "{{repo_operation}}"
    assert repo_context.request_body_example["query"] == "{{repo_query}}"
    assert repo_context.request_body_example["path"] == "{{repo_path}}"
    assert repo_context.request_body_example["line_start"] == "{{repo_line_start}}"
    assert repo_context.request_body_example["workspace"] == ""
    assert repo_context.response_variables == {}
    assert repo_context.input_bindings["event_id"].source == "Agent variable"
    assert repo_context.input_bindings["workspace"].source == "Fixed value"
    assert repo_context.input_bindings["workspace"].fixed_value == ""
    for field in ("operation", "query", "path", "line_start", "confirmation_pin"):
        assert repo_context.input_bindings[field].source == "Let the agent decide"
    for field in ("line_count", "max_results"):
        assert repo_context.input_bindings[field].source == "Fixed value"
    record_decision = next(tool for tool in manifest.tools if tool.name == "record_decision")
    assert record_decision.input_bindings["event_id"].source == "Agent variable"
    for field in ("outcome", "instruction", "confirmation_pin"):
        assert record_decision.input_bindings[field].source == "Let the agent decide"
    for field in ("constraints", "approved_action_ids", "confirmation_method"):
        assert record_decision.input_bindings[field].source == "Fixed value"
    assert "Request value sources:" in markdown
    assert "Let the agent decide" in markdown
    assert "curl --request POST" in markdown


@pytest.mark.parametrize(
    ("action_type", "field_values", "expected_parameters"),
    (
        (
            "thread.instruct",
            {
                "action_reference": "training-run",
                "action_instruction": "Stop all active batch work.",
            },
            {
                "reference": "training-run",
                "instruction": "Stop all active batch work.",
            },
        ),
        (
            "thread.interrupt",
            {
                "action_reference": "training-run",
                "action_turn_id": "turn-7",
            },
            {"reference": "training-run", "turn_id": "turn-7"},
        ),
        (
            "thread.spawn_root",
            {"action_task": "Investigate the database incident."},
            {"task": "Investigate the database incident.", "cwd": "."},
        ),
        (
            "thread.archive",
            {
                "action_reference": "old-task",
                "action_confirmed_thread_id": "thread-123",
            },
            {"reference": "old-task", "confirmed_thread_id": "thread-123"},
        ),
        (
            "demo.increase_db_ru_limit",
            {"action_target_ru": "800"},
            {"target_ru": 800},
        ),
        (
            "demo.pause_deployment",
            {"action_pause_reason": "incident-response"},
            {"reason": "incident-response"},
        ),
        ("demo.terminate_batch_runs", {}, {}),
    ),
)
def test_flat_samvaad_action_fields_map_to_strict_backend_parameters(
    action_type: str,
    field_values: dict[str, object],
    expected_parameters: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "event_id": "event-1",
        "action_type": action_type,
        "action_reference": "",
        "action_instruction": "",
        "action_turn_id": "",
        "action_task": "",
        "action_cwd": ".",
        "action_confirmed_thread_id": "",
        "action_target_ru": 0,
        "action_pause_reason": "",
        **field_values,
    }

    request = PrepareActionRequest.model_validate(payload)

    assert request.parameters == expected_parameters
    assert request.workspace_ref is None
    assert request.thread_id is None
    assert request.commit_or_state_hash is None


@pytest.mark.parametrize(
    "payload",
    (
        {
            "event_id": "event-1",
            "action_type": "thread.spawn_root",
            "action_task": "Investigate.",
            "action_cwd": "C:/arbitrary/path",
        },
        {
            "event_id": "event-1",
            "action_type": "thread.spawn_root",
            "action_cwd": ".",
        },
        {
            "event_id": "event-1",
            "action_type": "thread.interrupt",
            "action_reference": "task-1",
            "action_task": "Unrelated injected task.",
            "action_cwd": ".",
        },
        {
            "event_id": "event-1",
            "action_type": "demo.increase_db_ru_limit",
            "action_target_ru": 400,
            "action_cwd": ".",
        },
        {
            "event_id": "event-1",
            "action_type": "thread.spawn_root",
            "parameters": {"task": "Legacy task", "cwd": "."},
            "action_task": "Conflicting task.",
            "action_cwd": ".",
        },
    ),
)
def test_flat_samvaad_action_fields_reject_unsafe_or_ambiguous_payloads(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        PrepareActionRequest.model_validate(payload)


def test_development_demo_tool_manifest_omits_authorization_everywhere() -> None:
    manifest = build_samvaad_tool_manifest(
        configured_settings(hotline_public_tools_require_token=False)
    )
    rendered = manifest.model_dump_json()
    markdown = render_tool_manifest_markdown(manifest)

    assert manifest.authentication["type"] == "none-development-demo"
    assert all(tool.headers == {"Content-Type": "application/json"} for tool in manifest.tools)
    assert all("Authorization" not in tool.curl for tool in manifest.tools)
    assert '"Authorization":' not in rendered
    assert "${HOTLINE_TOOL_TOKEN}" not in rendered
    assert "Authentication: intentionally omitted" in markdown
    assert "Production configuration rejects this mode" in markdown


def test_each_live_tool_body_matches_its_current_daemon_schema() -> None:
    manifest = build_samvaad_tool_manifest(configured_settings())
    request_models = {
        "begin_inbound": BeginInboundSessionRequest,
        "get_context": EscalationContextRequest,
        "record_decision": RecordInstructionRequest,
        "prepare_action": PrepareActionRequest,
        "confirm_action": ConfirmActionRequest,
        "execute_action": ExecuteActionRequest,
        "list_threads": ThreadListRequest,
        "inspect_thread": ThreadInspectRequest,
        "repo_context": SarvamRepositoryContextRequest,
    }
    endpoint_paths = {
        "begin_inbound": "/v1/sarvam/tools/begin-inbound",
        "get_context": "/v1/sarvam/tools/context",
        "record_decision": "/v1/sarvam/tools/record-instruction",
        "prepare_action": "/v1/sarvam/tools/prepare-action",
        "confirm_action": "/v1/sarvam/tools/confirm-action",
        "execute_action": "/v1/sarvam/tools/execute-action",
        "list_threads": "/v1/sarvam/tools/threads/list",
        "inspect_thread": "/v1/sarvam/tools/threads/inspect",
        "repo_context": "/v1/sarvam/tools/repository-context",
    }

    for tool in manifest.tools:
        schema = request_models[tool.name].model_json_schema()
        body_fields = set(tool.request_body_example)
        assert body_fields <= set(schema["properties"])
        assert set(schema.get("required", [])) <= body_fields
        assert tool.url.endswith(endpoint_paths[tool.name])
        assert "identity_verified" not in body_fields


def test_tool_manifest_uses_safe_placeholder_without_public_url() -> None:
    manifest = build_samvaad_tool_manifest(configured_settings(public_base_url=None))

    assert manifest.base_url == PUBLIC_BASE_URL_PLACEHOLDER
    assert all(PUBLIC_BASE_URL_PLACEHOLDER in tool.url for tool in manifest.tools)


def test_samvaad_prompt_uses_exact_live_names_and_never_declares_pin_variable() -> None:
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "samvaad" / "agent_prompt.md").read_text(encoding="utf-8")
    contracts = (root / "samvaad" / "tool_contracts.md").read_text(encoding="utf-8")
    variables = (root / "samvaad" / "variables.json").read_text(encoding="utf-8")
    exact_names = {
        "begin_inbound",
        "get_context",
        "record_decision",
        "prepare_action",
        "confirm_action",
        "execute_action",
        "list_threads",
        "inspect_thread",
        "repo_context",
    }

    for name in exact_names:
        assert f"`{name}`" in prompt
        assert f"`{name}`" in contracts
    for stale_name in (
        "begin_inbound_session",
        "get_escalation_context",
        "record_instruction",
        "list_agent_threads",
        "inspect_agent_thread",
    ):
        assert f"`{stale_name}`" not in prompt
        assert f"`{stale_name}`" not in contracts
    assert "confirmation_pin" not in variables
    assert "identity_verified" not in variables
    assert "repo_operation" not in variables
    assert "repo_query" not in variables
    assert "repo_path" not in variables
    assert "repo_workspace" not in variables
    assert "repo_line_start" not in variables


@pytest.mark.parametrize(
    "base_url",
    [
        "http://voice.example.test",
        "https://user:password@voice.example.test",
        "https://voice.example.test?token=secret",
        "not-a-url",
    ],
)
def test_tool_manifest_rejects_unsafe_public_urls(base_url: str) -> None:
    with pytest.raises(DeploymentConfigurationError):
        build_samvaad_tool_manifest(configured_settings(), base_url=base_url)


def test_safe_deployment_view_masks_phone_number() -> None:
    view = safe_deployment_view(deployment())

    rendered = json_dumps(view)
    masked = view["connections"][0]["phone_numbers"][0]
    assert "+918000000001" not in rendered
    assert masked.endswith("0001")
    assert "*" in masked


def test_safe_deployment_summary_view_masks_flattened_phone_number() -> None:
    item = deployment()
    summary = DeploymentSummary(
        deployment_id=item.deployment_id,
        name=item.name,
        app_id=item.app_id,
        app_version=item.app_version,
        phone_numbers=["+918000000001"],
        channel_direction=item.channel_direction,
        status=item.status,
        created_by=item.created_by,
        created_at=item.created_at,
    )

    view = safe_deployment_view(summary)

    assert view["connections"][0]["connection_id"] is None
    assert view["connections"][0]["phone_numbers"] == ["+91******0001"]


def json_dumps(value: object) -> str:
    import json

    return json.dumps(value, sort_keys=True)
