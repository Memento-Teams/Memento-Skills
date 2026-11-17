# Optional public web research with Parallel MCP

The built-in `web-search` skill uses Tavily and requires `TAVILY_API_KEY`.
For public documentation or release-note research without a web API key, enable
Parallel in Memento-S's existing MCP configuration. Parallel is disabled by
default. The built-in search tool and its settings are unchanged.

After the first launch, edit `~/memento_s/mcp.json` (or `mcp.json` under your
`MEMENTO_HOME`). Set `mcp.parallel.enabled` to `true`. If your file predates the
Parallel entry, merge this entry into its existing `mcp` object:

```json
{
  "enabled": true,
  "mcp": {
    "parallel": {
      "transport": "streamable_http",
      "url": "https://mcp.parallel.ai/mcp",
      "headers": {
        "User-Agent": "Memento-S/0.4.0"
      },
      "timeout": 20000,
      "enabled": true
    }
  }
}
```

Preserve other server entries, then restart Memento-S to load the tools. A web
API key is optional for Parallel search at the service's unauthenticated rate
limits. Your usual agent configuration is still required. See
[Parallel MCP setup](https://docs.parallel.ai/integrations/mcp/quickstart) for
current limits and optional authentication.

Select Parallel explicitly in an ordinary agent request:

```bash
memento agent -m "Use parallel_web_search to find the official Python documentation about pathlib. Search only public documentation and cite the original source URLs in your answer."
```

The MCP registry prefixes server tool names, so Parallel's `web_search` becomes
`parallel_web_search`. The agent and skill executor receive its discovered
argument schema and the original result text, including source URLs. Skills
with an `allowed_tools` restriction must allow `parallel_web_search` to use it.
The built-in Tavily script continues to use Tavily; enabling MCP does not reroute
that script.

Only send public search queries that the user has authorized. Queries are sent
to Parallel; keep credentials, private documents, and account data out of them.
Treat retrieved content as source material and cite its original URLs. Existing
tool policy hooks and registry timeouts still apply. Service errors are returned
as tool errors, and an unavailable discovery endpoint is skipped with a message
on stderr. If no evidence is returned, report that instead of inventing sources.
There is no automatic provider fallback.

Set `mcp.parallel.enabled` back to `false` and restart to disable the integration.

This integration was contributed by Parallel.
