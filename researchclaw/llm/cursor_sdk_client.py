"""Cursor SDK LLM client — uses CURSOR_API_KEY + Agent.prompt (not OpenAI HTTP)."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from researchclaw.llm.client import LLMResponse

logger = logging.getLogger(__name__)


@dataclass
class CursorSDKConfig:
    model: str = "composer-2.5"
    api_key: str = ""
    api_key_env: str = "CURSOR_API_KEY"
    cwd: str = "."
    sandbox_enabled: bool = False
    timeout_sec: int = 1800


class CursorSDKClient:
    """Drop-in LLM client backed by ``cursor_sdk.Agent.prompt``."""

    def __init__(self, config: CursorSDKConfig) -> None:
        self.config = config
        self._resolved_key = config.api_key or os.environ.get(config.api_key_env, "")

    @classmethod
    def from_rc_config(cls, rc_config: Any) -> CursorSDKClient:
        llm = rc_config.llm
        cursor = getattr(llm, "cursor", None)
        cwd = getattr(cursor, "cwd", ".") if cursor else "."
        sandbox = getattr(cursor, "sandbox_enabled", False) if cursor else False
        timeout = getattr(cursor, "timeout_sec", None) if cursor else None
        return cls(
            CursorSDKConfig(
                model=llm.primary_model or "composer-2.5",
                api_key=llm.api_key or "",
                api_key_env=llm.api_key_env or "CURSOR_API_KEY",
                cwd=cwd,
                sandbox_enabled=sandbox,
                timeout_sec=int(timeout or 1800),
            )
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
        system: str | None = None,
        strip_thinking: bool = True,
    ) -> LLMResponse:
        del max_tokens, temperature  # Agent manages generation; json_mode used below
        from researchclaw.llm.acp_client import ACPClient

        prompt_text = ACPClient._messages_to_prompt(messages, system=system)
        if json_mode:
            prompt_text += "\n\nRespond with valid JSON only."

        content = self._complete(prompt_text, model=model or self.config.model)
        if strip_thinking:
            from researchclaw.utils.thinking_tags import strip_thinking_tags

            content = strip_thinking_tags(content)
        return LLMResponse(
            content=content,
            model=model or self.config.model,
            finish_reason="stop",
        )

    def preflight(self) -> tuple[bool, str]:
        if not self._resolved_key:
            return False, (
                f"Missing API key: set {self.config.api_key_env} or llm.api_key in config"
            )
        try:
            import cursor_sdk  # noqa: F401
        except ImportError:
            return False, (
                "cursor-sdk not installed. Run: pip install cursor-sdk  "
                "(or pip install -e \".[cursor]\" in AutoResearchClaw)"
            )
        return True, f"OK - Cursor SDK ({self.config.model}, cwd={self._abs_cwd()})"

    def _abs_cwd(self) -> str:
        return str(Path(self.config.cwd).expanduser().resolve())

    def _wait_for_run(self, run: Any) -> Any:
        """Block until ``run`` finishes, cancelling it after ``timeout_sec``."""
        timeout = int(self.config.timeout_sec or 0)
        if timeout <= 0:
            return run.wait()

        holder: dict[str, Any] = {}

        def _target() -> None:
            try:
                holder["result"] = run.wait()
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
                holder["error"] = exc

        thread = threading.Thread(target=_target, name="cursor-sdk-wait", daemon=True)
        thread.start()
        thread.join(timeout)
        if not thread.is_alive():
            if "error" in holder:
                raise holder["error"]
            return holder["result"]
        try:
            run.cancel()
        except Exception:
            logger.warning("Cursor run cancel failed after timeout", exc_info=True)
        raise RuntimeError(f"Cursor prompt timed out after {timeout}s")

    def _complete(self, prompt: str, *, model: str) -> str:
        from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions, SandboxOptions

        if not self._resolved_key:
            raise RuntimeError(f"{self.config.api_key_env} is not set")

        cwd = self._abs_cwd()
        Path(cwd).mkdir(parents=True, exist_ok=True)

        agent = Agent.create(
            AgentOptions(
                model=model,
                api_key=self._resolved_key,
                local=LocalAgentOptions(
                    cwd=cwd,
                    sandbox_options=SandboxOptions(
                        enabled=bool(self.config.sandbox_enabled)
                    ),
                ),
                tools=[],
            )
        )
        try:
            try:
                result = self._wait_for_run(agent.send(prompt))
            except CursorAgentError as err:
                raise RuntimeError(f"Cursor agent failed: {err.message}") from err
        finally:
            agent.close()

        status = getattr(result, "status", None)
        if status != "finished":
            message = getattr(result, "error_message", None) or status or "unknown"
            raise RuntimeError(f"Cursor run {status}: {message}")
        text = getattr(result, "result", None) or ""
        if not text.strip():
            raise RuntimeError("Cursor run finished with empty text")
        return text
