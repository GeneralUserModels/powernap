from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent.agent import Agent, STRUCTURED_FINAL_MAX_TOKENS
from apps.common.structured_completion import pydantic_response_format
from apps.memory.schemas.structured import FinalizePageOpsPayload
from shared.model_catalog import default_model


class _DefaultListPayload(BaseModel):
    items: list[str] = []
    notes: str


class AgentStructuredOutputTests(unittest.TestCase):
    def test_response_format_preserves_pydantic_required_fields(self):
        schema = pydantic_response_format(_DefaultListPayload)["json_schema"]["schema"]

        self.assertEqual(schema["required"], ["notes"])

    def test_structured_final_sends_explicit_json_schema(self):
        captured: dict = {}
        payload = {
            "create_pages": [],
            "update_pages": [],
            "delete_pages": [],
            "notes": "ok",
        }

        def fake_call(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(payload),
                            tool_calls=[],
                        )
                    )
                ]
            )

        agent = Agent(
            model=default_model("agent"),
            system_prompt="system",
            tools=[],
            llm_max_tokens=16000,
        )
        agent._call_structured_final = fake_call  # type: ignore[method-assign]

        result = agent._finalize_structured(
            [{"role": "user", "content": "finish"}],
            FinalizePageOpsPayload,
            "Return the requested result.",
            "test_structured",
        )

        self.assertEqual(json.loads(result), payload)
        self.assertEqual(captured["response_format"]["type"], "json_schema")
        json_schema = captured["response_format"]["json_schema"]
        self.assertEqual(json_schema["name"], "FinalizePageOpsPayload")
        self.assertIs(json_schema["strict"], True)
        self.assertEqual(json_schema["schema"]["title"], "FinalizePageOpsPayload")
        self.assertIn("description", json_schema["schema"]["properties"]["update_pages"])
        self.assertEqual(
            set(json_schema["schema"]["required"]),
            {"create_pages", "update_pages", "delete_pages", "notes"},
        )
        self.assertIs(captured["enable_json_schema_validation"], True)
        self.assertGreaterEqual(captured["max_tokens"], STRUCTURED_FINAL_MAX_TOKENS)


if __name__ == "__main__":
    unittest.main()
