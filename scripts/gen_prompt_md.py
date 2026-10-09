r"""Writes PROMPT.md: everything TradeDesk sends to its language models, generated from the code itself so it can
never drift from what the model really receives.

    .venv\Scripts\python scripts\gen_prompt_md.py           # write PROMPT.md
    .venv\Scripts\python scripts\gen_prompt_md.py --check   # exit 1 if PROMPT.md is out of date (used by the tests)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.llm import copilot  # noqa: E402
from app.llm.fallback import NOTICE as FALLBACK_NOTICE  # noqa: E402
from app.llm.prompt import build_system_prompt  # noqa: E402
from app.llm.tools import build_tools  # noqa: E402
from app.voice.service import PROMPT as VOICE_PROMPT  # noqa: E402

DATE_MARK = "2000-01-01"


def render() -> str:
    s = Settings()
    system = build_system_prompt(datetime(2000, 1, 1, tzinfo=timezone.utc)).replace(DATE_MARK, "{today's date, YYYY-MM-DD}")
    tools = build_tools()
    out = []
    w = out.append
    w("# PROMPT.md: the prompts and tools given to TradeDesk's AI\n")
    w("This file is generated from the code by `scripts/gen_prompt_md.py`, and a test fails if it ever differs from what")
    w("the model really receives. It contains the full system prompt, every tool definition sent with it, the model")
    w("settings, the fixed replies that code substitutes for the model's words, and the hint given to speech-to-text.\n")
    w("TradeDesk has one language-model agent: the chat copilot. It can only read the account and draft cards; it has no")
    w("tool that approves, sends or executes an order. Only the trader's click on an exact order card sends anything.\n")

    w("## 1. Model and settings\n")
    w(f"- Provider: AWS Bedrock Converse API (`LLM_PROVIDER=bedrock`), model `{s.bedrock_model_id}`, region `{s.aws_region}`.")
    w("- Temperature 0 (the same sentence should give the same card), at most 800 output tokens per call.")
    w(f"- Up to {copilot.MAX_STEPS} model calls per message (tool-use rounds); the last {copilot.HISTORY_LIMIT} messages of the conversation are kept.")
    w("- Orchestrator: `ORCHESTRATOR=langgraph` runs the same prompt and tools as a LangGraph graph")
    w("  (input guard -> router -> model <-> tools -> output guard). The router and both guards are plain code: no model call,")
    w("  no prompt. For a question, the router offers only the read-only tools in section 3.")
    w("- If Bedrock is unavailable, a built-in keyword parser (no language model, no prompt) answers that turn instead.\n")

    w("## 2. System prompt (sent verbatim on every model call)\n")
    w("The only variable part is today's date.\n")
    w("```text")
    w(system.rstrip("\n"))
    w("```\n")

    w("## 3. Tools offered to the model\n")
    w("Sent with every call as Bedrock `toolConfig.tools` (name, description, JSON input schema). No tool can approve,")
    w("send or execute an order, and the broker connection the tools use is read-only.\n")
    rule_tools = {"create_rule", "cancel_rule", "alert_on_holdings"}
    w("| Tool | What it can do |")
    w("|---|---|")
    for name, t in tools.items():
        kind = ("reads the account" if t.read_only
                else "saves or cancels a standing rule; a rule only ever alerts or prepares a card, it never sends" if name in rule_tools
                else "drafts an order card or plan; nothing is sent until the trader approves it")
        w(f"| `{name}` | {kind} |")
    w("")
    for name, t in tools.items():
        w(f"### `{name}`\n")
        w(t.spec.description + "\n")
        w("```json")
        w(json.dumps(t.spec.input_schema, indent=2, ensure_ascii=False))
        w("```\n")

    w("## 4. Replies written by code, not by the model\n")
    w("When a guard stops the model's answer, the trader sees one of these fixed texts instead:\n")
    for label, text in [
        ("A message tried to override the rules (answered before the model is called)", copilot.OVERRIDE_REFUSAL),
        ("The model claimed an order was placed", copilot.NOT_PLACED),
        ("The model gave advice or a prediction", copilot.NO_ADVICE),
        ("The model used a number not found in the account data", copilot.NOT_GROUNDED),
        ("The model used up its tool-call rounds", copilot.GAVE_UP),
        ("Bedrock was unavailable and the keyword parser answered", FALLBACK_NOTICE),
    ]:
        w(f"- **{label}:** “{text}”")
    w("\nOrder cards and plan descriptions are also written by code from the card itself, never by the model.\n")

    w("## 5. Speech-to-text hint (voice input)\n")
    w("Voice uses Groq `whisper-large-v3-turbo` (temperature 0). It receives this spelling hint, not instructions; the")
    w("transcript is shown to the trader to edit and is then handled exactly like typed text.\n")
    w("```text")
    w(VOICE_PROMPT)
    w("```")
    return "\n".join(out) + "\n"


def main() -> int:
    path = ROOT / "PROMPT.md"
    text = render()
    if "--check" in sys.argv:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            print("PROMPT.md is out of date: run python scripts/gen_prompt_md.py")
            return 1
        print("PROMPT.md is up to date")
        return 0
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)} ({len(text):,} characters, {len(build_tools())} tools)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
