"""Local GenerationPort: a deterministic, SDK-free narrator for the offline profile.

It stands in for a managed model in the gate, the tests and the demo. It never decides anything:
it restates the engine-owned facts it is handed as a short JSON note, so its output is grounded by
construction and the whole offline pipeline (including the narration path) runs with no network
and no cloud SDK. A silent empty return would let a producer ship the narration seam unwired, so
this deliberately produces a real, inspectable note.
"""

from __future__ import annotations

import json

from ...config import Settings
from ...ports.generation import GenerationRequest, GenerationResponse


class LocalGenerationAdapter:
    """Restate the request's engine facts as a deterministic JSON note (no model, no network)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        values = dict(request.facts)
        note = (
            f"Coverage stands at {values.get('covered', '0')} covered, "
            f"{values.get('partial', '0')} partial and {values.get('uncovered', '0')} uncovered; "
            f"{values.get('orphan_controls', '0')} orphan control(s) and "
            f"{values.get('stale_edges', '0')} stale mapping(s) need review."
        )
        return GenerationResponse(text=json.dumps({"note": note}), model="local-deterministic")
