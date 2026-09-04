# MCP clients

Use the CLI to complete setup before connecting an MCP client. The local stdio server is:

```bash
music-friend-mcp
```

MCP handles local music, watchlist, and inbox requests. It does not accept or manage credentials,
schedules, imports, restores, backups, or full-data deletion. The encrypted-vault fallback is
interactive CLI only; the current stdio MCP server requires an approved native credential store.

## Interface boundary

MCP schemas are the universal integration interface. A compatible client discovers the advertised
tools and their schemas from the local server; it does not need provider-specific instructions.

## Optional runtime skill

The canonical optional runtime skill remains at `skills/music-friend/SKILL.md` in a source
checkout and is packaged unchanged in the wheel. It can be installed from the wheel for Codex,
Claude, or a custom skills directory:

```bash
music-friend skill install --client codex
music-friend skill install --client claude
music-friend skill install --target SKILLS_DIRECTORY
```

See the [install guide](install.md#optional-agent-skill) for destinations, replacement behavior,
and local safety boundaries. MCP schemas remain sufficient for clients that do not load skills;
the skill is optional convenience guidance.

## Contributor guidance

Repository contributor guidance applies only inside a source checkout. It describes source layout,
validation, synthetic-data requirements, and provider-neutral architecture constraints; it is not
a runtime discovery mechanism.

## Tool surface

Music Friend exposes exactly these nine tools:

- `music_status`
- `refresh_music`
- `search_catalog`
- `list_watchlist`
- `update_watchlist`
- `list_inbox`
- `update_inbox_item`
- `explain_inbox_item`
- `summarize_listening_history`

The server uses stdio. The tools work with local identifiers and bounded arguments rather than raw
provider identifiers.

## Codex

Add this local server configuration to the appropriate Codex configuration file:

```toml
[mcp_servers.music-friend]
command = "music-friend-mcp"
```

## Claude Code

Add the equivalent server entry to the Claude Code MCP configuration:

```json
{
  "mcpServers": {
    "music-friend": {
      "command": "music-friend-mcp",
      "args": []
    }
  }
}
```

## Other stdio clients

A compatible client starts `music-friend-mcp`, completes normal MCP tool discovery, and calls only
the advertised tools.

## vLLM and Ollama

vLLM and Ollama can be used through a client that supports an OpenAI-compatible function-call loop.
Compatibility depends on that client and its configuration.

## Listening-history evidence

`summarize_listening_history` answers bounded historical questions from locally imported Spotify
music plays. Supply nullable UTC `since` and `until` timestamps and a ranking limit from 1 through
50. The result identifies its evidence boundary, covered dates, play time, brief/skipped counts,
and top artists and tracks. Imported plays remain separate from preferences and watchlist affinity.
