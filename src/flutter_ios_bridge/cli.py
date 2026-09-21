"""Command line entry point: `flutter-ios-bridge-mcp [serve|init|status|doctor]`."""

import argparse
import json
import sys

from . import __version__


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="flutter-ios-bridge-mcp",
                                 description="MCP bridge between Flutter on Windows/Linux and the iOS toolchain.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the MCP server (default)")
    s.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    sub.add_parser("init", help="write a commented config file template")
    sub.add_parser("status", help="print which backends are configured")
    d = sub.add_parser("doctor", help="audit the ios/ folder of a Flutter project")
    d.add_argument("project_path", nargs="?", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "init":
        from .config import CONFIG_TEMPLATE, config_path
        p = config_path()
        if p.exists():
            print(f"Config already exists: {p}")
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"Wrote {p}\nEdit it, then add the server to your MCP client (see README).")
        return

    if args.cmd in ("status", "doctor"):
        from .errors import BridgeError
        try:
            if args.cmd == "status":
                from .server import bridge_status
                import anyio
                res = anyio.run(bridge_status)
            else:
                from .project import doctor, resolve_project
                res = doctor(resolve_project(args.project_path))
        except BridgeError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(res, indent=2, default=str))
        return

    from .server import mcp
    transport = getattr(args, "transport", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
