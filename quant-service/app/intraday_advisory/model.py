"""Bounded DeepSeek and Codex transports for research-only advisory text."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any

from ..agent_paper.model import ModelFailure, ModelResult, find_codex_executable, parse_codex_stream
from ..event_research.model_client import ModelReviewFailure, completion
from ..event_research.model_config import settings as event_model_settings


SYSTEM_PROMPT = """你是 A 股盘中研究助手。输入是只读行情、持仓事实、正式推荐池、纪律事件和确定性信号。
只做风险提示、状态解释和条件式观察建议；禁止下单、禁止声称内外盘代表机构身份、禁止把缺失数据补写成事实。
输出必须是 JSON。使用自然中文，不得输出变量名、JSON 路径、布尔值、空值、UUID、内部任务名。
建议必须引用输入中的具体价格、幅度、成交额或触发线；持仓与推荐候选必须明确分开。
推荐候选尚未持有时，只能说暂停新买、等待确认或候选条件失效，禁止说减仓、卖出、退出。

十分钟快速检查默认静默。只有出现新的、会改变用户关注顺序或盘中应对的事项时，should_notify 才能为 true：
1. 风险等级升高，或指数/板块出现明确加速、反转、共振；
2. 持仓或推荐候选触及关键条件，原有判断明显强化、转弱或失效；
3. 新纪律事件需要用户当下留意。
普通涨跌、与上一轮相同的观察、没有新增动作、仅更换措辞，都必须 should_notify=false。
notification_reason 要用一句人话说明为什么值得打扰用户；静默时留空。

阶段简报输出结构化结果：重要风险优先，大盘一句话，再持仓与推荐池的关键变化。
不要逐只复述无变化对象，不要把第一只持仓的应对当成整个账户的操作指令。
holding_focus 与 recommendation_focus 各最多 2 项，只放真正需要关注的对象；没有就返回空数组。
持仓的 monitoring_focus 表示用户本交易日主动重点关注。优先复核它，但并不强制出卡，也不提高消息频率。
focus_technicals 仅使用已完成的逐分钟收盘价、量额计算；若状态不为 ready 或时点过旧，不得据此判断。
MACDFS、RSI、布林带及量价/VWAP 是相互印证或否定的证据，不是单指标买卖点；minute_kdj、minute_atr、minute_adx 不可用。
真实成交明细与已核实持仓快照是两类证据。broker_evidence 给出快照核实时点时，
可卖数量只在该时点已核实；若同日后续买卖未知，说明"快照后可卖量待核"，不得说"缺少核实可卖数量"。
成交明细缺失不应自动列为优先风险；只有它直接阻止本轮具体判断时才说明。
industry_boards.watched 是推荐候选所涉板块的精确匹配证据，按 observed_at 判断时效；
不得因为板块不在涨跌榜前五就说"板块数据缺失"，也不得用过期快照冒充本时点。
没有可核对的同日真实成交价、数量、费用时，不得给确定 B/S、回补价位或收益承诺。
每项必须包含股票代码、名称、当前状态、可核对证据和条件式应对。market_state 只能是 calm、watch、risk。"""

DELTA_PROMPT = """你是盘中重要变化解释助手。只解释输入scope中的本次变化，禁止全量大盘/持仓/推荐池复述。
condition_changes是代码核对出的原计划事实变化，previous_conditions是此前已通知的条件。
仅解释这些条件更新对跟踪计划的影响。行情异动和纪律触发已有独立卡片，不再跟发复述卡。
数值、区间、多空状态已由代码计算，禁止自行计算、打分或把委比当买卖成交。
只在变化会影响原有观察条件时should_notify=true；仅重复确定性异动摘要、普通波动、没有新解释则false。
delta_items最多3项；代码与名称必须逐字匹配输入。action只写对原条件的影响或下一步确认点，最多100字。
window_seconds 的单位是秒。action禁止复述时间长度、涨跌幅、量能倍数、成交数量等计算指标，代码会展示它们；只可引用输入已有的价格条件（元）。
没有明确输入的价位不编造；不下单，不把候选当持仓，不把内外盘称为主力资金，不从快照推断撤单。
previous_notified为空表示没有已通知的基线，不能编造此前判断。成交方向缺失不得补成零。
有些事件刚已发送确定性证据卡；没有新增条件解释时不需要再发一张。输出JSON。"""

DELTA_SCHEMA = {'type':'object','additionalProperties':False,'properties':{
    'should_notify':{'type':'boolean'},'headline':{'type':'string'},
    'delta_items':{'type':'array','items':{'type':'object','additionalProperties':False,
        'properties':{k:{'type':'string'} for k in ('symbol','name','action')},
        'required':['symbol','name','action']}}},'required':['should_notify','headline','delta_items']}


def _normalized_response(value, payload):
    if payload.get('delta_only'):
        if not isinstance(value,dict):
            raise ModelFailure('json_not_object')
        return {**value,'delta_items':(value.get('delta_items') or [])[:3], 'market_state':'watch'}
    output = _normalize(value)
    broker = payload.get('broker_evidence') or {}
    snapshot_at = broker.get('holding_snapshot_observed_at')
    boundaries: list[str] = []
    if snapshot_at:
        kept = []
        for risk in output['risks']:
            if '可卖数量' in risk and any(word in risk for word in ('缺少', '缺失', '未核实')):
                boundaries.append(f"可卖数量截至 {str(snapshot_at)[11:16]} 的已核实持仓快照；此后变化待核。")
                if any(word in risk for word in ('成交价', '费用', '成交明细')):
                    boundaries.append('本轮未附同日成交明细，不据此推算做 T 收益。')
            else:
                kept.append(risk)
        output['risks'] = kept
    watched = (payload.get('market_context') or {}).get('industry_boards', {}).get('watched') or []
    fresh_labels: set[str] = set()
    for item in watched:
        try:
            age = datetime.fromisoformat(str(payload['as_of'])) - datetime.fromisoformat(str(item['observed_at']))
        except (KeyError, TypeError, ValueError):
            continue
        if timedelta(0) <= age <= timedelta(minutes=3):
            fresh_labels.add(str(item.get('label')))
    if fresh_labels:
        output['risks'] = [risk for risk in output['risks'] if not (
            '板块' in risk and any(word in risk for word in ('缺少', '缺失', '没有'))
            and any(label in risk for label in fresh_labels))]
    output['data_boundaries'] = list(dict.fromkeys(boundaries))
    return output


FOCUS_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "symbol": {"type": "string"},
        "name": {"type": "string"},
        "status": {"type": "string"},
        "evidence": {"type": "string"},
        "action": {"type": "string"},
    },
    "required": ["symbol", "name", "status", "evidence", "action"],
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "market_state": {"type": "string", "enum": ["calm", "watch", "risk"]},
        "should_notify": {"type": "boolean"},
        "notification_reason": {"type": "string"},
        "headline": {"type": "string"},
        "market_summary": {"type": "string"},
        "holding_focus": {"type": "array", "items": FOCUS_ITEM_SCHEMA},
        "recommendation_focus": {"type": "array", "items": FOCUS_ITEM_SCHEMA},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "market_state", "should_notify", "notification_reason", "headline", "market_summary",
        "holding_focus", "recommendation_focus", "risks",
    ],
}


def _focus_items(value: Any) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        output.append({
            "symbol": str(item.get("symbol") or "")[:20],
            "name": str(item.get("name") or "")[:24],
            "status": str(item.get("status") or "")[:60],
            "evidence": str(item.get("evidence") or "")[:160],
            "action": str(item.get("action") or "")[:120],
        })
        if len(output) >= 2:
            break
    return output


def _normalize(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelFailure("json_not_object")
    state = str(value.get("market_state") or "watch")
    if state not in {"calm", "watch", "risk"}:
        state = "watch"
    holdings = _focus_items(value.get("holding_focus"))
    recommendations = _focus_items(value.get("recommendation_focus"))
    # Compatibility with already-persisted morning outputs.  New model calls
    # always use the structured schema above, while the renderer can still
    # replay the old evidence without pretending to know its portfolio role.
    legacy_attention = [str(item)[:40] for item in (value.get("attention_symbols") or [])[:3]]
    headline = str(value.get("headline") or value.get("summary") or "")[:100]
    market_summary = str(value.get("market_summary") or value.get("summary") or "")[:300]
    output = {
        "market_state": state,
        "should_notify": value.get("should_notify") is True,
        "notification_reason": str(value.get("notification_reason") or "")[:160],
        "headline": headline,
        "market_summary": market_summary,
        "holding_focus": holdings,
        "recommendation_focus": recommendations,
        "risks": [str(item)[:140] for item in (value.get("risks") or [])[:3]],
        "attention_symbols": [
            *(item["symbol"] for item in holdings if item["symbol"]),
            *(item["symbol"] for item in recommendations if item["symbol"]),
            *legacy_attention,
        ][:6],
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
        schema = json.dumps(DELTA_SCHEMA if payload.get('delta_only') else OUTPUT_SCHEMA, ensure_ascii=False)
        prompt = DELTA_PROMPT if payload.get('delta_only') else SYSTEM_PROMPT
        messages = [{"role": "system", "content": prompt + "\n输出 schema：" + schema},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]
        try:
            value, usage, returned_model = completion(messages, self.connection, started + self.timeout_seconds)
        except ModelReviewFailure as error:
            raise ModelFailure("provider_error", json.dumps(error.diagnostics, ensure_ascii=False)) from error
        return ModelResult(_normalized_response(value,payload), returned_model or self.connection[2],
                           int((time.monotonic() - started) * 1000), {"usage": usage})

    async def analyze(self, payload: dict[str, Any]) -> ModelResult:
        return await asyncio.to_thread(self._run, payload)


class CodexAdvisoryModel:
    provider = "codex"

    def __init__(self, *, model: str | None = None, reasoning_effort: str | None = None,
                 timeout_seconds: int = 240) -> None:
        self.model_id = model or os.getenv("INTRADAY_ADVISORY_CODEX_MODEL") or "gpt-6-sol"
        self.reasoning_effort = reasoning_effort or os.getenv("INTRADAY_ADVISORY_CODEX_REASONING") or "high"
        self.binary_pinned = bool(os.getenv("INTRADAY_ADVISORY_CODEX_BIN"))
        self.binary = os.getenv("INTRADAY_ADVISORY_CODEX_BIN") or find_codex_executable(
            {key: value for key, value in os.environ.items() if key != "AGENT_PAPER_CODEX_BIN"})
        self.timeout_seconds = timeout_seconds

    def _run(self, payload: dict[str, Any]) -> ModelResult:
        started = time.monotonic()
        prompt = (DELTA_PROMPT if payload.get('delta_only') else SYSTEM_PROMPT) + "\n当前盘中证据：\n" + json.dumps(payload, ensure_ascii=False, default=str)
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        env.pop("CODEX_API_KEY", None)
        with tempfile.TemporaryDirectory(prefix="intraday-advisory-") as workdir:
            schema_path = Path(workdir, "schema.json")
            schema_path.write_text(json.dumps(DELTA_SCHEMA if payload.get('delta_only') else OUTPUT_SCHEMA, ensure_ascii=False), encoding="utf-8")
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
            except FileNotFoundError as error:
                if self.binary_pinned:
                    raise ModelFailure("cli_unavailable", type(error).__name__) from error
                refreshed = find_codex_executable(
                    {key: value for key, value in os.environ.items() if key != "AGENT_PAPER_CODEX_BIN"})
                if refreshed == self.binary:
                    raise ModelFailure("cli_unavailable", type(error).__name__) from error
                self.binary = refreshed
                command[0] = refreshed
                try:
                    completed = subprocess.run(
                        command, input=prompt, capture_output=True, text=True, encoding="utf-8",
                        timeout=self.timeout_seconds, cwd=workdir, env=env,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except subprocess.TimeoutExpired as retry_error:
                    raise ModelFailure("timeout", f"{self.timeout_seconds}s") from retry_error
                except OSError as retry_error:
                    raise ModelFailure("cli_unavailable", type(retry_error).__name__) from retry_error
            except OSError as error:
                raise ModelFailure("cli_unavailable", type(error).__name__) from error
        output, usage, transcript = parse_codex_stream(completed.stdout)
        if completed.returncode != 0:
            raise ModelFailure("codex_failed", completed.stderr[-400:], transcript)
        return ModelResult(_normalized_response(output,payload), f"{self.model_id}/{self.reasoning_effort}",
                           int((time.monotonic() - started) * 1000), usage, transcript)

    async def analyze(self, payload: dict[str, Any]) -> ModelResult:
        return await asyncio.to_thread(self._run, payload)


__all__ = ["CodexAdvisoryModel", "DeepSeekAdvisoryModel", "OUTPUT_SCHEMA", "SYSTEM_PROMPT"]
