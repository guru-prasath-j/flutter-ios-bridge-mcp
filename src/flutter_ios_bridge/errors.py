try:  # mcp >= 2: only ToolError messages reach the client
    from mcp.server.mcpserver.exceptions import ToolError as _Base
except ImportError:  # mcp 1.x
    try:
        from mcp.server.fastmcp.exceptions import ToolError as _Base
    except ImportError:  # pragma: no cover
        _Base = RuntimeError


class BridgeError(_Base):
    """An error with a message meant to be shown to the user/agent as-is."""
