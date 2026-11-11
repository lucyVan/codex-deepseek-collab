"""Operator-configured official-SDK MCP launcher, including Python -I mode."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collab.mcp_server import main

if __name__ == "__main__":
    main()
