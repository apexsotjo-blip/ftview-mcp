"""Entry point: python ftview_server.py (stdio MCP server)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftview_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
