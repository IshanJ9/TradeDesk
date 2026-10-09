"""Demo-only trace events. This route never invokes the copilot or broker."""

import uuid

from fastapi import APIRouter, HTTPException, Request

from app.schemas import Model
from app.trace import Tracer

router = APIRouter(prefix="/api/dev", tags=["demo"])


class TraceSampleReply(Model):
    run_id: str
    demo: bool = True


@router.post("/trace-sample", response_model=TraceSampleReply)
async def trace_sample(request: Request) -> TraceSampleReply:
    if not request.app.state.settings.demo_mode:
        raise HTTPException(404, "Not found")
    tracer = Tracer(request.app.state.hub, run_id=f"demo-{uuid.uuid4().hex[:12]}")
    # Fixed illustrative timings, explicitly labelled as demo data in the UI.
    steps = [
        ("input_guard", "guard", "end", "DEMO DATA: checked a sample account question.", 4),
        ("router", "node", "end", "DEMO DATA: selected an account lookup.", 12),
        ("tool:get_positions", "tool", "end", "DEMO DATA: simulated a positions lookup; no broker request.", 85),
        ("output_guard", "guard", "blocked", "DEMO DATA: blocked an unsupported number in the sample reply.", 3),
    ]
    for node, kind, status, detail, ms in steps:
        tracer.emit(node, kind, "start")
        tracer.emit(node, kind, status, detail, ms)
    return TraceSampleReply(run_id=tracer.run_id)
