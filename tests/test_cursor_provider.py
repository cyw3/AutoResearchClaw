"""Cursor SDK provider (config, preflight, timeout) — no live Agent.prompt."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.config import RCConfig


def _write_cursor_config(
    tmp_path: Path,
    *,
    cwd: Path,
    timeout_sec: int | None = 1800,
    cursor_timeout_sec: int | None = None,
) -> Path:
    llm_lines = [
        "  provider: cursor",
        "  api_key_env: CURSOR_API_KEY",
        "  primary_model: composer-2.5",
    ]
    if timeout_sec is not None:
        llm_lines.append(f"  timeout_sec: {timeout_sec}")
    llm_lines.extend(
        [
            "  cursor:",
            f'    cwd: "{cwd}"',
            "    sandbox_enabled: false",
        ]
    )
    if cursor_timeout_sec is not None:
        llm_lines.append(f"    timeout_sec: {cursor_timeout_sec}")
    body = "\n".join(
        [
            "project:",
            "  name: cursor-test",
            "  mode: full-auto",
            "research:",
            "  topic: cursor provider unit test",
            "runtime:",
            "  timezone: UTC",
            "notifications:",
            "  channel: console",
            "knowledge_base:",
            "  backend: markdown",
            f'  root: "{tmp_path / "kb"}"',
            "llm:",
            *llm_lines,
            "experiment:",
            "  mode: simulated",
            "",
        ]
    )
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_cursor_config_roundtrip(tmp_path: Path) -> None:
    cwd = tmp_path / "workdir"
    cfg = RCConfig.load(_write_cursor_config(tmp_path, cwd=cwd), check_paths=False)
    assert cfg.llm.provider == "cursor"
    assert cfg.llm.api_key_env == "CURSOR_API_KEY"
    assert cfg.llm.primary_model == "composer-2.5"
    assert cfg.llm.timeout_sec == 1800
    assert Path(cfg.llm.cursor.cwd) == cwd
    assert cfg.llm.cursor.sandbox_enabled is False
    assert cfg.llm.cursor.timeout_sec == 1800


def test_cursor_timeout_defaults_to_agent_scale(tmp_path: Path) -> None:
    """Omitted llm.timeout_sec must not inherit the 600s chat-completion default."""
    from researchclaw.llm.cursor_sdk_client import CursorSDKClient

    cfg = RCConfig.load(
        _write_cursor_config(tmp_path, cwd=tmp_path / "workdir", timeout_sec=None),
        check_paths=False,
    )
    assert cfg.llm.timeout_sec == 600
    assert cfg.llm.cursor.timeout_sec == 1800
    client = CursorSDKClient.from_rc_config(cfg)
    assert client.config.timeout_sec == 1800


def test_cursor_timeout_honors_explicit_values(tmp_path: Path) -> None:
    from researchclaw.llm.cursor_sdk_client import CursorSDKClient

    cfg = RCConfig.load(
        _write_cursor_config(tmp_path, cwd=tmp_path / "workdir", timeout_sec=900),
        check_paths=False,
    )
    assert CursorSDKClient.from_rc_config(cfg).config.timeout_sec == 900

    cfg_cursor = RCConfig.load(
        _write_cursor_config(
            tmp_path,
            cwd=tmp_path / "workdir",
            timeout_sec=900,
            cursor_timeout_sec=2400,
        ),
        check_paths=False,
    )
    assert CursorSDKClient.from_rc_config(cfg_cursor).config.timeout_sec == 2400


def test_cursor_preflight_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    cfg = RCConfig.load(
        _write_cursor_config(tmp_path, cwd=tmp_path / "workdir"),
        check_paths=False,
    )
    from researchclaw.llm import create_llm_client

    client = create_llm_client(cfg)
    ok, msg = client.preflight()
    assert not ok
    assert "CURSOR_API_KEY" in msg


def test_cursor_chat_times_out_and_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor_sdk = pytest.importorskip("cursor_sdk")

    from researchclaw.llm.cursor_sdk_client import CursorSDKClient, CursorSDKConfig

    cancelled: list[bool] = []
    closed: list[bool] = []
    seen_tools: list[object] = []

    class _Run:
        status = "running"

        def wait(self) -> SimpleNamespace:
            time.sleep(30)
            return SimpleNamespace(status="finished", result="late", error_message=None)

        def cancel(self) -> None:
            cancelled.append(True)
            self.status = "cancelled"

    class _Agent:
        def send(self, message: str) -> _Run:
            assert "ping" in message
            return _Run()

        def close(self) -> None:
            closed.append(True)

    def _create(options: object = None, **kwargs: object) -> _Agent:
        _ = kwargs
        seen_tools.append(getattr(options, "tools", None))
        return _Agent()

    monkeypatch.setattr(cursor_sdk.Agent, "create", _create)
    client = CursorSDKClient(
        CursorSDKConfig(api_key="test-key", cwd=str(tmp_path), timeout_sec=1)
    )
    with pytest.raises(RuntimeError, match="timed out after 1s"):
        client.chat([{"role": "user", "content": "ping"}])
    assert cancelled == [True]
    assert closed == [True]
    assert seen_tools == [[]]
