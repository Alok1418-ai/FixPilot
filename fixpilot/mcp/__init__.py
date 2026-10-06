"""MCP integration: FixPilot as a tool server, and as a tool client.

FixPilot's own skills are in-process and deterministic; MCP is the seam that
lets the same agent be driven by an editor, or pull capabilities from a tool
server the team already runs, without either side depending on the other's
framework.
"""

from .client import MCPClient, MCPError, MCPToolError
from .server import PROTOCOL_VERSION, SERVER_INFO, TOOL_SPECS, MCPServer

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPToolError",
    "MCPServer",
    "PROTOCOL_VERSION",
    "SERVER_INFO",
    "TOOL_SPECS",
]
