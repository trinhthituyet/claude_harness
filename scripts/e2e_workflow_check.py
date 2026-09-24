"""End-to-end check of a real workflow run, including a loop.

Builds a three-node graph (design → build → test) where a test failure loops back to
build, then runs it against a temp project whose test suite fails on the first pass.
The point is to see the harness take the loop edge and then finish — real control
flow, not a prompt that merely describes one.

Run with:  .venv/bin/python scripts/e2e_workflow_check.py
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

WORK = Path(tempfile.mkdtemp(prefix="harness-wf-e2e-"))
PROJECT = WORK / "project"
PROJECT.mkdir()
os.environ["HARNESS_DB"] = str(WORK / "harness.db")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.services.runner import manager  # noqa: E402

# A tiny broken module plus a checker script. The first pass fails, so the graph
# must loop back to the engineer before it can finish.
(PROJECT / "calc.py").write_text(
    "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a + b\n",
    encoding="utf-8",
)
(PROJECT / "check.py").write_text(
    "from calc import add, mul\n"
    "assert add(2, 3) == 5, f'add(2,3) returned {add(2,3)}, expected 5'\n"
    "assert mul(2, 3) == 6, f'mul(2,3) returned {mul(2,3)}, expected 6'\n"
    "print('OK')\n",
    encoding="utf-8",
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


async def main() -> int:
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", timeout=120
        ) as http:
            print("\n== build the graph ==")
            roles = {}
            for name, prompt in (
                ("WF Engineer",
                 "You are an engineer. Fix the code so the checker passes. Use Edit or "
                 "Write on files in the project directory. Be brief."),
                ("WF Tester",
                 "You are a reviewer. Read calc.py and check.py with the Read tool and "
                 "work out, by inspection, whether every assertion in check.py would "
                 "pass. State clearly PASS or FAIL and which assertion fails. Never edit "
                 "anything, and do not use Bash."),
            ):
                response = await http.post("/api/roles", json={
                    "name": name, "description": name, "system_prompt": prompt,
                })
                check(f"role {name}", response.status_code == 201, response.text[:120])
                roles[name] = response.json()["id"]

            workflow = await http.post("/api/workflows", json={
                "name": "Fix until green",
                "description": "Engineer fixes, tester verifies, loop on failure.",
                "max_steps": 8,
                "nodes": [
                    {"key": "build", "role_id": roles["WF Engineer"], "is_start": True,
                     "max_visits": 3,
                     "instructions": "Fix exactly ONE failing assertion in calc.py, then stop "
                     "and hand back. Do not fix more than one per turn."},
                    {"key": "test", "role_id": roles["WF Tester"], "max_visits": 3,
                     "instructions": "Read calc.py and check.py and report PASS or FAIL "
                     "with the reason."},
                ],
                "edges": [
                    {"from_key": "build", "to_key": "test", "label": "to_test"},
                    {"from_key": "test", "to_key": "build", "label": "rework",
                     "condition": "the reviewer found an assertion that would still fail"},
                    {"from_key": "test", "to_key": None, "label": "approved",
                     "condition": "the reviewer confirmed every assertion would pass",
                     "is_default": True},
                ],
            })
            check("workflow created", workflow.status_code == 201, workflow.text[:200])
            if workflow.status_code != 201:
                return 1
            graph = workflow.json()
            check("the loop edge points back to build",
                  any(e["label"] == "rework" and e["to_key"] == "build" for e in graph["edges"]))
            check("the approved edge finishes",
                  any(e["label"] == "approved" and e["to_key"] is None for e in graph["edges"]))

            task = await http.post("/api/tasks", json={
                "name": "make the checker pass",
                "prompt": "calc.py has bugs that would make check.py's assertions "
                          "fail. Get every assertion correct.",
                "project_path": str(PROJECT),
                "workflow_id": graph["id"],
                # The OS sandbox cannot nest inside an existing sandbox, and this script
                # may itself be sandboxed. Real runs should leave this on.
                "sandbox_bash": False,
                "approval_timeout_s": 60,
            })
            check("task created", task.status_code == 201, task.text[:200])
            if task.status_code != 201:
                return 1

            print("\n== run it ==")
            started = await http.post(f"/api/tasks/{task.json()['id']}/run")
            check("run started", started.status_code == 202, started.text[:150])
            run_id = started.json()["run_id"]

            run = manager.get(run_id)
            queue = run.bus.subscribe()
            path: list[str] = []
            edges_taken: list[dict] = []
            deadline = asyncio.get_event_loop().time() + 600
            while asyncio.get_event_loop().time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), 30)
                except asyncio.TimeoutError:
                    print("    …waiting")
                    continue
                kind, payload = event["type"], event.get("payload", {})
                if kind == "_eof":
                    break
                if kind in {"node_started", "node_finished", "edge_taken", "workflow_started",
                            "workflow_stopped", "workflow_finished", "error", "status"}:
                    print(f"    [{kind}] {json.dumps(payload)[:160]}")
                if kind == "node_started":
                    path.append(f"{payload['node']}#{payload['visit']}")
                if kind == "edge_taken":
                    edges_taken.append(payload)
                if kind == "status" and payload.get("state") in {
                    "completed", "failed", "cancelled"
                }:
                    break

            print("\n== assertions ==")
            check("the start node ran first", path and path[0] == "build#1", str(path))
            check("the tester ran", any(p.startswith("test#") for p in path), str(path))
            check("the loop was taken at least once",
                  any(e.get("label") == "rework" for e in edges_taken),
                  str([e.get("label") for e in edges_taken]))
            check("build ran more than once (the loop actually looped)",
                  len([p for p in path if p.startswith("build#")]) >= 2, str(path))
            check("the walk ended on an END edge",
                  any(e.get("to") == "END" for e in edges_taken),
                  str([(e.get("label"), e.get("to")) for e in edges_taken]))

            fixed = (PROJECT / "calc.py").read_text()
            check("both bugs were fixed in the project",
          "a + b" in fixed and "a * b" in fixed,
                  fixed.strip().replace("\n", " "))

            detail = (await http.get(f"/api/runs/{run_id}")).json()
            print(f"  status={detail['run']['status']} exit={detail['run']['exit_reason']}")
            if detail["run"]["error_text"]:
                print(f"  error: {detail['run']['error_text'][:400]}")
            check("the run completed", detail["run"]["status"] == "completed",
                  detail["run"]["status"])
            check("the exit reason names the workflow",
                  str(detail["run"]["exit_reason"]).startswith("workflow_"),
                  str(detail["run"]["exit_reason"]))
            check("the graph is in the run snapshot",
                  detail["workflow"].get("name") == "Fix until green")
            cost = detail["run"]["total_cost_usd"]
            print(f"  cost: ${cost:.4f}" if cost else "  cost: unknown")

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    print(f"workdir: {WORK}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
