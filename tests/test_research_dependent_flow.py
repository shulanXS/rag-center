"""Tests for the two-stage dependent Research flow.

The dependent flow runs:
  1. Planner
  2. Premise round (queries targeting only premise aspects)
  3. Premise decider
  4. If all premises confirmed: answer round (queries targeting answer
     aspects, with the confirmed premise values implied via the
     retrieval)
  5. Decider across the merged pool
  6. Supplement if needed
  7. Finalizer

These tests exercise the planner query selection helpers and the
flow glue without needing a live LLM.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.research import PlannedQuery, ResearchAspect, ResearchState
from app.services.research_service import ResearchService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aspect(aspect_id: str, description: str, stage: str = "answer") -> ResearchAspect:
    return ResearchAspect(aspect_id=aspect_id, description=description, stage=stage)


def _query(query_id: str, query: str, aspect_ids: list[str]) -> PlannedQuery:
    return PlannedQuery(query_id=query_id, query=query, aspect_ids=aspect_ids)


def _planner_result(problem_type: str, aspects, queries):
    return SimpleNamespace(
        problem_type=problem_type,
        aspects=aspects,
        queries=queries,
        raw_output=None,
        model="m",
        latency_ms=0,
        degraded=False,
        error=None,
        usage={},
    )


def _service() -> ResearchService:
    """Build a ResearchService with all collaborators stubbed.

    The dependent flow never touches the underlying services because
    we are only validating the planner-output → query-split glue and
    the premise-confirmed gate.
    """
    return ResearchService(
        rag_service=MagicMock(),
        planner=MagicMock(),
        decider=MagicMock(),
        finalizer=MagicMock(),
        parallel_retrieval=MagicMock(),
        fusion=MagicMock(),
        supplement=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Query split helpers
# ---------------------------------------------------------------------------


def test_select_premise_queries_non_dependent_returns_all():
    s = _service()
    aspects = [_aspect("A1", "answer a"), _aspect("A2", "answer b")]
    queries = [_query("Q1", "q1", ["A1", "A2"])]
    result = s._select_premise_queries(
        _planner_result("simple", aspects, queries)
    )
    assert result == queries


def test_select_premise_queries_dependent_returns_only_premise_targets():
    s = _service()
    aspects = [
        _aspect("A1", "premise 1", "premise"),
        _aspect("A2", "premise 2", "premise"),
        _aspect("A3", "answer 1", "answer"),
    ]
    queries = [
        _query("Q1", "premise q1", ["A1"]),
        _query("Q2", "premise q2", ["A2"]),
        _query("Q3", "answer q3", ["A3"]),
    ]
    selected = s._select_premise_queries(_planner_result("dependent", aspects, queries))
    selected_ids = [q.query_id for q in selected]
    assert selected_ids == ["Q1", "Q2"]


def test_select_premise_queries_falls_back_to_all_when_no_premise_aspect():
    s = _service()
    aspects = [_aspect("A1", "answer", "answer")]
    queries = [_query("Q1", "q1", ["A1"])]
    selected = s._select_premise_queries(_planner_result("dependent", aspects, queries))
    # No premise aspects, so fall back to all queries (defensive)
    assert selected == queries


def test_select_answer_queries_returns_only_answer_targets():
    s = _service()
    aspects = [
        _aspect("A1", "premise 1", "premise"),
        _aspect("A2", "premise 2", "premise"),
        _aspect("A3", "answer 1", "answer"),
        _aspect("A4", "answer 2", "answer"),
    ]
    queries = [
        _query("Q1", "premise q1", ["A1"]),
        _query("Q2", "answer q3", ["A3"]),
        _query("Q3", "answer q3+4", ["A3", "A4"]),
    ]
    selected = s._select_answer_queries(_planner_result("dependent", aspects, queries))
    selected_ids = [q.query_id for q in selected]
    assert selected_ids == ["Q2", "Q3"]


def test_select_answer_queries_returns_empty_for_non_dependent():
    s = _service()
    aspects = [_aspect("A1", "answer", "answer")]
    queries = [_query("Q1", "q1", ["A1"])]
    assert s._select_answer_queries(_planner_result("simple", aspects, queries)) == []


# ---------------------------------------------------------------------------
# Premise confirmed gate
# ---------------------------------------------------------------------------


def test_all_premises_confirmed_true_when_every_premise_has_core():
    s = _service()
    state = ResearchState(
        research_id="r",
        original_query="q",
        tenant_id="t",
        kb_ids=["kb"],
        index_versions={},
        aspects=[
            _aspect("A1", "premise 1", "premise"),
            _aspect("A2", "answer 1", "answer"),
        ],
    )
    decider_result = SimpleNamespace(
        selections={
            "A1": [SimpleNamespace(role="core")],
            "A2": [SimpleNamespace(role="supporting")],
        }
    )
    assert s._all_premises_confirmed(decider_result, state) is True


def test_all_premises_confirmed_false_when_premise_only_supporting():
    s = _service()
    state = ResearchState(
        research_id="r",
        original_query="q",
        tenant_id="t",
        kb_ids=["kb"],
        index_versions={},
        aspects=[
            _aspect("A1", "premise 1", "premise"),
            _aspect("A2", "answer 1", "answer"),
        ],
    )
    decider_result = SimpleNamespace(
        selections={
            "A1": [SimpleNamespace(role="supporting")],  # not core
            "A2": [SimpleNamespace(role="core")],
        }
    )
    assert s._all_premises_confirmed(decider_result, state) is False


def test_all_premises_confirmed_true_when_no_premise_aspects():
    """Non-dependent flow has no premise aspects, so the gate is
    trivially satisfied."""
    s = _service()
    state = ResearchState(
        research_id="r",
        original_query="q",
        tenant_id="t",
        kb_ids=["kb"],
        index_versions={},
        aspects=[_aspect("A1", "answer", "answer")],
    )
    decider_result = SimpleNamespace(selections={})
    assert s._all_premises_confirmed(decider_result, state) is True
