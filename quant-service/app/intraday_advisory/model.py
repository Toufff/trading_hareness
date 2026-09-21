"""Bounded DeepSeek and Codex transports for research-only advisory text."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any

from ..agent_paper.model import ModelFailure, ModelResult, parse_codex_stream
from ..event_research.model_client import ModelReviewFailure, completion
from ..event_research.model_config import settings as event_model_settings


SYSTEM_PROMPT = """你是 A 股盘中研究助手。输入是只读行情、持仓事实、正式推荐池、纪律事件和确定性信号。
只做风险提示、状态解释和条件式观察建议；禁止下单、禁止声称主动买卖量代表机构身份、禁止把缺失数据补写成事实。
输出必须是 JSON。使用自然中文，不得输出变量名、JSON 路径、布尔值、空值、UUID、内部任务名。
建议必须引用输入中的具体价格、幅度、成交额或触发线；持仓与推荐候选必须明确区分。
推荐候选尚未持有时，只能说暂停新买、等待确认或候选条件失效，禁止说减仓、卖出、退出。
十分钟报告只写相较上一轮的变化，最多三个重点对象；完整报告也只保留最重要的三条建议和三条风险。
market_state 只能是 calm、watch、risk；attention_symbols 最多 3 个；guidance 最多 3 条。"""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "market_state": {"type": "string", "enum": ["calm", "watch", "risk"]},
        "summary": {"type": "string"},
        "attention_symbols": {"type": "array", "items": {"type": "string"}},
        "guidance": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["market_state", "summary", "attention_symbols", "guidance", "risks"],
}


def _normalize(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelFailure("json_not_object")
    state = str(value.get("market_state") or "watch")
    if state not in {"calm", "watch", "risk"}:
        state = "watch"
    output = {
        "market_state": state,
        "summary": str(value.get("summary") or "")[:240],
        "attention_symbols": [str(item)[:40] for item in (value.get("attention_symbols") or [])[:3]],
        "guidance": [str(item)[:140] for item in (value.get("guidance") or [])[:3]],
        "risks": [str(item)[:140] for item in (value.get("risks") or [])[:3]],
    }
    # Do not let harmless wording variation create a ten-minute notification.
    # A push-worthy state change is a changed risk regime or attention set;
    # every full output is still persisted for audit.
    canonical = {"market_state": output["market_state"],
                 "attention_symbols": sorted(set(output["attention_symbols"]))}
    import hashlib
    output["state_fingerprint"] = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return output


class DeepSeekAdvisoryModel:
    provider = "deepseek"

    def __init__(self, timeout_seconds: int = 180) -> None:
        self.connection = event_model_settings()
        self.timeout_seconds = timeout_seconds

    def _run(self, payload: dict[str, Any]) -> ModelResult:
        started = time.monotonic()
        schema = json.dumps(OUTPUT_SCHEMA, ensure_ascii=False)
        messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n输出 schema：" + schema},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]
        try:
            value, usage, returned_model = completion(messages, self.connection, started + self.timeout_seconds)
        except ModelReviewFailure as error:
            raise ModelFailure("provider_error", json.dumps(error.diagnostics, ensure_ascii=False)) from error
        return ModelResult(_normalize(value), returned_model or self.connection[2],
                           int((time.monotonic() - started) * 1000), {"usage": usage})

    async def analyze(self, payload: dict[str, Any]) -> ModelResult:
        return await asyncio.to_thread(self._run, payload)


class CodexAdvisoryModel:
    provider = "codex"

    def __init__(self, *, model: str | None = None, reasoning_effort: str | None = None,
                 timeout_seconds: int = 240) -> None:
        self.model_id = model or os.getenv("INTRADAY_ADVISORY_CODEX_MODEL") or "gpt-5.6-sol"
        self.reasoning_effort = reasoning_effort or os.getenv("INTRADAY_ADVISORY_CODEX_REASONING") or "medium"
        self.binary = os.getenv("INTRADAY_ADVISORY_CODEX_BIN") or "codex"
        self.timeout_seconds = timeout_seconds

    def _run(self, payload: dict[str, Any]) -> ModelResult:
        started = time.monotonic()
        prompt = SYSTEM_PROMPT + "\n当前盘中证据：\n" + json.dumps(payload, ensure_ascii=False, default=str)
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env.pop("CODEX_API_KEY", None)
        with tempfile.TemporaryDirectory(prefix="intraday-advisory-") as workdir:
            schema_path = Path(workdir, "schema.json")
            schema_path.write_text(json.dumps(OUTPUT_SCHEMA, ensure_ascii=False), encoding="utf-8")
            command = [self.binary, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                       "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
                       "--model", self.model_id, "--config", f'model_reasoning_effort="{self.reasoning_effort}"',
                       "--output-schema", str(schema_path), "-"]
            try:
                completed = subprocess.run(
                    command, input=prompt, capture_output=True, text=True, encoding="utf-8",
                    timeout=self.timeout_seconds, cwd=workdir, env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as error:
                raise ModelFailure("timeout", f"{self.timeout_seconds}s") from error
            except OSError as error:
                raise ModelFailure("cli_unavailable", type(error).__name__) from error
        output, usage, transcript = parse_codex_stream(completed.stdout)
        if completed.returncode != 0:
            raise ModelFailure("codex_failed", completed.stderr[-400:], transcript)
        return ModelResult(_normalize(output), f"{self.model_id}/{self.reasoning_effort}",
                           int((time.monotonic() - started) * 1000), usage, transcript)

    async def analyze(self, payload: dict[str, Any]) -> ModelResult:
        return await asyncio.to_thread(self._run, payload)


__all__ = ["CodexAdvisoryModel", "DeepSeekAdvisoryModel", "OUTPUT_SCHEMA", "SYSTEM_PROMPT"]
