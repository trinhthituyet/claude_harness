"""End-to-end check of the Chat assistant against the real Agent SDK.

Exercises the whole loop: a plain-English request, the model choosing harness tools,
a confirmation the user approves, and a second one the user declines with a
correction. Asserts what actually landed in the database.

Run with:  .venv/bin/python scripts/e2e_chat_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORKDIR = Path(tempfile.mkdtemp(prefix="harness-chat-e2e-"))
os.environ["HARNESS_DB"] = str(WORKDIR / "harness.db")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.services.chat import manager as chat_manager  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


async def drive(http, chat_id: str, *, answer, budget: float = 180.0) -> dict:
    """Pump one turn's events, answering confirmations with ``answer(request)``.

    Returns what was observed: the confirmations seen, the assistant's text, and the
    tools it tried to call.
    """
    chat = chat_manager.get(chat_id)
    queue = chat.bus.subscribe()
    seen = {"confirmations": [], "text": [], "tools": [], "results": []}
    deadline = asyncio.get_event_loop().time() + budget
    try:
        while asyncio.get_event_loop().time() < deadline:
            try:
                event = await asyncio.wait_for(queue.get(), 15)
            except asyncio.TimeoutError:
                continue
            kind, payload = event["type"], event.get("payload", {})
            if kind == "_eof":
                break
            if kind in {"assistant", "tool_use", "tool_result", "confirmation_request",
                        "confirmation_resolved", "error", "status"}:
                print(f"    [{kind}] {json.dumps(payload)[:170]}")
            if kind == "tool_use":
                seen["tools"].append(payload["name"])
            if kind == "tool_result":
                seen["results"].append(payload)
            if kind == "assistant":
                seen["text"].append(payload["text"])
            if kind == "confirmation_request":
                seen["confirmations"].append(payload)
                body = answer(payload)
                posted = await http.post(
                    f"/api/chat/{chat_id}/confirmations/{payload['id']}", json=body
                )
                check("confirmation answered over the API", posted.status_code == 200,
                      posted.text[:120])
            if kind == "status" and payload.get("state") in {"idle", "failed"}:
                break
    finally:
        chat.bus.unsubscribe(queue)
    return seen


async def main() -> int:
    app = create_app()
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test", timeout=120) as http:
            print("\n== turn 1: ask for a role, approve it ==")
            started = await http.post("/api/chat", json={
                "title": "e2e",
                "message": "Create a role called Tester whose job is writing and running "
                           "tests and reporting real failures honestly.",
            })
            check("chat created", started.status_code == 201, started.text[:150])
            chat_id = started.json()["chat_id"]

            seen = await drive(http, chat_id, answer=lambda r: {"approved": True})
            check("the assistant called create_role",
                  any(t == "create_role" for t in seen["tools"]), str(seen["tools"]))
            check("it asked for confirmation before writing",
                  any(c["tool_name"] == "create_role" for c in seen["confirmations"]),
                  f"{len(seen['confirmations'])} confirmation(s)")

            roles = (await http.get("/api/roles")).json()
            check("the role now exists", any(r["name"] == "Tester" for r in roles),
                  str([r["name"] for r in roles]))
            tester = next((r for r in roles if r["name"] == "Tester"), None)
            check("its system prompt is real content, not a placeholder",
                  bool(tester and len(tester["system_prompt"]) > 40),
                  f"{len(tester['system_prompt']) if tester else 0} chars")

            print("\n== turn 2: an under-specified request should ask, not guess ==")
            await http.post(f"/api/chat/{chat_id}/messages", json={
                "text": "Now add a role called Foo.",
            })
            seen2 = await drive(
                http, chat_id,
                answer=lambda r: {"approved": False, "reason": "should not have been reached"},
            )
            check("it asked instead of creating anything",
                  not seen2["confirmations"] and bool(seen2["text"]),
                  f"{len(seen2['confirmations'])} confirmation(s)")
            check("the question mentions what it needs",
                  any("?" in text for text in seen2["text"]),
                  " ".join(seen2["text"])[:120])
            check("nothing was created while it waited for an answer",
                  not any(r["name"] == "Foo" for r in (await http.get("/api/roles")).json()))

            print("\n== turn 3: answer it, then decline the confirmation ==")
            await http.post(f"/api/chat/{chat_id}/messages", json={
                "text": "Foo reviews database migrations for backwards compatibility. "
                        "Go ahead and create it.",
            })
            seen3 = await drive(
                http, chat_id,
                answer=lambda r: {"approved": False,
                                  "reason": "Actually no, don't create Foo. Leave the roles "
                                            "as they are."},
            )
            check("a confirmation was raised once it had enough detail",
                  any(c["tool_name"] == "create_role" for c in seen3["confirmations"]),
                  str([c["tool_name"] for c in seen3["confirmations"]]))

            roles_after = (await http.get("/api/roles")).json()
            check("the declined role was NOT created",
                  not any(r["name"] == "Foo" for r in roles_after),
                  str([r["name"] for r in roles_after]))
            check("the assistant acknowledged the decline",
                  bool(seen3["text"]), " ".join(seen3["text"])[:140])

            print("\n== transcript and state ==")
            detail = (await http.get(f"/api/chat/{chat_id}")).json()
            types = [m["type"] for m in detail["messages"]]
            check("the transcript was persisted", len(types) > 6, f"{len(types)} entries")
            check("it contains every user message", types.count("user") == 3,
                  str(types.count("user")))
            check("confirmations are in the transcript",
                  "confirmation_request" in types and "confirmation_resolved" in types)
            check("no confirmations left pending", detail["pending_confirmations"] == [])
            check("chat is idle again", detail["chat"]["status"] == "idle",
                  detail["chat"]["status"])
            if detail["chat"]["error_text"]:
                print(f"  error_text: {detail['chat']['error_text'][:300]}")

            print("\n== containment ==")
            init = [m for m in detail["messages"]
                    if m["type"] == "status" and m["payload"].get("tools")]
            if init:
                tools = init[0]["payload"]["tools"]
                check("the session has only harness tools",
                      all(t.startswith("mcp__harness__") for t in tools),
                      f"{len(tools)} tools")
                check("no filesystem or shell tools are present",
                      not any(t in tools for t in ("Bash", "Read", "Write", "Edit")))
            else:
                check("init reported the session's tool list", False, "no init status seen")

            cost = detail["chat"]["total_cost_usd"]
            print(f"  cost: ${cost:.4f}" if cost else "  cost: unknown")

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    print(f"workdir: {WORKDIR}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
