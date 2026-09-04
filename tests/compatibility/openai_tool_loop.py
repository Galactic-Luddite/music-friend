"""Non-shipping fixture for one OpenAI-compatible MCP tool-call bridge turn."""

from __future__ import annotations

import json

from mcp.client import Client


async def run_synthetic_tool_loop(
    server: object,
    listed_tools: object,
    tool_call: dict[str, str],
) -> dict[str, object]:
    """Translate one synthetic function call through a local MCP server.

    This intentionally does not contact a model endpoint. It validates only the JSON
    translation a host can use with an OpenAI-compatible tool-call API.
    """
    tools = _openai_tools(listed_tools)
    name = tool_call["name"]
    arguments = json.loads(tool_call["arguments"])
    if type(arguments) is not dict:
        raise ValueError("synthetic tool arguments must be an object")
    async with Client(server) as client:  # type: ignore[arg-type]
        result = await client.call_tool(name, arguments)
    return {"tools": tools, "tool_result": result.structured_content}


def _openai_tools(listed_tools: object) -> list[dict[str, object]]:
    values = getattr(listed_tools, "tools", listed_tools)
    if not isinstance(values, (list, tuple)):
        raise ValueError("listed tools are invalid")
    converted: list[dict[str, object]] = []
    for tool in values:
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", None)
        schema = getattr(tool, "input_schema", None)
        if (
            not isinstance(name, str)
            or not isinstance(description, str)
            or not isinstance(schema, dict)
        ):
            raise ValueError("listed tool is invalid")
        converted.append(
            {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": schema},
            }
        )
    return converted
