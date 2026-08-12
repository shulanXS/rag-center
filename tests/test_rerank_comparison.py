import math

import pytest

from scripts.compare_rerankers import (
    _build_run_report,
    case_ndcg,
    compare_reports,
    compute_ranking_metrics,
    percentile,
    render_markdown,
    validate_dataset,
)


def _dataset() -> dict:
    return {
        "name": "fixture",
        "candidate_top_k": 4,
        "result_top_n": 3,
        "cases": [
            {
                "id": "case-1",
                "question": "问题",
                "candidates": [
                    {
                        "chunk_id": "core",
                        "document_id": "doc-1",
                        "title": "核心",
                        "content": "核心证据",
                        "score": 0.4,
                        "relevance": 3,
                    },
                    {
                        "chunk_id": "support",
                        "document_id": "doc-2",
                        "title": "支撑",
                        "content": "支撑证据",
                        "score": 0.3,
                        "relevance": 2,
                    },
                    {
                        "chunk_id": "topic",
                        "document_id": "doc-3",
                        "title": "相关",
                        "content": "主题相关",
                        "score": 0.2,
                        "relevance": 1,
                    },
                    {
                        "chunk_id": "noise",
                        "document_id": "doc-4",
                        "title": "无关",
                        "content": "无关",
                        "score": 0.1,
                        "relevance": 0,
                    },
                ],
            }
        ],
    }


def _report(provider: str, ranking: list[str], latency: float = 10.0) -> dict:
    return {
        "dataset": "fixture",
        "dataset_created_at": None,
        "provider": provider,
        "model": provider,
        "summary": {
            "p50_latency_ms": latency,
            "p95_latency_ms": latency,
            "success_rate": 1.0,
            "degraded_rate": 0.0,
        },
        "cases": [
            {
                "id": "case-1",
                "question": "问题",
                "runs": [
                    {
                        "success": True,
                        "rankings": [
                            {"chunk_id": chunk_id, "rerank_score": 1.0 - index * 0.1}
                            for index, chunk_id in enumerate(ranking)
                        ],
                    }
                ],
            }
        ],
    }


def test_validate_dataset_accepts_complete_labels() -> None:
    result = validate_dataset(_dataset())
    assert result["valid"] is True
    assert result["valid_case_count"] == 1
    assert result["relevance_counts"] == {"0": 1, "1": 1, "2": 1, "3": 1}
    assert result["warnings"]


def test_validate_dataset_rejects_missing_relevance() -> None:
    dataset = _dataset()
    dataset["cases"][0]["candidates"][0]["relevance"] = None
    result = validate_dataset(dataset)
    assert result["valid"] is False
    assert any("relevance" in error for error in result["errors"])


def test_percentile_uses_linear_interpolation() -> None:
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([10], 0.95) == 10
    assert percentile([], 0.95) is None


def test_ranking_metrics_use_separate_relevance_thresholds() -> None:
    metrics = compute_ranking_metrics(
        _dataset(),
        {"case-1": ["support", "core", "topic"]},
    )
    assert metrics["mrr@5"] == pytest.approx(0.5)
    assert metrics["hit_rate@5"] == pytest.approx(1.0)
    assert metrics["recall@5"] == pytest.approx(1.0)
    assert 0 < metrics["ndcg@5"] < 1


def test_core_and_evidence_thresholds_are_not_mixed() -> None:
    dataset = _dataset()
    dataset["cases"][0]["candidates"].append(
        {
            "chunk_id": "support-2",
            "document_id": "doc-5",
            "title": "支撑2",
            "content": "支撑证据2",
            "score": 0.1,
            "relevance": 2,
        }
    )
    metrics = compute_ranking_metrics(
        dataset,
        {"case-1": ["support", "support-2", "topic"]},
    )
    assert metrics["mrr@5"] == pytest.approx(0.0)
    assert metrics["hit_rate@5"] == pytest.approx(0.0)
    assert metrics["recall@5"] == pytest.approx(2 / 3)


def test_case_ndcg_penalizes_core_evidence_moving_down() -> None:
    case = _dataset()["cases"][0]
    ideal = case_ndcg(case, ["core", "support", "topic"])
    worse = case_ndcg(case, ["noise", "topic", "core"])
    assert ideal == pytest.approx(1.0)
    assert worse < ideal


def test_compare_reports_records_win_and_changes() -> None:
    comparison = compare_reports(
        _dataset(),
        _report("llm", ["noise", "topic", "core"], latency=100.0),
        _report("qwen3", ["core", "support", "topic"], latency=20.0),
    )
    assert comparison["outcomes"] == {"win": 1, "tie": 0, "loss": 0, "missing": 0}
    assert comparison["changes"]["ndcg@5"]["absolute_change"] > 0
    assert math.isclose(comparison["changes"]["p95_latency_ms"]["absolute_change"], -80.0)
    assert comparison["actual_candidates_per_case"] == {"min": 4, "max": 4}


def test_markdown_reports_actual_candidates_and_annotation_status() -> None:
    dataset = _dataset()
    dataset["annotation"] = {
        "method": "model_assisted",
        "status": "pending_human_review",
    }
    comparison = compare_reports(
        dataset,
        _report("llm", ["core", "support", "topic"]),
        _report("qwen3", ["core", "support", "topic"]),
    )

    markdown = render_markdown(comparison)

    assert "实际候选数/Case：4" in markdown
    assert "标注方式：model_assisted" in markdown
    assert "标注状态：pending_human_review" in markdown


def test_report_excludes_warmup_from_latency_percentiles() -> None:
    report = _build_run_report(
        dataset=_dataset(),
        provider="qwen3",
        model="qwen3-rerank",
        base_url_host="example.com",
        repeat=3,
        warmup=1,
        case_results=[
            {
                "id": "case-1",
                "question": "问题",
                "warmups": [{"success": True, "latency_ms": 9999, "rankings": []}],
                "runs": [
                    {"success": True, "latency_ms": 10, "rankings": []},
                    {"success": True, "latency_ms": 20, "rankings": []},
                    {"success": True, "latency_ms": 30, "rankings": []},
                ],
            }
        ],
    )
    assert report["summary"]["p50_latency_ms"] == 20
    assert report["summary"]["p95_latency_ms"] == pytest.approx(29)
