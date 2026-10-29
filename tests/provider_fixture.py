"""Synthetic chat/tool transcript for offline provider and authorization handoff tests."""

import json

import httpx


class ProviderFixture:
    def __init__(self, *, denied_dataset=False):
        self.requests = []
        self.denied_dataset = denied_dataset

    def handle(self, request):
        data = json.loads(request.content)
        self.requests.append(data)
        observations = [m for m in data["messages"] if m["role"] == "tool"]
        if observations:
            value = json.loads(observations[-1]["content"])
            assert json.loads(value["summary"]) == {"mean": 4.0, "count": 3}
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "fixture-final-1",
                        "type": "function",
                        "function": {
                            "name": "finalize_statistics",
                            "arguments": json.dumps(
                                {
                                    "status": "completed",
                                    "explanation": "Mean is 4.",
                                    "result_ids": [value["tool_call_id"]],
                                    "assumptions": [],
                                }
                            ),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            dataset = json.loads(data["messages"][1]["content"])["datasets"][0]
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "fixture-call-1",
                        "type": "function",
                        "function": {
                            "name": "verification_mean",
                            "arguments": json.dumps(
                                {
                                    "dataset_id": "outside-fixture"
                                    if self.denied_dataset
                                    else dataset["dataset_id"],
                                    "column": "price",
                                }
                            ),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        return httpx.Response(
            200,
            json={
                "model": "fixture-model-v1",
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "completion_tokens_details": {"reasoning_tokens": 10},
                },
            },
        )
