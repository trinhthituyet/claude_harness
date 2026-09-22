"""End-to-end check against the real Agent SDK.

Drives the actual HTTP API (via ASGI transport, no port binding) through:
create role -> team -> task -> run, streams the session's events, answers the
approval prompt that an out-of-root write triggers, and asserts what landed on disk.

Run with:  .venv/bin/python scripts/e2e_check.py
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

WORKDIR = Path(tempfile.mkdtemp(prefix="harness-e2e-"))
PROJECT = WORKDIR / "project"
OUTSIDE = WORKDIR / "outside"
PROJECT.mkdir()
OUTSIDE.mkdir()
os.environ["HARNESS_DB"] = str(WORKDIR / "harness.db")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.services.runner import manager  # noqa: E402

PROMPT = (
    "Do exactly these two steps, reporting each outcome, then stop.\n"
    f"1. Use the Write tool to write the word hello to {PROJECT}/notes.txt\n"
    f"2. Use the Write tool to write the word hello to {OUTSIDE}/evil.txt\n"
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


async def main() -> int:
    app = create_app()
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test", timeout=60) as http:
            print("\n== setup ==")
            role = await http.post("/api/roles", json={
                "name": "Engineer",
                "description": "Implements the change.",
                "system_prompt": "You are a careful engineer. Follow the steps literally.",
            })
            check("create role", role.status_code == 201, role.text[:160])
            role_id = role.json()["id"]

            team = await http.post("/api/teams", json={
                "name": "Solo", "members": [{"role_id": role_id, "is_lead": True}],
            })
            check("create team", team.status_code == 201, team.text[:160])

            task = await http.post("/api/tasks", json={
                "name": "e2e",
                "prompt": PROMPT,
                "project_path": str(PROJECT),
                "team_id": team.json()["id"],
                # The OS sandbox cannot nest inside an existing sandbox; this script
                # may itself run inside one, so Bash confinement is turned off here.
                # Real runs should leave it on.
                "sandbox_bash": False,
                "approval_timeout_s": 60,
                "max_turns": 6,
            })
            check("create task", task.status_code == 201, task.text[:200])
            if task.status_code != 201:
                return 1
            task_id = task.json()["id"]

            bad_path = await http.get("/api/fs/validate", params={"path": "/nope/nope"})
            check("path validation rejects a missing directory", bad_path.json()["ok"] is False)

            print("\n== run ==")
            started = await http.post(f"/api/tasks/{task_id}/run")
            check("trigger run", started.status_code == 202, started.text[:160])
            run_id = started.json()["run_id"]
            print(f"  run_id {run_id}")

            run = manager.get(run_id)
            queue = run.bus.subscribe()

            approvals_seen = 0
            deadline = asyncio.get_event_loop().time() + 180
            while asyncio.get_event_loop().time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), 10)
                except asyncio.TimeoutError:
                    continue
                kind, payload = event["type"], event.get("payload", {})
                if kind == "_eof":
                    break
                summary = json.dumps(payload)
                print(f"  [{kind}] {summary[:190]}")
                if kind == "permission_request":
                    approvals_seen += 1
                    answered = await http.post(
                        f"/api/runs/{run_id}/approvals/{payload['id']}",
                        json={"approved": False, "reason": "denied by the e2e check"},
                    )
                    check("answer the approval prompt", answered.status_code == 200,
                          answered.text[:160])
                if kind == "status" and payload.get("state") in {
                    "completed", "failed", "cancelled"
                }:
                    break

            print("\n== assertions ==")
            check("an out-of-root write asked for approval", approvals_seen >= 1,
                  f"{approvals_seen} prompt(s)")
            check("in-root file was written", (PROJECT / "notes.txt").exists())
            check("out-of-root file was NOT written", not (OUTSIDE / "evil.txt").exists())

            detail = (await http.get(f"/api/runs/{run_id}")).json()
            print(f"  status={detail['run']['status']} exit={detail['run']['exit_reason']}")
            if detail["run"]["error_text"]:
                print(f"  error: {detail['run']['error_text'][:400]}")

            check("run finished", detail["run"]["status"] in {"completed", "failed", "cancelled"},
                  detail["run"]["status"])
            check("events were persisted", len(detail["events"]) > 3,
                  f"{len(detail['events'])} events")
            check("permission decisions were audited", len(detail["decisions"]) > 0,
                  f"{len(detail['decisions'])} decisions")
            denied = [d for d in detail["decisions"] if d["decision"] == "deny"]
            asked = [d for d in detail["decisions"] if d["decision"] == "ask"]
            check("the out-of-root write is recorded as ask and/or deny",
                  bool(denied or asked), f"{len(asked)} ask, {len(denied)} deny")

            options = detail["options"]
            check("allowed_tools stayed empty", options.get("allowed_tools") == [])
            # The project root is realpath'd, so on macOS /var/... becomes /private/var/...
            check("cwd is the resolved project root",
                  options.get("cwd") == str(Path(os.path.realpath(PROJECT))),
                  options.get("cwd"))
            check("setting_sources is isolated", options.get("setting_sources") == [])
            check("strict_mcp_config is on", options.get("strict_mcp_config") is True)
            check("network tools disallowed", set(options.get("disallowed_tools") or [])
                  == {"WebFetch", "WebSearch"})

            events = (await http.get(f"/api/runs/{run_id}/events")).text
            check("finished runs replay over SSE", "_eof" in events)

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    print(f"workdir: {WORKDIR}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
