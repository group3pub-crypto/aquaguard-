"""Tool registry: name, description, JSON-schema parameters, async handler, role.

`get_tool_schemas()` yields OpenAI/watsonx-style function definitions so an
orchestrator (IBM watsonx Orchestrate / BOB) can call the same tools.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from bot.auth import Role, role_allows

Handler = Callable[[Any, dict, str], Awaitable[str]]  # (api_client, args, actor) -> text

_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


class ToolError(Exception):
    pass


class PermissionDenied(ToolError):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler
    required_role: Role = Role.RESIDENT


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(f"unknown tool {name}") from None

    def names(self) -> list[str]:
        return list(self._tools)

    def get_tool_schemas(self, role: Role | None = None) -> list[dict]:
        """Function-calling schemas; if `role` is given only tools it may use."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
                "x-required-role": t.required_role.name.lower(),
            }
            for t in self._tools.values()
            if role is None or role_allows(role, t.required_role)
        ]

    @staticmethod
    def validate(tool: Tool, args: dict) -> dict:
        props = tool.parameters.get("properties", {})
        for req in tool.parameters.get("required", []):
            if req not in args:
                raise ToolError(f"missing argument: {req}")
        clean: dict = {}
        for key, val in args.items():
            spec = props.get(key)
            if spec is None:
                raise ToolError(f"unexpected argument: {key}")
            typ = _TYPES[spec["type"]]
            if isinstance(val, bool) and spec["type"] in ("integer", "number"):
                raise ToolError(f"argument {key} must be {spec['type']}")
            if not isinstance(val, typ):
                raise ToolError(f"argument {key} must be {spec['type']}")
            if "enum" in spec and val not in spec["enum"]:
                raise ToolError(f"argument {key} must be one of {spec['enum']}")
            if "minimum" in spec and val < spec["minimum"]:
                raise ToolError(f"argument {key} must be >= {spec['minimum']}")
            if "maximum" in spec and val > spec["maximum"]:
                raise ToolError(f"argument {key} must be <= {spec['maximum']}")
            if spec["type"] == "string" and len(val) > spec.get("maxLength", 64):
                raise ToolError(f"argument {key} too long")
            clean[key] = val
        return clean

    async def call(self, name: str, args: dict, role: Role | None, api: Any, actor: str = "bot") -> str:
        tool = self.get(name)
        if not role_allows(role, tool.required_role):
            raise PermissionDenied(f"{name} requires role {tool.required_role.name.lower()}")
        return await tool.handler(api, self.validate(tool, args), actor)


def build_default_registry() -> ToolRegistry:
    from bot.tools import alerts, losses, report, usage, valve

    reg = ToolRegistry()
    for mod in (usage, alerts, losses, valve, report):
        mod.register(reg)
    return reg
