"""Workflows: a directed graph of roles describing how a team works.

A node is a role doing one step. An edge is a transition, optionally guarded by a
condition; an edge pointing at nothing (``to_node_id is NULL``) ends the workflow.
Because an edge may point back at an earlier node, loops are expressible — so every
node carries a visit budget and every workflow a step budget.

Execution follows LangGraph's superstep model (see docs/example_graph.py): a *set* of
nodes is active at once, so a node may fan out to several successors in parallel, and
several branches converging on one node join there and run it once. That is why edges
are evaluated as a group rather than picked one at a time.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

#: Reserved edge target meaning "stop here and report".
END = "END"


class Workflow(Base, TimestampMixin):
    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # Hard ceiling on supersteps for one run, whatever the graph says.
    max_steps: Mapped[int] = mapped_column(Integer, default=20)
    #: Where to go when a budget runs out, instead of failing the run. Mirrors the
    #: example graph's ``human_escalation`` node. NULL means "fail".
    escalation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    nodes: Mapped[list["WorkflowNode"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowNode.position",
        lazy="selectin",
        foreign_keys="WorkflowNode.workflow_id",
    )
    edges: Mapped[list["WorkflowEdge"]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowEdge.position",
        lazy="selectin",
        foreign_keys="WorkflowEdge.workflow_id",
    )


class WorkflowNode(Base):
    __tablename__ = "workflow_nodes"
    __table_args__ = (UniqueConstraint("workflow_id", "key", name="uq_workflow_node_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    #: Short stable identifier used in edges and in routing decisions.
    key: Mapped[str] = mapped_column(String(64))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id", ondelete="RESTRICT"))
    #: What this role should do at this step, on top of its own system prompt.
    instructions: Mapped[str] = mapped_column(Text, default="")
    #: Name this step's output is filed under, so later steps can refer to it the way
    #: the example graph reads ``state["arch_doc"]``. Defaults to the node key.
    output_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: JSON Schema this step's result must match — the equivalent of the example graph's
    #: ``with_structured_output(ReviewResult)``. NULL means free-form prose.
    output_schema_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: Which earlier results this step is shown, as ``{"from", "path", "as"}`` entries.
    #: Empty means "everything produced so far".
    inputs_json: Mapped[list] = mapped_column(JSON, default=list)
    is_start: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Loop budget: how many times this node may run in a single workflow run.
    max_visits: Mapped[int] = mapped_column(Integer, default=3)
    position: Mapped[int] = mapped_column(Integer, default=0)
    #: Canvas coordinates. NULL means "lay this one out automatically", so a graph
    #: built before the editor existed still renders sensibly.
    pos_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    pos_y: Mapped[float | None] = mapped_column(Float, nullable=True)

    workflow: Mapped[Workflow] = relationship(
        back_populates="nodes", foreign_keys=[workflow_id]
    )
    role = relationship("Role", lazy="selectin")


class WorkflowEdge(Base):
    __tablename__ = "workflow_edges"
    __table_args__ = (
        UniqueConstraint("from_node_id", "label", name="uq_workflow_edge_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    from_node_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_nodes.id", ondelete="CASCADE"), index=True
    )
    #: NULL means this edge finishes the workflow.
    to_node_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_nodes.id", ondelete="CASCADE"), nullable=True
    )
    #: Short name the router picks by, e.g. "needs_rework".
    label: Mapped[str] = mapped_column(String(64))
    #: When this transition applies, in words, judged by a model. Empty means
    #: unconditional.
    condition: Mapped[str] = mapped_column(Text, default="")
    #: A deterministic test over the source step's structured result, e.g.
    #: ``issues contains architecture``. Evaluated by the harness — no model call.
    expression: Mapped[str] = mapped_column(Text, default="")
    #: Taken when no condition matches. At most one per source node.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Node keys whose visit budget is reset when this edge is taken. The example
    #: graph does this when the project manager sends work back upstream: the review
    #: loop starts again with a fresh budget.
    resets_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    position: Mapped[int] = mapped_column(Integer, default=0)

    workflow: Mapped[Workflow] = relationship(
        back_populates="edges", foreign_keys=[workflow_id]
    )
    from_node = relationship("WorkflowNode", foreign_keys=[from_node_id], lazy="selectin")
    to_node = relationship("WorkflowNode", foreign_keys=[to_node_id], lazy="selectin")
