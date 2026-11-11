"""Tests for the Tencent Cloud TokenHub provider presets.

Covers: provider presets (three regional sites), CLI registration, factory
wiring, request URL/auth header, JSON-mode fallback for TokenHub model ids.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from researchclaw.cli import (
    _PROVIDER_CHOICES,
    _PROVIDER_MODELS,
    _PROVIDER_URLS,
    cmd_init,
)
from researchclaw.llm import PROVIDER_PRESETS, create_llm_client
from researchclaw.llm.client import LLMClient, LLMConfig

_TOKENHUB_SITES = {
    "tokenhub": "https://tokenhub.tencentcloudmaas.com/v1",
    "tokenhub-intl": "https://tokenhub-intl.tencentcloudmaas.com/v1",
    "tokenhub-us": "https://tokenhub-us.tencentcloudmaas.tech/v1",
}


class _DummyHTTPResponse:
    def __init__(self, payload: Mapping[str, Any]):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _DummyHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _rc_config(
    *,
    provider: str = "tokenhub",
    api_key: str = "",
    api_key_env: str = "TOKENHUB_API_KEY",
    base_url: str = "",
    primary_model: str = "deepseek-v4.1-flash",
    fallback_models: tuple[str, ...] = ("deepseek-v4-flash", "hy4"),
) -> SimpleNamespace:
    return SimpleNamespace(
        llm=SimpleNamespace(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            wire_api="chat_completions",
            primary_model=primary_model,
            fallback_models=fallback_models,
            timeout_sec=60,
        ),
    )


# ---------------------------------------------------------------------------
# Provider presets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("provider", "base_url"), sorted(_TOKENHUB_SITES.items()))
def test_tokenhub_presets(provider: str, base_url: str) -> None:
    assert PROVIDER_PRESETS[provider]["base_url"] == base_url
    # OpenAI-compatible: no Anthropic adapter.
    assert PROVIDER_PRESETS[provider].get("adapter") is None


# ---------------------------------------------------------------------------
# from_rc_config wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("provider", "base_url"), sorted(_TOKENHUB_SITES.items()))
def test_from_rc_config_uses_preset(
    provider: str, base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKENHUB_API_KEY", "th-key")
    client = LLMClient.from_rc_config(_rc_config(provider=provider))

    assert client.config.base_url == base_url
    assert client.config.api_key == "th-key"
    assert client.config.primary_model == "deepseek-v4.1-flash"
    assert client._model_chain == ["deepseek-v4.1-flash", "deepseek-v4-flash", "hy4"]
    assert client._anthropic is None


def test_from_rc_config_reads_custom_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKENHUB_INTL_API_KEY", "intl-key")
    monkeypatch.delenv("TOKENHUB_API_KEY", raising=False)
    client = LLMClient.from_rc_config(
        _rc_config(provider="tokenhub-intl", api_key_env="TOKENHUB_INTL_API_KEY")
    )
    assert client.config.api_key == "intl-key"


def test_inline_api_key_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKENHUB_API_KEY", "env-key")
    client = LLMClient.from_rc_config(
        _rc_config(api_key="inline-key", api_key_env="TOKENHUB_API_KEY")
    )
    assert client.config.api_key == "inline-key"


def test_custom_base_url_overrides_preset() -> None:
    client = LLMClient.from_rc_config(
        _rc_config(api_key="k", api_key_env="", base_url="https://proxy.test/v1")
    )
    assert client.config.base_url == "https://proxy.test/v1"


def test_create_llm_client_returns_llm_client() -> None:
    client = create_llm_client(_rc_config(api_key="th-key", api_key_env=""))
    assert isinstance(client, LLMClient)
    assert client.config.base_url == _TOKENHUB_SITES["tokenhub"]


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("provider", "base_url"), sorted(_TOKENHUB_SITES.items()))
def test_request_uses_openai_compatible_endpoint(
    provider: str, base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(
        request: urllib.request.Request, timeout: int
    ) -> _DummyHTTPResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return _DummyHTTPResponse(
            {
                "model": "deepseek-v4.1-flash",
                "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = LLMClient(
        LLMConfig(
            base_url=base_url,
            api_key="th-key",
            primary_model="deepseek-v4.1-flash",
            fallback_models=[],
            timeout_sec=60,
        )
    )

    response = client._raw_call(
        "deepseek-v4.1-flash", [{"role": "user", "content": "ping"}], 1024, 0.2, False
    )

    request = captured["request"]
    assert isinstance(request, urllib.request.Request)
    assert request.full_url == f"{base_url}/chat/completions"
    assert request.get_header("Authorization") == "Bearer th-key"
    assert captured["timeout"] == 60
    assert response.content == "pong"


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-v4.1-flash",
        "deepseek/deepseek-flash",
        "hy4",
        "glm-5.3",
        "kimi-k3.1-pro",
        "minimax-m3",
        "mimo-v2.6-pro",
        "step-5-preview",
    ],
)
def test_json_mode_skips_response_format_for_tokenhub_models(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TokenHub models do not reliably support ``response_format``."""
    captured: dict[str, Any] = {}

    def fake_urlopen(
        request: urllib.request.Request, timeout: int
    ) -> _DummyHTTPResponse:
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _DummyHTTPResponse(
            {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = LLMClient(
        LLMConfig(
            base_url=_TOKENHUB_SITES["tokenhub"],
            api_key="th-key",
            primary_model=model,
            fallback_models=[],
        )
    )
    client._raw_call(model, [{"role": "user", "content": "json"}], 1024, 0.5, True)

    body = captured["body"]
    assert "response_format" not in body
    messages = body["messages"]
    assert any(
        "JSON" in m.get("content", "") for m in messages if m["role"] == "system"
    )


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def test_tokenhub_in_provider_choices() -> None:
    providers = {value[0] for value in _PROVIDER_CHOICES.values()}
    assert set(_TOKENHUB_SITES) <= providers


@pytest.mark.parametrize(("provider", "base_url"), sorted(_TOKENHUB_SITES.items()))
def test_tokenhub_in_provider_urls(provider: str, base_url: str) -> None:
    assert _PROVIDER_URLS[provider] == base_url


def test_tokenhub_in_provider_models() -> None:
    for provider in _TOKENHUB_SITES:
        primary, fallbacks = _PROVIDER_MODELS[provider]
        assert primary == "deepseek-v4.1-flash"
        assert "deepseek-v4-flash" in fallbacks


@pytest.mark.parametrize(
    ("provider", "api_key_env", "base_url"), sorted(
        (p, env, _TOKENHUB_SITES[p])
        for p, env in (
            ("tokenhub", "TOKENHUB_API_KEY"),
            ("tokenhub-intl", "TOKENHUB_INTL_API_KEY"),
            ("tokenhub-us", "TOKENHUB_US_API_KEY"),
        )
    )
)
def test_init_wizard_writes_tokenhub_defaults(
    provider: str,
    api_key_env: str,
    base_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TTY:
        def isatty(self) -> bool:
            return True

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", _TTY())
    # Look the choice number up instead of hard-coding it.
    choice = next(
        key for key, value in _PROVIDER_CHOICES.items() if value[0] == provider
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: choice)
    monkeypatch.setattr("researchclaw.cli._prompt_opencode_install", lambda: False)

    assert cmd_init(argparse.Namespace(force=False)) == 0
    content = (tmp_path / "config.arc.yaml").read_text(encoding="utf-8")

    assert f'provider: "{provider}"' in content
    assert f'base_url: "{base_url}"' in content
    assert f'api_key_env: "{api_key_env}"' in content
    assert 'primary_model: "deepseek-v4.1-flash"' in content
    assert '    - "deepseek-v4-flash"' in content
