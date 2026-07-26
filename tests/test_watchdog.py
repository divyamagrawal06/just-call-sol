from agent_hotline.watchdog import AgentFailureWatchdog, WatchdogPolicy


def test_repeated_codex_error_escalates_without_model_tool_choice() -> None:
    watchdog = AgentFailureWatchdog(WatchdogPolicy(repeated_error_threshold=2, cooldown_seconds=60))
    payload = {
        "threadId": "thr_1",
        "turnId": "turn_1",
        "message": "upstream provider returned 503",
    }
    assert watchdog.evaluate_codex_notification("error", payload) is None
    request = watchdog.evaluate_codex_notification("error", payload)
    assert request is not None
    assert request.source == "codex_app_server"
    assert request.kind == "provider_failure"
    assert request.context.thread_id == "thr_1"


def test_watchdog_suppresses_duplicate_during_cooldown() -> None:
    watchdog = AgentFailureWatchdog(WatchdogPolicy(repeated_error_threshold=1, cooldown_seconds=60))
    payload = {"threadId": "thr_1", "message": "provider unavailable"}
    assert watchdog.evaluate_codex_notification("error", payload) is not None
    assert watchdog.evaluate_codex_notification("error", payload) is None


def test_nonzero_process_exit_escalates() -> None:
    watchdog = AgentFailureWatchdog()
    request = watchdog.evaluate_process_exit(
        agent="codex",
        return_code=1,
        thread_id="thr_2",
        stderr_tail="connection reset",
    )
    assert request is not None
    assert request.source == "watchdog"
    assert request.severity == "high"


def test_low_severity_infrastructure_alert_does_not_call() -> None:
    watchdog = AgentFailureWatchdog()
    request = watchdog.evaluate_infrastructure_alert(
        alert_name="cpu",
        summary="CPU reached 70 percent",
        severity="medium",
        resource_ref="service/api",
    )
    assert request is None


def test_critical_infrastructure_alert_is_grounded() -> None:
    watchdog = AgentFailureWatchdog()
    request = watchdog.evaluate_infrastructure_alert(
        alert_name="db-ru",
        summary="Database request units are exhausted and requests are failing.",
        severity="critical",
        resource_ref="db/prod",
        evidence={"failed_requests": 42},
    )
    assert request is not None
    assert request.kind == "incident"
    assert request.context.evidence[0].ref == "db/prod"
