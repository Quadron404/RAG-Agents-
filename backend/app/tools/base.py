from __future__ import annotations

from typing import Dict

ToolSchema = Dict[str, object]


class Tool:
    def __init__(self, name: str, description: str, parameters: Dict[str, object]):
        self.name = name
        self.description = description
        self.parameters = parameters

    def schema(self) -> ToolSchema:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


ExecutionEvent = Dict[str, object]