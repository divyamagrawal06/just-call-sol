from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from agent_hotline.settings import reset_settings_cache

_SETTINGS_ENV_PREFIXES = (
    "HOTLINE_",
    "OPENAI_",
    "TWILIO_",
    "VAPI_",
    "VOBIZ_",
    "OWNER_",
)
_SETTINGS_ENV_NAMES = frozenset({"PUBLIC_BASE_URL"})


@pytest.fixture(autouse=True)
def isolate_settings_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep a developer's runtime credentials and feature flags out of tests.

    Values installed by a test with ``monkeypatch.setenv`` remain available
    because isolation happens before the test and its explicit fixtures run.
    """

    for name in tuple(os.environ):
        normalized = name.upper()
        if normalized in _SETTINGS_ENV_NAMES or normalized.startswith(_SETTINGS_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)

    reset_settings_cache()
    try:
        yield
    finally:
        reset_settings_cache()
