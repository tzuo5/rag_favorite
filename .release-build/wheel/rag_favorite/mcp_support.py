"""Client-neutral MCP configuration and protocol smoke testing."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig, ConfigError
from .mcp_contracts import MCP_TOOL_NAMES


def mcp_installed() -> bool:
    return importlib.util.find_spec("mcp") is not None


def client_config(
    config: AppConfig,
    *,
    python_executable: Path | None = None,
) -> dict[str, Any]:
    """Return a portable `mcpServers` fragment without embedding credentials."""

    executable = (python_executable or Path(sys.executable)).expanduser().absolute()
    return {
        "mcpServers": {
            "rag-favorite": {
                "command": str(executable),
                "args": ["-m", "rag_favorite.mcp_server"],
                "env": {"RAG_FAVORITE_CONFIG": str(config.source)},
            }
        }
    }


def server_info(config: AppConfig) -> dict[str, Any]:
    return {
        "name": "rag-favorite",
        "version": __version__,
        "transport": "stdio",
        "network_listener": False,
        "tools": list(MCP_TOOL_NAMES),
        "configuration": str(config.source),
        "mcp_dependency_installed": mcp_installed(),
    }


def write_client_config(path: Path, payload: dict[str, Any], *, force: bool) -> Path:
    destination = path.expanduser().resolve()
    if destination.exists() and not force:
        raise ConfigError(f"MCP configuration already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(destination)
    return destination


async def _protocol_smoke(config: AppConfig) -> tuple[str, ...]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise RuntimeError(
            "MCP support is not installed. Run: pip install 'rag-favorite[mcp]'"
        ) from exc

    environment = os.environ.copy()
    environment["RAG_FAVORITE_CONFIG"] = str(config.source)
    parameters = StdioServerParameters(
        command=str(Path(sys.executable).absolute()),
        args=["-m", "rag_favorite.mcp_server"],
        env=environment,
    )
    async with stdio_client(parameters) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = tuple(sorted(tool.name for tool in tools.tools))
    expected = tuple(sorted(MCP_TOOL_NAMES))
    if names != expected:
        raise RuntimeError(
            f"Unexpected MCP tools: {names}; expected exactly: {expected}"
        )
    return names


def protocol_smoke(config: AppConfig) -> tuple[str, ...]:
    try:
        return asyncio.run(_protocol_smoke(config))
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("MCP stdio handshake failed.") from exc
