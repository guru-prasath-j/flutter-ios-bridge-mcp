"""Work with both the mcp 1.x (FastMCP) and 2.x (MCPServer) Python SDKs."""

import functools
import inspect

import anyio

from .errors import BridgeError

try:  # mcp >= 2
    from mcp.server.mcpserver import Image, MCPServer as Server  # type: ignore
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server, Image  # type: ignore


def threaded(fn):
    """Run a blocking tool in a worker thread so the stdio loop stays responsive."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except BridgeError:
            raise
        except Exception as e:  # surface real causes (network, file not found...) to the agent
            raise BridgeError(f"{type(e).__name__}: {e}") from e

    wrapper.__signature__ = inspect.signature(fn)
    return wrapper


__all__ = ["Server", "Image", "threaded"]
