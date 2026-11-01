# SPDX-License-Identifier: Apache-2.0
"""The plugin runs as an MCP server over stdio, as the core's plugin host starts it."""

import sys

import anyio
from horae_sdk.testing import read_result
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from horae_github import COMMAND, NAME, SECRET

TOOLS = {f"{NAME}.review_requests", f"{NAME}.assigned", f"{NAME}.mentions"}


async def serve_and_call() -> tuple[set[str], str | None]:
    # `COMMAND` without its interpreter: run the module with the test's own Python. The
    # environment has no token, so the call fails before any network access.
    server = StdioServerParameters(command=sys.executable, args=COMMAND[1:], env={"TZ": "UTC"})
    async with Client(server, read_timeout_seconds=30) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        replayed = read_result(await client.call_tool(f"{NAME}.mentions", {}))
    return tools, replayed.error


def test_serves_over_stdio() -> None:
    tools, error = anyio.run(serve_and_call)
    assert tools == TOOLS
    assert error == f"github: {SECRET} is not set"
