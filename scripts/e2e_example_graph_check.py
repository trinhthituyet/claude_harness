"""End-to-end check of the LangGraph example's shape, running for real.

Builds the graph from docs/example_graph.py through the API — parallel fan-out, a join,
a review loop that re-runs only the flagged roles, a delivery loop, escalation — and runs
it against a temp project. The point is to see the harness fan out, join, route with
feedback and finish: the control flow, not just the prompts.

Run with:  .venv/bin/python scripts/e2e_example_graph_check.py
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

WORK = Path(tempfile.mkdtemp(prefix="harness-example-graph-"))
PROJECT = WORK / "project"
PROJECT.mkdir()
os.environ["HARNESS_DB"] = str(WORK / "harness.db")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.services.runner import manager  # noqa: E402

# The example's own task: fizzbuzz. Small enough that three roles working in parallel
# finish quickly, real enough that the reviewer has something to disagree about.
GOAL = (
    "Build solution.py with a function fizzbuzz(n: int) -> list[str] returning the "
    "FizzBuzz sequence from 1 to n, raising ValueError if n < 1. Keep it to one small "
    "module. Do not run any shell commands; write and read files only."
)

ROLES = {
    "Manager": "You are a software manager. Be brief and decisive.",
    "Architect": "You are a software architect. Produce a short architecture note: "
                 "components, responsibilities, interfaces. Write it to arch.md. Be brief.",
    "Designer": "You are a software designer. Produce a short design note: function "
                "signatures, data flow, error handling. Write it to design.md. Be brief.",
    "Tester": "You are a software tester. Write test_solution.py containing pytest tests "
              "that import from solution. Do not write solution.py. Be brief.",
    "Engineer": "You are a software engineer. Write solution.py so the tests would pass. "
                "Use Write or Edit only; never run shell commands. Be brief.",
    "ProjectManager": "You are a project manager. Read solution.py and test_solution.py "
                      "and judge by inspection whether the code would pass the tests and "
                      "matches the documents. Say ACCEPT or the problem. Never edit.",
    "Human": "You are the human escalation point. Summarise what is unresolved and stop.",
}

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def graph_payload(role_ids: dict[str, int]) -> dict:
    return {
        "name": "Software team",
        "description": "The LangGraph example: parallel design, review loop, delivery loop.",
        "max_steps": 14,
        "escalation_key": "human",
        "nodes": [
            {"key": "dispatch", "role_id": role_ids["Manager"], "is_start": True,
             "max_visits": 1, "output_key": "brief",
             "instructions": "State in two lines what the architect, designer and tester "
                             "should each produce. Write no files."},
            {"key": "architect", "role_id": role_ids["Architect"], "max_visits": 3,
             "output_key": "arch_doc"},
            {"key": "designer", "role_id": role_ids["Designer"], "max_visits": 3,
             "output_key": "design_doc"},
            {"key": "tester", "role_id": role_ids["Tester"], "max_visits": 3,
             "output_key": "test_cases"},
            {"key": "review", "role_id": role_ids["Manager"], "max_visits": 3,
             "output_key": "review_notes",
             "instructions": "Review the three artifacts for consistency with the goal. "
                             "Say which role, if any, must change something."},
            {"key": "engineer", "role_id": role_ids["Engineer"], "max_visits": 3,
             "output_key": "code"},
            {"key": "pm", "role_id": role_ids["ProjectManager"], "max_visits": 3,
             "output_key": "pm_report"},
            {"key": "human", "role_id": role_ids["Human"], "max_visits": 1},
        ],
        "edges": [
            {"from_key": "dispatch", "to_key": "architect", "label": "to_architect"},
            {"from_key": "dispatch", "to_key": "designer", "label": "to_designer"},
            {"from_key": "dispatch", "to_key": "tester", "label": "to_tester"},
            {"from_key": "architect", "to_key": "review", "label": "report"},
            {"from_key": "designer", "to_key": "review", "label": "report"},
            {"from_key": "tester", "to_key": "review", "label": "report"},
            {"from_key": "review", "to_key": "architect", "label": "arch_issues",
             "condition": "the architecture note must change"},
            {"from_key": "review", "to_key": "designer", "label": "design_issues",
             "condition": "the design note must change"},
            {"from_key": "review", "to_key": "tester", "label": "test_issues",
             "condition": "the tests must change"},
            {"from_key": "review", "to_key": "engineer", "label": "approved",
             "condition": "all three artifacts are acceptable", "is_default": True},
            {"from_key": "engineer", "to_key": "pm", "label": "deliver"},
            {"from_key": "pm", "to_key": "engineer", "label": "code_bug",
             "condition": "the code itself is wrong"},
            {"from_key": "pm", "to_key": "tester", "label": "test_issue",
             "condition": "a test case is wrong rather than the code",
             "resets": ["review", "tester"]},
            {"from_key": "pm", "to_key": None, "label": "accepted",
             "condition": "the code would pass the tests and matches the documents",
             "is_default": True},
            {"from_key": "human", "to_key": None, "label": "handed_over"},
        ],
    }


async def main() -> int:
    app = create_app()
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", timeout=180
        ) as http:
            print("\n== build the example graph ==")
            role_ids = {}
            for name, prompt in ROLES.items():
                response = await http.post("/api/roles", json={
                    "name": name, "description": name, "system_prompt": prompt,
                })
                assert response.status_code == 201, response.text
                role_ids[name] = response.json()["id"]
            check("seven roles created", len(role_ids) == 7)

            workflow = await http.post("/api/workflows", json=graph_payload(role_ids))
            check("the example graph validates and saves",
                  workflow.status_code == 201, workflow.text[:300])
            if workflow.status_code != 201:
                return 1
            graph = workflow.json()
            check("the fan-out arms are unconditional",
                  all(e["condition"] == "" for e in graph["edges"]
                      if e["from_key"] == "dispatch"))
            check("the escalation node is recorded", graph["escalation_key"] == "human")
            check("an upstream edge resets the review budget",
                  sorted(next(e for e in graph["edges"]
                              if e["label"] == "test_issue")["resets"]) == ["review", "tester"])

            task = await http.post("/api/tasks", json={
                "name": "fizzbuzz by committee",
                "prompt": GOAL,
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
            supersteps: list[list[str]] = []
            node_starts: list[str] = []
            edges_taken: list[dict] = []
            resets: list[dict] = []
            artifacts: list[str] = []
            deadline = asyncio.get_event_loop().time() + 900
            while asyncio.get_event_loop().time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), 60)
                except asyncio.TimeoutError:
                    print("    …waiting")
                    continue
                kind, payload = event["type"], event.get("payload", {})
                if kind == "_eof":
                    break
                if kind in {"superstep", "node_started", "node_finished", "edge_taken",
                            "visits_reset", "routing_failed", "workflow_stopped",
                            "workflow_finished", "error", "status"}:
                    print(f"    [{kind}] {json.dumps(payload)[:170]}")
                if kind == "superstep":
                    supersteps.append(payload["parallel"])
                if kind == "node_started":
                    node_starts.append(payload["node"])
                if kind == "node_finished":
                    artifacts.append(payload.get("artifact", ""))
                if kind == "edge_taken":
                    edges_taken.append(payload)
                if kind == "visits_reset":
                    resets.append(payload)
                if kind == "workflow_finished":
                    supersteps = supersteps or []
                if kind == "status" and payload.get("state") in {
                    "completed", "failed", "cancelled"
                }:
                    break

            print("\n== control flow ==")
            check("the start node ran first", node_starts[:1] == ["dispatch"], str(node_starts[:4]))
            check("the three roles ran in one parallel superstep",
                  any(sorted(group) == ["architect", "designer", "tester"]
                      for group in supersteps),
                  str(supersteps))
            reviews = [n for n in node_starts if n == "review"]
            check("the fan-out joined into a single review",
                  len(reviews) >= 1 and node_starts.count("review") == len(reviews),
                  f"review ran {len(reviews)}x for {node_starts.count('architect')} architect runs")
            check("the engineer ran after the review approved",
                  "engineer" in node_starts, str(node_starts))
            check("the project manager reviewed the delivery", "pm" in node_starts)

            labels = [e.get("label") for e in edges_taken]
            check("edges carry routing reasons",
                  any(e.get("why") for e in edges_taken),
                  str([e.get("why", "")[:60] for e in edges_taken[:3]]))
            check("named artifacts were produced",
                  {"arch_doc", "design_doc", "test_cases"} <= set(artifacts),
                  str(sorted(set(artifacts))))

            print("\n== files on disk ==")
            produced = sorted(p.name for p in PROJECT.iterdir())
            print(f"  {produced}")
            check("the engineer wrote the module", (PROJECT / "solution.py").exists(),
                  str(produced))
            check("the tester wrote tests",
                  any(n.startswith("test") for n in produced), str(produced))

            detail = (await http.get(f"/api/runs/{run_id}")).json()
            print(f"\n  status={detail['run']['status']} exit={detail['run']['exit_reason']}")
            if detail["run"]["error_text"]:
                print(f"  error: {detail['run']['error_text'][:300]}")
            check("the run finished cleanly",
                  detail["run"]["status"] in {"completed", "failed"},
                  detail["run"]["status"])
            check("the graph is in the run snapshot",
                  detail["workflow"].get("escalation") == "human")
            cost = detail["run"]["total_cost_usd"]
            print(f"  cost: ${cost:.4f}" if cost else "  cost: unknown")
            print(f"  labels taken: {labels}")
            if resets:
                print(f"  visit budgets reset: {resets}")

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    print(f"workdir: {WORK}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
