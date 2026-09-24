"""
Software-team workflow as a LangGraph state graph.

    START -> manager_dispatch -> [architect | designer | tester] (parallel)
          -> manager_review --(issues)--> flagged roles only --> manager_review ...   (loop 1)
          -> engineer -> project_manager --(code_bug)--> engineer                      (loop 2)
                                          --(arch/design/test issue)--> that role --> loop 1
                                          --(accepted)--> END
    Both loops are capped; hitting a cap goes to human_escalation -> END.

Install:  pip install langgraph langchain-anthropic pytest
Env:      ANTHROPIC_API_KEY
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
MODEL = "claude-sonnet-5"      # change to any model you have access to
MAX_REVIEW_ROUNDS = 3          # loop 1 guard (manager <-> architect/designer/tester)
MAX_CODE_ROUNDS = 3            # loop 2 guard (project manager <-> engineer)

llm = ChatAnthropic(model=MODEL, temperature=0)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
class State(TypedDict, total=False):
    requirements: str

    # role outputs
    arch_doc: str
    design_doc: str
    test_cases: str            # a pytest file that does `from solution import *`
    code: str                  # contents of solution.py

    # loop 1
    review_feedback: dict[str, list[str]]   # {"architect": [...], "designer": [...], "tester": [...]}
    review_round: int
    approved: bool

    # loop 2
    pm_feedback: str
    failure_type: str          # none | code_bug | arch_issue | design_issue | test_issue
    code_round: int
    test_output: str

    status: str                # running | accepted | needs_human


# --------------------------------------------------------------------------- #
# Structured outputs
# --------------------------------------------------------------------------- #
class Issues(BaseModel):
    architect: list[str] = Field(default_factory=list)
    designer: list[str] = Field(default_factory=list)
    tester: list[str] = Field(default_factory=list)


class ReviewResult(BaseModel):
    approved: bool
    issues: Issues
    summary: str = ""


class PMReport(BaseModel):
    architecture_ok: bool = Field(description="Code follows the architecture document")
    design_ok: bool = Field(description="Code matches the design document")
    failure_type: Literal["none", "code_bug", "arch_issue", "design_issue", "test_issue"]
    feedback: str = Field(description="Concrete, actionable feedback for whoever must fix it")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def text_of(msg) -> str:
    c = msg.content
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") for b in c if isinstance(b, dict))


def strip_fences(s: str) -> str:
    m = re.search(r"```(?:python)?\n(.*?)```", s, re.DOTALL)
    return m.group(1) if m else s


def run_tests(code: str, tests: str, timeout: int = 60) -> tuple[bool, str]:
    """Run the tester's pytest file against the engineer's code.
    WARNING: this executes LLM-written code. Run inside a container/sandbox."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, "solution.py").write_text(code)
        Path(d, "test_solution.py").write_text(tests)
        try:
            p = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--tb=short"],
                cwd=d, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, "Test run timed out."
        return p.returncode == 0, (p.stdout + p.stderr)[-4000:]


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def manager_dispatch(state: State) -> State:
    """Entry point: initialise counters. Fan-out happens in the conditional edge."""
    return {
        "review_feedback": {}, "review_round": 0, "code_round": 0,
        "approved": False, "status": "running",
    }


def make_role_node(role: str, state_key: str, system_prompt: str):
    """Factory for architect / designer / tester. On re-entry the role sees its
    previous output plus the manager's feedback addressed to it."""
    def node(state: State) -> State:
        prompt = f"Project requirements:\n{state['requirements']}\n"
        feedback = state.get("review_feedback", {}).get(role, [])
        if feedback:
            prompt += (
                f"\nYour previous output:\n{state.get(state_key, '')}\n\n"
                "Fix these points and return the complete updated version:\n- "
                + "\n- ".join(feedback)
            )
        out = text_of(llm.invoke([SystemMessage(system_prompt), HumanMessage(prompt)]))
        if role == "tester":
            out = strip_fences(out)
        return {state_key: out}
    return node


architect = make_role_node(
    "architect", "arch_doc",
    "You are a software architect. Produce an architecture document: components, "
    "responsibilities, interfaces, data flow, technology choices.",
)
designer = make_role_node(
    "designer", "design_doc",
    "You are a software designer. Produce a detailed design: modules, classes/functions, "
    "signatures, data models, error handling. Assume a single Python module `solution.py`.",
)
tester = make_role_node(
    "tester", "test_cases",
    "You are a software tester. Output ONLY a runnable pytest file. It must start with "
    "`from solution import *` and cover normal cases, edge cases and error cases.",
)


def manager_review(state: State) -> State:
    """Fan-in: review the three deliverables together (also checks cross-consistency)."""
    reviewer = llm.with_structured_output(ReviewResult)
    result: ReviewResult = reviewer.invoke([
        SystemMessage(
            "You are a software manager. Review the architecture, design and test cases "
            "against the requirements AND for consistency with each other. Approve only if "
            "all three are acceptable; otherwise list specific issues per role."
        ),
        HumanMessage(
            f"Requirements:\n{state['requirements']}\n\n"
            f"ARCHITECTURE:\n{state['arch_doc']}\n\n"
            f"DESIGN:\n{state['design_doc']}\n\n"
            f"TEST CASES:\n{state['test_cases']}"
        ),
    ])
    return {
        "approved": result.approved,
        "review_feedback": {
            "architect": result.issues.architect,
            "designer": result.issues.designer,
            "tester": result.issues.tester,
        },
        "review_round": state.get("review_round", 0) + 1,
    }


def engineer(state: State) -> State:
    prompt = (
        f"Requirements:\n{state['requirements']}\n\n"
        f"ARCHITECTURE:\n{state['arch_doc']}\n\n"
        f"DESIGN:\n{state['design_doc']}\n\n"
        f"TEST CASES (your code must pass these):\n{state['test_cases']}\n"
    )
    if state.get("pm_feedback"):
        prompt += (
            f"\nYour previous code:\n{state.get('code', '')}\n\n"
            f"Project manager feedback / test output to address:\n{state['pm_feedback']}\n"
        )
    out = text_of(llm.invoke([
        SystemMessage("You are a software engineer. Output ONLY the complete contents of "
                      "solution.py, nothing else."),
        HumanMessage(prompt),
    ]))
    return {"code": strip_fences(out)}


def project_manager(state: State) -> State:
    """Real test execution + LLM check of architecture/design conformance."""
    tests_ok, test_output = run_tests(state["code"], state["test_cases"])

    pm = llm.with_structured_output(PMReport)
    report: PMReport = pm.invoke([
        SystemMessage(
            "You are a project manager checking delivered code. Decide whether the code "
            "follows the architecture and the design. If something is wrong, classify who "
            "must fix it: code_bug, arch_issue, design_issue or test_issue (a wrong or "
            "unreasonable test case). Use 'none' only if everything is fine."
        ),
        HumanMessage(
            f"ARCHITECTURE:\n{state['arch_doc']}\n\nDESIGN:\n{state['design_doc']}\n\n"
            f"CODE:\n{state['code']}\n\nTEST RESULT (passed={tests_ok}):\n{test_output}"
        ),
    ])

    accepted = tests_ok and report.architecture_ok and report.design_ok
    failure = "none" if accepted else (
        report.failure_type if report.failure_type != "none" else "code_bug"
    )
    update: State = {
        "test_output": test_output,
        "failure_type": failure,
        "code_round": state.get("code_round", 0) + 1,
        "status": "accepted" if accepted else "running",
        "pm_feedback": "",
    }
    if failure == "code_bug":
        update["pm_feedback"] = f"{report.feedback}\n\nTest output:\n{test_output}"
    elif failure in ("arch_issue", "design_issue", "test_issue"):
        # Send the problem back upstream through loop 1 with a fresh review budget.
        role = {"arch_issue": "architect", "design_issue": "designer", "test_issue": "tester"}[failure]
        update["review_feedback"] = {role: [report.feedback]}
        update["review_round"] = 0
    return update


def human_escalation(state: State) -> State:
    return {"status": "needs_human"}


# --------------------------------------------------------------------------- #
# Routers (conditional edges)
# --------------------------------------------------------------------------- #
def route_dispatch(state: State) -> list[str]:
    return ["architect", "designer", "tester"]          # parallel fan-out


def route_after_review(state: State):
    flagged = [r for r, items in state.get("review_feedback", {}).items() if items]
    if state.get("approved") or not flagged:
        return "engineer"
    if state["review_round"] >= MAX_REVIEW_ROUNDS:
        return "human_escalation"
    return flagged                                       # only roles with issues re-run


def route_after_pm(state: State) -> str:
    if state["status"] == "accepted":
        return END
    if state["code_round"] >= MAX_CODE_ROUNDS:
        return "human_escalation"
    return {
        "code_bug": "engineer",
        "arch_issue": "architect",
        "design_issue": "designer",
        "test_issue": "tester",
    }[state["failure_type"]]


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
def build_graph():
    g = StateGraph(State)

    g.add_node("manager_dispatch", manager_dispatch)
    g.add_node("architect", architect)
    g.add_node("designer", designer)
    g.add_node("tester", tester)
    g.add_node("manager_review", manager_review)
    g.add_node("engineer", engineer)
    g.add_node("project_manager", project_manager)
    g.add_node("human_escalation", human_escalation)

    g.add_edge(START, "manager_dispatch")
    g.add_conditional_edges("manager_dispatch", route_dispatch,
                            ["architect", "designer", "tester"])

    # Each role reports to the manager. Roles that run in the same step are
    # joined automatically: manager_review runs once, after all of them finish.
    # (Don't use add_edge([a, d, t], ...) here: it waits for ALL three, which
    # would deadlock when only some roles re-run after feedback.)
    for role in ("architect", "designer", "tester"):
        g.add_edge(role, "manager_review")

    g.add_conditional_edges(
        "manager_review", route_after_review,
        ["architect", "designer", "tester", "engineer", "human_escalation"],
    )

    g.add_edge("engineer", "project_manager")
    g.add_conditional_edges(
        "project_manager", route_after_pm,
        ["engineer", "architect", "designer", "tester", "human_escalation", END],
    )
    g.add_edge("human_escalation", END)

    return g.compile()


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    graph = build_graph()

    requirements = (
        "Build a Python module with a function `fizzbuzz(n: int) -> list[str]` that returns "
        "the FizzBuzz sequence from 1 to n, and raises ValueError if n < 1."
    )

    final = graph.invoke({"requirements": requirements}, config={"recursion_limit": 60})

    print("STATUS:", final["status"])
    print("\n--- CODE ---\n", final.get("code", ""))
    print("\n--- LAST TEST OUTPUT ---\n", final.get("test_output", ""))