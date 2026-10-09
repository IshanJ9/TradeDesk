"""The copilot as a LangGraph state graph (ORCHESTRATOR=langgraph). Same tools, same guards, same answers
as the classic loop in app/llm/copilot.py; what it adds is an explicit, inspectable flow and a live trace.

    START -> input_guard --refused--> END
                 |
               router            (code: READ route = read-only tools, ACT route = all tools)
                 |
               model <------+    (the LLM: asks for tools or answers)
                 |          |
          tool calls? --> tools  (code runs each tool; at most MAX_STEPS rounds)
                 |
            output_guard         (code: card wording, no "I placed it", no advice, every number grounded)
                 |
                END

Every node reports to the activity panel as a TraceEvent. Nothing in the graph can send an order: the tools
can only read or draft a card, and only the trader's Approve click (a separate HTTP route) sends anything.
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.api_models import ChatReply
from app.agent.router import Route, route
from app.events import EventHub
from app.llm.copilot import GAVE_UP, MAX_STEPS, Copilot
from app.llm.prompt import build_system_prompt
from app.llm.tools import ToolContext
from app.llm.types import Message, ToolCall, ToolResult
from app.schemas import AuditKind
from app.trace import Tracer


class TurnState(TypedDict, total=False):
    message: str
    tracer: Tracer
    ctx: ToolContext
    route: Route
    messages: list[Message]
    calls: list[ToolCall]
    steps: int
    final: str
    reply: ChatReply


class GraphCopilot(Copilot):
    def __init__(self, *args: Any, hub: EventHub, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._hub = hub
        self._graph = self._build()

    def _build(self):
        g = StateGraph(TurnState)
        g.add_node("input_guard", self._input_guard)
        g.add_node("router", self._router)
        g.add_node("model", self._model)
        g.add_node("tools", self._tools_node)
        g.add_node("output_guard", self._output_guard)
        g.add_edge(START, "input_guard")
        g.add_conditional_edges("input_guard", lambda s: "done" if "reply" in s else "next", {"done": END, "next": "router"})
        g.add_edge("router", "model")
        g.add_conditional_edges("model", lambda s: "tools" if s.get("calls") else "answer", {"tools": "tools", "answer": "output_guard"})
        g.add_conditional_edges("tools", lambda s: "again" if s["steps"] < MAX_STEPS else "stop", {"again": "model", "stop": "output_guard"})
        g.add_edge("output_guard", END)
        return g.compile()

    async def _turn(self, message: str) -> ChatReply:
        self._audit.record(AuditKind.USER_MESSAGE, "user", message, data={"message": message})
        tracer = Tracer(self._hub)
        state = await self._graph.ainvoke({"message": message, "tracer": tracer, "steps": 0, "final": ""})
        return state["reply"]

    # ---- nodes ---------------------------------------------------------------------------------- #

    async def _input_guard(self, s: TurnState) -> dict:
        t = s["tracer"]
        with t.step("input_guard", "guard"):
            refusal = self._refuse_override(s["message"])
        if refusal:
            t.emit("input_guard", "guard", "blocked", "message tried to change the assistant's rules; answered by code")
            return {"reply": refusal}
        return {}

    async def _router(self, s: TurnState) -> dict:
        r = route(s["message"])
        s["tracer"].emit("router", "node", "end", "question: read-only tools" if r == "read" else "may prepare a card for your approval")
        return {
            "route": r,
            "ctx": self._context(s["message"]),
            "messages": [*self._history, Message("user", s["message"])],
        }

    async def _model(self, s: TurnState) -> dict:
        tools = [t for t in self._tools.values() if s["route"] == "act" or t.read_only]
        with s["tracer"].step("model", "node", f"step {s['steps'] + 1}"):
            turn = await self._llm.complete(
                system=build_system_prompt(self._clock()), messages=s["messages"], tools=[t.spec for t in tools]
            )
        if not turn.tool_calls:
            return {"calls": [], "final": turn.text}
        return {"calls": turn.tool_calls, "messages": [*s["messages"], Message("assistant", turn.text, tool_calls=turn.tool_calls)]}

    async def _tools_node(self, s: TurnState) -> dict:
        t, ctx, results = s["tracer"], s["ctx"], []
        for call in s["calls"]:
            if s["route"] == "read" and call.name in self._tools and not self._tools[call.name].read_only:
                t.emit(f"tool:{call.name}", "guard", "blocked", "not available for a question")
                results.append(self._not_offered(call))
                continue
            with t.step(f"tool:{call.name}", "tool"):
                result = await self._run_tool(ctx, call)
            status = result.output.get("status") if isinstance(result.output, dict) else None
            if status in ("blocked", "invalid"):
                t.emit(f"tool:{call.name}", "guard", "blocked", str(result.output.get("message") or status))
            results.append(result)
        steps = s["steps"] + 1
        out: dict = {"calls": [], "steps": steps, "messages": [*s["messages"], Message("tool", tool_results=results)]}
        if steps >= MAX_STEPS:
            out["final"] = GAVE_UP
        return out

    async def _output_guard(self, s: TurnState) -> dict:
        t = s["tracer"]
        with t.step("output_guard", "guard"):
            text, replaced = self._shape(s["ctx"], s.get("final", ""), s["message"])
            reply = self._finish(s["ctx"], s["message"], text)
        if replaced:
            t.emit("output_guard", "guard", "blocked", f"replaced the model's answer: {replaced}")
        return {"reply": reply}

    @staticmethod
    def _not_offered(call: ToolCall) -> ToolResult:
        """The model asked for a drafting tool on a question. Refused in code; the model answers from data."""
        return ToolResult(call.id, call.name, {"status": "error", "message": "That tool is not available for a question. Answer from the data instead."})
