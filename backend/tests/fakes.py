"""Test doubles for the LLM API."""

from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

from anthropic.types import Usage
from pydantic import BaseModel

from trackrecord.promises.schema import ExtractedPromise, ExtractionOutput


def usage(input_tokens: int = 1000, output_tokens: int = 200) -> Usage:
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def response(output: BaseModel | None, *, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        parsed_output=output,
        stop_reason=stop_reason,
        stop_details=None,
        usage=usage(),
        _request_id="req_test",
    )


class FakeMessages:
    def __init__(self, respond: Callable[[dict[str, Any]], Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._respond = respond
        self.batches = FakeBatches()

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._respond(kwargs)


class FakeLlmClient:
    """Stands in for anthropic.AsyncAnthropic: `messages.parse` and `messages.batches`."""

    def __init__(self, respond: Callable[[dict[str, Any]], Any]) -> None:
        self.messages = FakeMessages(respond)


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[list[dict[str, Any]]] = []
        self.status = "in_progress"
        self.results_by_id: dict[str, list[Any]] = {}

    async def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
        self.created.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}")

    async def retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=batch_id, processing_status=self.status)

    async def results(self, batch_id: str) -> AsyncIterator[Any]:
        async def iterate() -> AsyncIterator[Any]:
            for item in self.results_by_id.get(batch_id, []):
                yield item

        return iterate()


def batch_success(custom_id: str, output: ExtractionOutput) -> SimpleNamespace:
    message = SimpleNamespace(
        id="msg_test",
        stop_reason="end_turn",
        usage=usage(),
        content=[SimpleNamespace(type="text", text=output.model_dump_json())],
    )
    return SimpleNamespace(
        custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=message)
    )


def batch_error(custom_id: str) -> SimpleNamespace:
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="errored"))


def promise(**overrides: Any) -> ExtractedPromise:
    """An extracted promise with neutral defaults: NVIDIA's Q3 FY2027 revenue guidance."""
    fields: dict[str, Any] = {
        "quote": "Revenue is expected to be $108.0 billion, plus or minus 2%.",
        "metric": "revenue",
        "metric_name": "Revenue",
        "basis": "unspecified",
        "fiscal_year": 2027,
        "fiscal_quarter": 3,
        "kind": "plus_minus",
        "unit": "usd",
        "scale": "billions",
        "value": 108.0,
        "low": None,
        "high": None,
        "approximate": False,
        "plus_minus": 2,
        "plus_minus_unit": "percent_of_value",
        "plus_minus_scale": None,
        "bound_direction": None,
        "relative_reference": None,
        "change_low": None,
        "change_high": None,
        "change_words": None,
        "confidence": 0.95,
    }
    fields.update(overrides)
    return ExtractedPromise(**fields)


# The extraction a correct model would return for tests/fixtures/nvda_q2fy27_release.htm.
NVDA_Q2FY27_OUTPUT = ExtractionOutput(
    promises=[
        promise(),
        promise(
            quote="GAAP and non-GAAP gross margins are expected to be 74.0%, plus or minus 50 "
            "basis points.",
            metric="gross_margin",
            metric_name="GAAP gross margin",
            basis="gaap",
            unit="percent",
            scale="units",
            value=74.0,
            plus_minus=50,
            plus_minus_unit="basis_points",
        ),
        promise(
            quote="GAAP and non-GAAP gross margins are expected to be 74.0%, plus or minus 50 "
            "basis points.",
            metric="gross_margin",
            metric_name="Non-GAAP gross margin",
            basis="non_gaap",
            unit="percent",
            scale="units",
            value=74.0,
            plus_minus=50,
            plus_minus_unit="basis_points",
        ),
        promise(
            quote="GAAP and non-GAAP operating expenses are expected to be approximately $9.2 "
            "billion and $9.0 billion, respectively.",
            metric="operating_expenses",
            metric_name="GAAP operating expenses",
            basis="gaap",
            kind="point",
            value=9.2,
            approximate=True,
            plus_minus=None,
            plus_minus_unit=None,
        ),
        promise(
            quote="GAAP and non-GAAP operating expenses are expected to be approximately $9.2 "
            "billion and $9.0 billion, respectively.",
            metric="operating_expenses",
            metric_name="Non-GAAP operating expenses",
            basis="non_gaap",
            kind="point",
            value=9.0,
            approximate=True,
            plus_minus=None,
            plus_minus_unit=None,
        ),
        promise(
            quote="For the full year fiscal 2027, NVIDIA expects GAAP and non-GAAP tax rates to "
            "be between 16.0% and 18.0%",
            metric="tax_rate",
            metric_name="GAAP tax rate",
            basis="gaap",
            fiscal_quarter=None,
            kind="range",
            unit="percent",
            scale="units",
            value=None,
            low=16.0,
            high=18.0,
            plus_minus=None,
            plus_minus_unit=None,
        ),
    ]
)
