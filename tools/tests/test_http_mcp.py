# SPDX-License-Identifier: Apache-2.0
"""HTTP MCP discovery and later skill dispatch, using the real MCP SDK."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import tools
from core.skill.execution.adapter import SkillToolAdapter
from core.skill.schema import ErrorType, Skill
from middleware.config import g_config
from middleware.config.mcp_config_manager import McpConfigManager
from shared.hooks import HookExecutor
from shared.hooks.types import HookResult
from shared.schema import SkillConfig
from tools.mcp.client import MCPToolLoader
from tools.registry import ToolExecutionError, ToolTimeoutError


SOURCE_URL = "https://docs.python.org/3/library/pathlib.html"
ARGUMENTS = {
    "objective": "Find official public documentation for pathlib.",
    "search_queries": ["Python pathlib official documentation"],
}


@pytest.fixture
def config():
    data = copy.deepcopy(McpConfigManager().load_template())
    data["mcp"]["parallel"]["enabled"] = True
    return data


@pytest.fixture
def server(monkeypatch):
    """Mock HTTP only; exercise SDK initialization, schemas and calls normally."""
    state = {"requests": [], "clients": [], "result": {
        "content": [{"type": "text", "text": json.dumps({"results": [{
            "title": "pathlib — Object-oriented filesystem paths",
            "url": SOURCE_URL,
            "excerpts": ["This module offers classes representing filesystem paths."],
        }]})}],
        "isError": False,
    }}

    async def respond(request):
        state["requests"].append(request)
        if state.get("unavailable"):
            return httpx.Response(503, text="Service unavailable")
        message = json.loads(request.content)
        method = message["method"]
        if method.startswith("notifications/"):
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-search", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "web_search", "description": "Search public documentation.",
                "inputSchema": {"type": "object", "properties": {
                    "objective": {"type": "string"},
                    "search_queries": {"type": "array", "items": {"type": "string"}},
                }, "required": ["objective", "search_queries"]},
            }]}
        elif method == "tools/call":
            assert message["params"]["name"] == "web_search"
            assert message["params"]["arguments"] == ARGUMENTS
            if state.get("wait"):
                await asyncio.Event().wait()
            result = state["result"]
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    client_class = httpx.AsyncClient

    class RecordingClient(client_class):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)
            state["clients"].append(self)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)
    return state


def skill_adapter(tmp_path, hooks=None):
    adapter = SkillToolAdapter(SkillConfig(
        skills_dir=tmp_path, builtin_skills_dir=tmp_path, workspace_dir=tmp_path,
    ), hook_executor=hooks)
    adapter.set_context(Skill(name="web-search", description="Public web research"), tmp_path)
    return adapter


@pytest.mark.asyncio
async def test_template_is_opt_in_and_tavily_stays_available(server, monkeypatch):
    template = McpConfigManager().load_template()
    McpConfigManager()._validate(template)
    assert template["mcp"]["parallel"]["enabled"] is False
    g_config.load()
    registry = await tools.bootstrap(mcp_config=template)
    assert registry.is_registered("search_web")
    assert not registry.is_registered("parallel_web_search")
    monkeypatch.setattr(g_config, "get_env", lambda: {})
    assert "Missing TAVILY_API_KEY" in await registry.execute("search_web", {"query": "public docs"})
    assert server["requests"] == []


@pytest.mark.asyncio
async def test_default_tavily_dispatch(monkeypatch):
    from tools.atomics.web import search_web

    monkeypatch.setattr(g_config, "get_env", lambda: {"TAVILY_API_KEY": "test-placeholder"})

    def post(url, **kwargs):
        assert url == "https://api.tavily.com/search"
        assert kwargs["headers"] == {"Authorization": "Bearer test-placeholder"}
        assert kwargs["timeout"] == 20.0
        assert kwargs["json"] == {
            "query": "public docs", "search_depth": "basic", "max_results": 3,
            "include_raw_content": False,
        }
        return httpx.Response(200, request=httpx.Request("POST", url), json={"results": [{
            "title": "Official documentation", "url": SOURCE_URL, "content": "Public evidence",
        }]})

    monkeypatch.setattr(httpx, "post", post)
    assert SOURCE_URL in await search_web("public docs")


@pytest.mark.asyncio
async def test_enabled_http_tool_survives_discovery_and_keeps_source_urls(config, server, tmp_path):
    g_config.load()
    registry = await tools.bootstrap(mcp_config=config)
    assert all(c.is_closed for c in server["clients"])
    schema = registry.get_schema("parallel_web_search")["function"]["parameters"]
    assert schema["required"] == ["objective", "search_queries"]
    assert registry.is_registered("search_web")
    for _ in range(2):
        observation, error = await skill_adapter(tmp_path).execute("parallel_web_search", ARGUMENTS)
        assert error is None
        assert SOURCE_URL in observation
        assert "filesystem paths" in observation
    assert len(server["clients"]) == 3  # discovery, then a fresh session for each call
    assert all(c.is_closed for c in server["clients"])
    assert all(r.headers["User-Agent"] == "Memento-S/0.4.0" for r in server["requests"])
    assert all("Authorization" not in r.headers for r in server["requests"])
    assert server["requests"][0].extensions["timeout"]["read"] == 40


@pytest.mark.asyncio
async def test_global_disable_does_not_connect(config, server):
    config["enabled"] = False
    await MCPToolLoader(tools.get_registry()).load_from_config(config)
    assert server["requests"] == []


@pytest.mark.asyncio
async def test_policy_hook_can_block_search_before_network(config, server, tmp_path):
    await MCPToolLoader(tools.get_registry()).load_from_config(config)
    count = len(server["requests"])
    hooks = AsyncMock(spec=HookExecutor)
    hooks.execute.return_value = HookResult(allowed=False, reason="Public query not authorized")
    observation, error = await skill_adapter(tmp_path, hooks).execute("parallel_web_search", ARGUMENTS)
    assert error["error_type"] == ErrorType.POLICY_BLOCKED
    assert "not authorized" in observation
    assert len(server["requests"]) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("result, expected", [
    ({"content": []}, "(no output)"),
    ({"content": [{"type": "text", "text": "No results found."}]}, "No results found."),
    ({"content": [{"type": "text", "text": "Rate limit exceeded"}], "isError": True},
     "ERR: Rate limit exceeded"),
])
async def test_empty_and_error_results(config, server, result, expected):
    await MCPToolLoader(tools.get_registry()).load_from_config(config)
    server["result"] = result
    assert await tools.execute_tool("parallel_web_search", ARGUMENTS) == expected
    assert all(c.is_closed for c in server["clients"])


@pytest.mark.asyncio
async def test_unavailable_discovery_is_skipped(config, server, capsys):
    server["unavailable"] = True
    await MCPToolLoader(tools.get_registry()).load_from_config(config)
    assert not tools.is_tool_registered("parallel_web_search")
    assert "Skipping server 'parallel'" in capsys.readouterr().err
    assert all(c.is_closed for c in server["clients"])


@pytest.mark.asyncio
async def test_unavailable_call_fails_without_provider_fallback(config, server):
    await MCPToolLoader(tools.get_registry()).load_from_config(config)
    server["unavailable"] = True
    with pytest.raises(ToolExecutionError):
        await tools.execute_tool("parallel_web_search", ARGUMENTS)
    assert all(c.is_closed for c in server["clients"])
    assert all(r.url.host == "mcp.parallel.ai" for r in server["requests"])


@pytest.mark.asyncio
async def test_registry_timeout_closes_http_client(config, server):
    registry = tools.get_registry()
    await MCPToolLoader(registry).load_from_config(config)
    registry.get("parallel_web_search").timeout_ms = 100
    server["wait"] = True
    with pytest.raises(ToolTimeoutError):
        await registry.execute("parallel_web_search", ARGUMENTS)
    assert all(c.is_closed for c in server["clients"])


async def run_research_skill(tmp_path, monkeypatch):
    """Use a deterministic LLM double; keep agent, tools and hooks real."""
    from core.skill.execution.agent import SkillAgent
    from core.skill.execution import hooks
    from middleware.llm.schema import LLMResponse, ToolCall

    monkeypatch.setattr(hooks.registry, "_global_skill_agent_hook_executor", None)
    monkeypatch.setattr(hooks.registry, "_global_workspace_root", None)
    monkeypatch.setattr(hooks.registry, "_global_policy_manager", None)
    hooks.register_skill_agent_hooks(workspace_root=tmp_path)
    calls = []
    evidence_url = None
    note = tmp_path / "research.md"

    class ResearchLLM:
        context_window = 128000
        max_tokens = 4096

        async def async_chat(self, *, messages, tools, tool_choice):
            nonlocal evidence_url
            calls.append(messages)
            names = {t["function"]["name"] for t in tools}
            assert "parallel_web_search" in names
            if len(calls) == 1:
                return LLMResponse(tool_calls=[ToolCall(
                    id="search", name="parallel_web_search", arguments=ARGUMENTS,
                )])
            if len(calls) == 2:
                result = next(m for m in messages if m.get("role") == "tool")
                evidence = json.loads(result["content"])
                evidence_url = next(r["url"] for r in evidence["results"]
                                    if r["url"].startswith("https://docs.python.org/"))
                return LLMResponse(tool_calls=[ToolCall(
                    id="note", name="file_create", arguments={
                        "path": str(note), "content": f"Source: [Python documentation]({evidence_url})",
                    },
                )])
            return LLMResponse(content=f"Research saved. [Python documentation]({evidence_url})")

    root = Path(__file__).resolve().parents[2]
    skill = Skill(name="web-search", description="Public documentation research",
                  content=(root / "builtin/skills/web-search/SKILL.md").read_text(),
                  allowed_tools=["parallel_web_search", "file_create"])
    agent = SkillAgent(config=SkillConfig(
        skills_dir=tmp_path, builtin_skills_dir=root / "builtin/skills", workspace_dir=tmp_path,
    ), llm=ResearchLLM())
    outcome, _ = await agent.run(skill, "Find public pathlib documentation using parallel_web_search, cite sources and save research.md.",
                                 None, tmp_path, "public-research-test", None, max_turns=3)
    assert outcome.success, outcome
    assert evidence_url and evidence_url in str(outcome.result)
    assert evidence_url in note.read_text()
    return evidence_url


@pytest.mark.asyncio
async def test_skill_run_carries_original_evidence_into_cited_output(config, server, tmp_path, monkeypatch):
    g_config.load()
    await tools.bootstrap(mcp_config=config)
    assert await run_research_skill(tmp_path, monkeypatch) == SOURCE_URL


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("MEMENTO_TEST_LIVE_PARALLEL") != "1", reason="explicit live opt-in required")
async def test_live_keyless_search_through_skill_adapter(config, tmp_path, monkeypatch):
    requests = []
    from tools.mcp import client as mcp_client
    build_client = mcp_client._build_http_client

    def recording_client(settings):
        client = build_client(settings)

        async def record(request):
            requests.append((str(request.url), request.headers.get("User-Agent"),
                             "Authorization" in request.headers))

        client.event_hooks["request"].append(record)
        return client

    monkeypatch.setattr(mcp_client, "_build_http_client", recording_client)
    g_config.load()
    await tools.bootstrap(mcp_config=config)
    observation, error = await skill_adapter(tmp_path).execute("parallel_web_search", ARGUMENTS)
    assert error is None, observation
    assert "https://docs.python.org/" in observation
    assert requests and all(agent == "Memento-S/0.4.0" and not auth for _, agent, auth in requests)
    source = await run_research_skill(tmp_path, monkeypatch)
    print("Live keyless source URL:", source, "User-Agent: Memento-S/0.4.0; no Authorization header")
