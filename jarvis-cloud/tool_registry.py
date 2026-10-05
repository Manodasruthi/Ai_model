"""Only functions explicitly registered here can run. Never eval model output."""
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from pydantic import BaseModel, ConfigDict, ValidationError


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Tool:
    description: str
    arguments: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[dict]]
    mutates_device: bool = False


async def server_time(_: EmptyArgs) -> dict:
    return {"utc": datetime.now(timezone.utc).isoformat()}


REGISTRY = {
    "get_server_time": Tool("Read the current UTC date and time.", EmptyArgs, server_time),
}


def definitions() -> list[dict]:
    return [{"type": "function", "function": {
        "name": name, "description": tool.description,
        "parameters": tool.arguments.model_json_schema(),
    }} for name, tool in REGISTRY.items()]


async def execute(name: str, arguments: str) -> str:
    tool = REGISTRY.get(name)
    if not tool:
        return json.dumps({"error": "Unknown tool"})
    if tool.mutates_device:
        # Implement a separate, authenticated confirmation flow before enabling writes.
        return json.dumps({"error": "Device changes are disabled in this starter"})
    try:
        parsed = tool.arguments.model_validate_json(arguments)
        result = await asyncio.wait_for(tool.handler(parsed), timeout=8)
        return json.dumps(result, ensure_ascii=False)[:4000]
    except (ValueError, ValidationError):
        return json.dumps({"error": "Invalid tool arguments"})
    except Exception:
        return json.dumps({"error": "Tool failed or timed out"})
