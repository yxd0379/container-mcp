from __future__ import annotations

import asyncio
import logging
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import uvicorn
from mcp.server.fastmcp import FastMCP
from uvicorn.config import LOGGING_CONFIG


PROJECT_DIR = Path(__file__).resolve().parents[2]
TMP_DIR = PROJECT_DIR / "tmp"
SERVICE_LOG_PATH = TMP_DIR / "container-mcp.log"
SERVICE_PID_PATH = TMP_DIR / "container-mcp.pid"
DEFAULT_RUNLOG_DIR = PROJECT_DIR / "RUNLOG"
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 9943
MANUAL_RUN_ID = "manual"

RUNLOG_DIR = DEFAULT_RUNLOG_DIR
ALLOWED_CONTAINERS: frozenset[str] = frozenset()

LOGGER = logging.getLogger("uvicorn.error")
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def configure_runlog_dir(value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else PROJECT_DIR / path).resolve()


def validate_container(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError(
            "container must be a Docker container name or id using only letters, "
            "digits, underscore, period, and hyphen"
        )
    return value


def resolve_container(value: str) -> str:
    container = validate_container(value)
    if container not in ALLOWED_CONTAINERS:
        allowed = ", ".join(sorted(ALLOWED_CONTAINERS))
        raise ValueError(f"container {container!r} is not allowed; allowed containers: {allowed}")
    return container


def _error_text(name: str, exc: Exception) -> str:
    cause = exc.__cause__ or exc
    if name == "dpatch":
        return str(getattr(cause, "code", type(cause).__name__))
    return str(cause).replace("\r", "\\r").replace("\n", "\\n")[:500]


class ContainerMCP(FastMCP):
    """FastMCP with one server-side diagnostic record around every tool call."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        context = self.get_context()
        try:
            request_id = str(context.request_id)
        except ValueError:
            request_id = "-"
        raw_container = arguments.get("container")
        container = (
            raw_container
            if isinstance(raw_container, str) and raw_container in ALLOWED_CONTAINERS
            else "<rejected>"
        )
        fields = f"request_id={request_id} tool={name} container={container}"
        started = time.monotonic()
        LOGGER.info("tool.start %s", fields)
        try:
            result = await super().call_tool(name, arguments)
        except asyncio.CancelledError:
            duration = int((time.monotonic() - started) * 1000)
            LOGGER.warning("tool.cancelled %s duration_ms=%d", fields, duration)
            raise
        except Exception as exc:
            duration = int((time.monotonic() - started) * 1000)
            LOGGER.error(
                "tool.failed %s duration_ms=%d error=%r",
                fields,
                duration,
                _error_text(name, exc),
            )
            raise
        duration = int((time.monotonic() - started) * 1000)
        LOGGER.info("tool.finish %s duration_ms=%d", fields, duration)
        return result


server = ContainerMCP(
    "container-mcp",
    instructions=(
        "Run commands inside an explicitly selected allowed container. "
        "Use dinspect to check isolation, dexec to run commands, and dpatch to apply "
        "Codex-style patches to absolute container paths. Complete dexec output is "
        "stored in RUNLOG; never assume a container protects the host."
    ),
)


from . import dexec, dinspect, dpatch  # noqa: E402


server.tool()(dexec.dexec)
server.tool()(dpatch.dpatch)
server.tool()(dinspect.dinspect)


@server._mcp_server.set_logging_level()
async def _set_logging_level(level: mcp_types.LoggingLevel) -> None:
    dexec.set_logging_level(level)


def uvicorn_log_config() -> dict[str, Any]:
    """Add local timestamps to Uvicorn's standard logging configuration."""
    log_config = deepcopy(LOGGING_CONFIG)
    for formatter in log_config["formatters"].values():
        formatter["fmt"] = f"%(asctime)s {formatter['fmt']}"
        formatter["datefmt"] = LOG_DATE_FORMAT
        formatter["use_colors"] = False
    log_config["root"] = {"handlers": ["default"], "level": "INFO"}
    return log_config


def run_http_server() -> None:
    uvicorn.run(
        server.streamable_http_app(),
        host=server.settings.host,
        port=server.settings.port,
        log_level=server.settings.log_level.lower(),
        log_config=uvicorn_log_config(),
    )
