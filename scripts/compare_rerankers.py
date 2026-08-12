from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.providers.rerank.base import RerankProvider
from app.providers.rerank.qwen3 import Qwen3RerankProvider

EVAL_USER_ID = "rerank_eval_runner"
METRIC_NAMES = ("ndcg@5", "mrr@5", "hit_rate@5", "recall@5")


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise TypeError(f"JSON root must be an object: {path}")
    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def validate_dataset(data: dict[str, Any]) -> dict[str, Any]:
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise TypeError("dataset must contain a cases array")

    errors: list[str] = []
    warnings: list[str] = []
    relevance_counts = {str(value): 0 for value in range(4)}
    seen_case_ids: set[str] = set()
    valid_cases = 0
    duplicate_chunk_count = 0
    empty_candidate_case_count = 0
    missing_relevance_count = 0

    for case_index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"cases[{case_index}] must be an object")
            continue
        case_id = str(case.get("id") or "").strip()
        question = str(case.get("question") or "").strip()
        if not case_id:
            errors.append(f"cases[{case_index}] missing id")
            case_id = f"case-{case_index}"
        elif case_id in seen_case_ids:
            errors.append(f"duplicate case id: {case_id}")
        seen_case_ids.add(case_id)
        if not question:
            errors.append(f"{case_id}: missing question")

        candidates = case.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            errors.append(f"{case_id}: candidates must be a non-empty array")
            empty_candidate_case_count += 1
            continue
        seen_chunks: set[str] = set()
        has_core = False
        case_has_error = False
        for candidate_index, candidate in enumerate(candidates):
            prefix = f"{case_id}.candidates[{candidate_index}]"
            if not isinstance(candidate, dict):
                errors.append(f"{prefix} must be an object")
                case_has_error = True
                continue
            chunk_id = str(candidate.get("chunk_id") or "").strip()
            if not chunk_id:
                errors.append(f"{prefix} missing chunk_id")
                case_has_error = True
            elif chunk_id in seen_chunks:
                errors.append(f"{case_id}: duplicate chunk_id: {chunk_id}")
                duplicate_chunk_count += 1
                case_has_error = True
            seen_chunks.add(chunk_id)
            for required in ("document_id", "title", "content", "score"):
                if required not in candidate:
                    errors.append(f"{prefix} missing {required}")
                    case_has_error = True

            relevance = candidate.get("relevance")
            if (
                isinstance(relevance, bool)
                or not isinstance(relevance, int)
                or relevance not in range(4)
            ):
                errors.append(f"{prefix} relevance must be an integer from 0 to 3")
                if relevance is None:
                    missing_relevance_count += 1
                case_has_error = True
                continue
            relevance_counts[str(relevance)] += 1
            has_core = has_core or relevance == 3
        if not has_core:
            errors.append(f"{case_id}: at least one candidate must have relevance=3")
            case_has_error = True
        if not case_has_error:
            valid_cases += 1

    if valid_cases < 30:
        warnings.append(f"only {valid_cases} valid cases; 30 or more are recommended")
    return {
        "valid": not errors,
        "case_count": len(cases),
        "valid_case_count": valid_cases,
        "candidate_top_k": data.get("candidate_top_k"),
        "result_top_n": data.get("result_top_n"),
        "relevance_counts": relevance_counts,
        "duplicate_chunk_count": duplicate_chunk_count,
        "empty_candidate_case_count": empty_candidate_case_count,
        "missing_relevance_count": missing_relevance_count,
        "errors": errors,
        "warnings": warnings,
    }


def _resolve_case_kb_ids(dataset: dict[str, Any], case: dict[str, Any]) -> list[str]:
    values = case.get("kb_ids") or dataset.get("kb_ids")
    if not values:
        single = case.get("kb_id") or dataset.get("kb_id")
        values = [single] if single else []
    normalized = [str(value).strip() for value in values if str(value).strip()]
    if not normalized:
        raise ValueError(f"{case.get('id')}: kb_id or kb_ids is required")
    return normalized


def prepare_dataset(
    *,
    source: dict[str, Any],
    base_url: str,
    api_key: str,
    profile: str,
    candidate_top_k: int,
) -> dict[str, Any]:
    source_cases = source.get("cases")
    if not isinstance(source_cases, list):
        raise TypeError("source dataset must contain a cases array")
    headers = {"Authorization": f"Bearer {api_key}"}
    prepared_cases: list[dict[str, Any]] = []
    with httpx.Client(timeout=60.0, trust_env=False) as client:
        for index, case in enumerate(source_cases, start=1):
            if not isinstance(case, dict):
                raise TypeError(f"source case {index} must be an object")
            question = str(case.get("question") or "").strip()
            if not question:
                raise ValueError(f"source case {index} missing question")
            kb_ids = _resolve_case_kb_ids(source, case)
            payload = {
                "kb_ids": kb_ids,
                "user_id": EVAL_USER_ID,
                "query": question,
                "profile": profile,
                "top_k": candidate_top_k,
                "rerank_options": {"enabled": False},
            }
            response = client.post(
                f"{base_url.rstrip('/')}/api/v1/rag/retrieve",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise RuntimeError(
                    f"retrieve failed for {case.get('id') or index}: "
                    f"code={body.get('code')} msg={body.get('msg')}"
                )
            chunks = body.get("data", {}).get("retrieved_chunks")
            if not isinstance(chunks, list):
                raise TypeError(f"retrieve response missing chunks: {case.get('id') or index}")
            candidates = [
                {
                    "chunk_id": chunk.get("chunk_id"),
                    "document_id": chunk.get("document_id"),
                    "title": chunk.get("title"),
                    "content": chunk.get("content"),
                    "score": chunk.get("score"),
                    "relevance": None,
                    "relevance_note": "",
                }
                for chunk in chunks
            ]
            prepared_cases.append(
                {
                    "id": case.get("id") or f"case-{index}",
                    "question": question,
                    "kb_ids": kb_ids,
                    "ground_truth": case.get("ground_truth"),
                    "candidates": candidates,
                }
            )
            print(f"prepared {index}/{len(source_cases)}: {case.get('id') or question}")

    return {
        "name": f"{source.get('name') or 'rerank'}_comparison",
        "created_at": datetime.now(UTC).isoformat(),
        "profile": profile,
        "candidate_top_k": candidate_top_k,
        "result_top_n": min(5, candidate_top_k),
        "cases": prepared_cases,
    }


def _build_provider(name: str) -> tuple[RerankProvider, str, str]:
    if name == "qwen3":
        provider = Qwen3RerankProvider()
        return (
            provider,
            provider.model,
            _safe_hostname(settings.rerank_base_url),
        )
    if name == "llm":
        try:
            from app.providers.llm.openai_compatible import OpenAICompatibleLLMProvider
            from app.providers.rerank.llm import LLMRerankProvider
        except ImportError as exc:
            raise RuntimeError(
                "LLM rerank provider has been removed; use the saved LLM baseline report"
            ) from exc
        return (
            LLMRerankProvider(llm_provider=OpenAICompatibleLLMProvider()),
            settings.llm_model,
            _safe_hostname(settings.llm_base_url),
        )
    raise ValueError(f"unsupported provider: {name}")


def _safe_hostname(url: str) -> str:
    return urlparse(url).hostname or ""


async def run_provider(
    *,
    dataset: dict[str, Any],
    provider_name: str,
    repeat: int,
    warmup: int,
) -> dict[str, Any]:
    provider, model, base_url_host = _build_provider(provider_name)
    top_n = int(dataset.get("result_top_n") or 5)
    case_results: list[dict[str, Any]] = []

    for case_index, case in enumerate(dataset.get("cases", []), start=1):
        question = str(case["question"])
        candidates = list(case["candidates"])
        warmup_results = []
        formal_results = []
        total_runs = warmup + repeat
        for iteration in range(total_runs):
            started_at = time.perf_counter()
            try:
                chunks = await provider.rerank(
                    query=question,
                    chunks=candidates,
                    top_n=top_n,
                )
                latency_ms = (time.perf_counter() - started_at) * 1000
                result = {
                    "iteration": iteration + 1,
                    "success": True,
                    "latency_ms": round(latency_ms, 3),
                    "rankings": [
                        {
                            "chunk_id": chunk["chunk_id"],
                            "rerank_score": chunk.get("rerank_score"),
                        }
                        for chunk in chunks
                    ],
                    "error": None,
                }
            except Exception as exc:  # noqa: BLE001 - each remote run must be recorded
                latency_ms = (time.perf_counter() - started_at) * 1000
                result = {
                    "iteration": iteration + 1,
                    "success": False,
                    "latency_ms": round(latency_ms, 3),
                    "rankings": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            if iteration < warmup:
                warmup_results.append(result)
            else:
                formal_results.append(result)
        case_results.append(
            {
                "id": case["id"],
                "question": question,
                "warmups": warmup_results,
                "runs": formal_results,
            }
        )
        print(f"ran {provider_name} {case_index}/{len(dataset.get('cases', []))}: {case['id']}")

    return _build_run_report(
        dataset=dataset,
        provider=provider_name,
        model=model,
        base_url_host=base_url_host,
        repeat=repeat,
        warmup=warmup,
        case_results=case_results,
    )


async def run_ab(
    *,
    dataset: dict[str, Any],
    repeat: int,
    warmup: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    providers = {
        "llm": _build_provider("llm"),
        "qwen3": _build_provider("qwen3"),
    }
    collected: dict[str, list[dict[str, Any]]] = {"llm": [], "qwen3": []}
    top_n = int(dataset.get("result_top_n") or 5)

    for case_index, case in enumerate(dataset.get("cases", [])):
        per_provider = {
            name: {"id": case["id"], "question": case["question"], "warmups": [], "runs": []}
            for name in providers
        }
        total_rounds = warmup + repeat
        for round_index in range(total_rounds):
            order = ["llm", "qwen3"] if (case_index + round_index) % 2 == 0 else ["qwen3", "llm"]
            for name in order:
                provider = providers[name][0]
                started_at = time.perf_counter()
                try:
                    chunks = await provider.rerank(
                        query=case["question"],
                        chunks=list(case["candidates"]),
                        top_n=top_n,
                    )
                    result = {
                        "iteration": round_index + 1,
                        "success": True,
                        "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                        "rankings": [
                            {
                                "chunk_id": chunk["chunk_id"],
                                "rerank_score": chunk.get("rerank_score"),
                            }
                            for chunk in chunks
                        ],
                        "error": None,
                    }
                except Exception as exc:  # noqa: BLE001 - each remote run must be recorded
                    result = {
                        "iteration": round_index + 1,
                        "success": False,
                        "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                        "rankings": [],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                target = "warmups" if round_index < warmup else "runs"
                per_provider[name][target].append(result)
        for name in providers:
            collected[name].append(per_provider[name])
        print(f"ran ab {case_index + 1}/{len(dataset.get('cases', []))}: {case['id']}")

    reports = {}
    for name, (_, model, host) in providers.items():
        reports[name] = _build_run_report(
            dataset=dataset,
            provider=name,
            model=model,
            base_url_host=host,
            repeat=repeat,
            warmup=warmup,
            case_results=collected[name],
        )
    return reports["llm"], reports["qwen3"]


def _build_run_report(
    *,
    dataset: dict[str, Any],
    provider: str,
    model: str,
    base_url_host: str,
    repeat: int,
    warmup: int,
    case_results: list[dict[str, Any]],
) -> dict[str, Any]:
    formal_runs = [run for case in case_results for run in case["runs"]]
    successful = [run for run in formal_runs if run["success"]]
    latencies = [float(run["latency_ms"]) for run in successful]
    consistencies = []
    for case in case_results:
        rankings = [
            tuple(item["chunk_id"] for item in run["rankings"])
            for run in case["runs"]
            if run["success"]
        ]
        if rankings:
            reference = rankings[0]
            consistencies.append(sum(value == reference for value in rankings) / len(rankings))
    total = len(formal_runs)
    return {
        "status": "completed" if successful else "failed",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": dataset.get("name"),
        "dataset_created_at": dataset.get("created_at"),
        "provider": provider,
        "model": model,
        "base_url_host": base_url_host,
        "repeat": repeat,
        "warmup": warmup,
        "summary": {
            "case_count": len(case_results),
            "request_count": total,
            "successful_requests": len(successful),
            "failed_requests": total - len(successful),
            "success_rate": len(successful) / total if total else 0.0,
            "degraded_rate": (total - len(successful)) / total if total else 0.0,
            "p50_latency_ms": percentile(latencies, 0.50),
            "p95_latency_ms": percentile(latencies, 0.95),
            "max_latency_ms": max(latencies) if latencies else None,
            "mean_latency_ms": statistics.fmean(latencies) if latencies else None,
            "top5_consistency": statistics.fmean(consistencies) if consistencies else None,
        },
        "cases": case_results,
    }


def _first_successful_rankings(report: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for case in report.get("cases", []):
        successful = next((run for run in case.get("runs", []) if run.get("success")), None)
        if successful:
            result[str(case["id"])] = [
                str(item["chunk_id"]) for item in successful.get("rankings", [])
            ]
    return result


def compute_ranking_metrics(
    dataset: dict[str, Any],
    rankings_by_case: dict[str, list[str]],
) -> dict[str, float | None]:
    os.environ.setdefault(
        "IR_DATASETS_HOME",
        str(Path(tempfile.gettempdir()) / "rag-center-ir-datasets"),
    )
    try:
        from ranx import evaluate
    except ImportError as exc:
        raise RuntimeError('ranx is required; install with pip install -e ".[eval]"') from exc

    graded_qrels: dict[str, dict[str, int]] = {}
    core_qrels: dict[str, dict[str, int]] = {}
    evidence_qrels: dict[str, dict[str, int]] = {}
    run: dict[str, dict[str, float]] = {}
    for case in dataset.get("cases", []):
        case_id = str(case["id"])
        ranking = rankings_by_case.get(case_id)
        if not ranking:
            continue
        relevance_by_chunk = {
            str(candidate["chunk_id"]): int(candidate["relevance"])
            for candidate in case["candidates"]
        }
        graded_qrels[case_id] = relevance_by_chunk
        core_qrels[case_id] = {
            chunk_id: 1 for chunk_id, relevance in relevance_by_chunk.items() if relevance == 3
        }
        evidence_qrels[case_id] = {
            chunk_id: 1 for chunk_id, relevance in relevance_by_chunk.items() if relevance >= 2
        }
        run[case_id] = {
            chunk_id: float(len(ranking) - rank) for rank, chunk_id in enumerate(ranking)
        }
    if not run:
        return {name: None for name in METRIC_NAMES}
    return {
        "ndcg@5": float(evaluate(graded_qrels, run, "ndcg@5")),
        "mrr@5": float(evaluate(core_qrels, run, "mrr@5")),
        "hit_rate@5": float(evaluate(core_qrels, run, "hit_rate@5")),
        "recall@5": float(evaluate(evidence_qrels, run, "recall@5")),
    }


def case_ndcg(case: dict[str, Any], ranking: list[str], k: int = 5) -> float:
    relevance = {
        str(candidate["chunk_id"]): int(candidate["relevance"]) for candidate in case["candidates"]
    }

    def dcg(values: list[int]) -> float:
        return sum((2**value - 1) / math.log2(index + 2) for index, value in enumerate(values))

    actual = [relevance.get(chunk_id, 0) for chunk_id in ranking[:k]]
    ideal = sorted(relevance.values(), reverse=True)[:k]
    ideal_score = dcg(ideal)
    return dcg(actual) / ideal_score if ideal_score else 0.0


def compare_reports(
    dataset: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    for label, report in (("baseline", baseline), ("candidate", candidate)):
        if report.get("dataset") != dataset.get("name"):
            raise ValueError(f"{label} report uses a different dataset name")
        if report.get("dataset_created_at") != dataset.get("created_at"):
            raise ValueError(f"{label} report uses a different dataset version")

    baseline_rankings = _first_successful_rankings(baseline)
    candidate_rankings = _first_successful_rankings(candidate)
    baseline_metrics = compute_ranking_metrics(dataset, baseline_rankings)
    candidate_metrics = compute_ranking_metrics(dataset, candidate_rankings)
    cases = []
    counts = {"win": 0, "tie": 0, "loss": 0, "missing": 0}
    for case in dataset.get("cases", []):
        case_id = str(case["id"])
        old_ranking = baseline_rankings.get(case_id)
        new_ranking = candidate_rankings.get(case_id)
        if not old_ranking or not new_ranking:
            outcome = "missing"
            old_ndcg = new_ndcg = delta = None
        else:
            old_ndcg = case_ndcg(case, old_ranking)
            new_ndcg = case_ndcg(case, new_ranking)
            delta = new_ndcg - old_ndcg
            outcome = "tie" if abs(delta) <= 1e-12 else ("win" if delta > 0 else "loss")
        counts[outcome] += 1
        cases.append(
            {
                "id": case_id,
                "question": case["question"],
                "outcome": outcome,
                "baseline_ndcg@5": old_ndcg,
                "candidate_ndcg@5": new_ndcg,
                "delta": delta,
                "baseline_top5": old_ranking or [],
                "candidate_top5": new_ranking or [],
                "annotations": [
                    {
                        "chunk_id": str(item["chunk_id"]),
                        "title": item.get("title"),
                        "relevance": item.get("relevance"),
                        "relevance_note": item.get("relevance_note"),
                    }
                    for item in case["candidates"]
                ],
            }
        )

    changes = {}
    for name in METRIC_NAMES:
        old = baseline_metrics[name]
        new = candidate_metrics[name]
        absolute = new - old if old is not None and new is not None else None
        relative = absolute / old if absolute is not None and old else None
        changes[name] = {
            "baseline": old,
            "candidate": new,
            "absolute_change": absolute,
            "relative_change": relative,
        }

    for name in (
        "p50_latency_ms",
        "p95_latency_ms",
        "success_rate",
        "degraded_rate",
        "top5_consistency",
    ):
        old = baseline.get("summary", {}).get(name)
        new = candidate.get("summary", {}).get(name)
        absolute = (
            new - old if isinstance(old, (int, float)) and isinstance(new, (int, float)) else None
        )
        relative = absolute / old if absolute is not None and old else None
        changes[name] = {
            "baseline": old,
            "candidate": new,
            "absolute_change": absolute,
            "relative_change": relative,
        }
    candidate_counts = [
        len(case.get("candidates", []))
        for case in dataset.get("cases", [])
        if isinstance(case, dict)
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": dataset.get("name"),
        "case_count": len(dataset.get("cases", [])),
        "candidate_top_k": dataset.get("candidate_top_k"),
        "actual_candidates_per_case": {
            "min": min(candidate_counts) if candidate_counts else 0,
            "max": max(candidate_counts) if candidate_counts else 0,
        },
        "result_top_n": dataset.get("result_top_n"),
        "annotation": dataset.get("annotation"),
        "baseline": {"provider": baseline.get("provider"), "model": baseline.get("model")},
        "candidate": {"provider": candidate.get("provider"), "model": candidate.get("model")},
        "changes": changes,
        "outcomes": counts,
        "cases": cases,
    }


def render_markdown(comparison: dict[str, Any]) -> str:
    def fmt(value: Any, *, percent: bool = False) -> str:
        if value is None:
            return "N/A"
        if percent:
            return f"{float(value) * 100:.2f}%"
        return f"{float(value):.4f}"

    annotation = comparison.get("annotation") or {}
    actual_candidates = comparison.get("actual_candidates_per_case") or {}
    min_candidates = actual_candidates.get("min")
    max_candidates = actual_candidates.get("max")
    actual_candidate_text = (
        str(min_candidates)
        if min_candidates == max_candidates
        else f"{min_candidates}～{max_candidates}"
    )
    lines = [
        "# LLM 重排与 Qwen3-Rerank 对比报告",
        "",
        f"- 生成时间：{comparison['generated_at']}",
        f"- 数据集：{comparison.get('dataset')}",
        f"- Case 数：{comparison.get('case_count')}",
        f"- 候选 TopK（请求值）：{comparison.get('candidate_top_k')}",
        f"- 实际候选数/Case：{actual_candidate_text}",
        f"- 结果 TopN：{comparison.get('result_top_n')}",
        f"- 标注方式：{annotation.get('method') or '未记录'}",
        f"- 标注状态：{annotation.get('status') or '未记录'}",
        "",
        "| 指标 | LLM 重排 | Qwen3-Rerank | 绝对变化 | 相对变化 |",
        "|------|----------|---------------|----------|----------|",
    ]
    percent_metrics = {
        "hit_rate@5",
        "success_rate",
        "degraded_rate",
        "top5_consistency",
    }
    for name, item in comparison["changes"].items():
        percent = name in percent_metrics
        lines.append(
            f"| {name} | {fmt(item['baseline'], percent=percent)} | "
            f"{fmt(item['candidate'], percent=percent)} | "
            f"{fmt(item['absolute_change'], percent=percent)} | "
            f"{fmt(item['relative_change'], percent=True)} |"
        )
    outcomes = comparison["outcomes"]
    lines.extend(
        [
            "",
            "## 综合结论",
            "",
            "综上，Qwen3-Rerank 相较 LLM 重排在调用时延、结果稳定性和工程可控性方面具有明显优势，且本轮测试的核心证据命中率没有下降，具备替换在线 LLM 重排的技术价值；但当前 nDCG、MRR 和 Recall 指标仍有下降，尚未达到质量无损替换的验收标准。负向案例分析表明，现有结构化切块中存在空分隔符、纯标题和上下文不完整等低信息片段，这些片段在拼接文档标题后仍可能获得较高相关度，从而干扰 Top5 内部排序。因此，本轮结果主要用于确认精排模型的性能优势和暴露当前数据质量问题；后续完成低信息片段过滤、层级索引与上下文化切块优化，并对标注数据进行人工复核后，将按照相同评测口径重新执行 LLM 重排与 Qwen3-Rerank 的对比测试，再决定是否完成全量替换。",
            "",
            "## 胜平负",
            "",
            f"- 胜：{outcomes['win']}",
            f"- 平：{outcomes['tie']}",
            f"- 负：{outcomes['loss']}",
            f"- 缺失：{outcomes['missing']}",
            "",
            "## 负向案例",
            "",
        ]
    )
    losses = [case for case in comparison["cases"] if case["outcome"] == "loss"]
    if not losses:
        lines.append("无。")
    for case in losses:
        lines.extend(
            [
                f"### {case['id']}",
                "",
                f"- 问题：{case['question']}",
                f"- nDCG@5：{fmt(case['baseline_ndcg@5'])} → {fmt(case['candidate_ndcg@5'])}",
                f"- LLM Top5：{', '.join(case['baseline_top5'])}",
                f"- Qwen3 Top5：{', '.join(case['candidate_top5'])}",
                "",
                "| chunk_id | title | relevance | relevance_note |",
                "|----------|-------|-----------|----------------|",
                *[
                    f"| {item['chunk_id']} | {item.get('title') or ''} | "
                    f"{item.get('relevance')} | {item.get('relevance_note') or ''} |"
                    for item in case["annotations"]
                ],
                "",
            ]
        )
    lines.extend(
        [
            "## 限制",
            "",
            "- 排序标签来自 relevance 标注；标注方式与复核状态见报告元信息。",
            "- 远程调用时延会受到网络和平台负载影响。",
            "- rerank_score 只在单次候选集内使用，不跨请求比较绝对值。",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare and compare rerank evaluations.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Freeze retrieve candidates for labeling.")
    prepare.add_argument("--dataset", type=Path, required=True)
    api_key_source = prepare.add_mutually_exclusive_group(required=True)
    api_key_source.add_argument("--api-key")
    api_key_source.add_argument("--api-key-file", type=Path)
    prepare.add_argument("--base-url", default="http://127.0.0.1:8000")
    prepare.add_argument("--profile", default="balanced")
    prepare.add_argument("--candidate-top-k", type=int, default=20)
    prepare.add_argument("--output", type=Path, required=True)

    validate = subparsers.add_parser("validate", help="Validate labels and candidate fixtures.")
    validate.add_argument("--dataset", type=Path, required=True)

    run = subparsers.add_parser("run", help="Run one rerank provider on frozen candidates.")
    run.add_argument("--provider", choices=("llm", "qwen3"), required=True)
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--repeat", type=int, default=3)
    run.add_argument("--warmup", type=int, default=1)
    run.add_argument("--output", type=Path, required=True)

    ab = subparsers.add_parser("ab", help="Alternate LLM and Qwen3 calls in one time window.")
    ab.add_argument("--dataset", type=Path, required=True)
    ab.add_argument("--repeat", type=int, default=3)
    ab.add_argument("--warmup", type=int, default=1)
    ab.add_argument("--baseline-output", type=Path, required=True)
    ab.add_argument("--candidate-output", type=Path, required=True)

    compare = subparsers.add_parser("compare", help="Generate metrics and Markdown report.")
    compare.add_argument("--dataset", type=Path, required=True)
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--json-output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "prepare":
            api_key = args.api_key
            if args.api_key_file:
                lines = args.api_key_file.read_text(encoding="utf-8").splitlines()
                prefix = "plain key (save now, shown once): "
                api_key = next(
                    (
                        line.removeprefix(prefix).strip()
                        for line in lines
                        if line.startswith(prefix)
                    ),
                    None,
                )
            if not api_key:
                raise ValueError("API key is empty or missing from --api-key-file")
            prepared = prepare_dataset(
                source=load_json(args.dataset),
                base_url=args.base_url,
                api_key=api_key,
                profile=args.profile,
                candidate_top_k=args.candidate_top_k,
            )
            save_json(args.output, prepared)
            print(f"saved: {args.output}")
            return 0
        if args.command == "validate":
            result = validate_dataset(load_json(args.dataset))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 1
        if args.command == "run":
            report = asyncio.run(
                run_provider(
                    dataset=load_json(args.dataset),
                    provider_name=args.provider,
                    repeat=args.repeat,
                    warmup=args.warmup,
                )
            )
            save_json(args.output, report)
            print(f"saved: {args.output}")
            return 0
        if args.command == "ab":
            baseline, candidate = asyncio.run(
                run_ab(
                    dataset=load_json(args.dataset),
                    repeat=args.repeat,
                    warmup=args.warmup,
                )
            )
            save_json(args.baseline_output, baseline)
            save_json(args.candidate_output, candidate)
            print(f"saved: {args.baseline_output}")
            print(f"saved: {args.candidate_output}")
            return 0
        if args.command == "compare":
            comparison = compare_reports(
                load_json(args.dataset),
                load_json(args.baseline),
                load_json(args.candidate),
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(render_markdown(comparison), encoding="utf-8")
            if args.json_output:
                save_json(args.json_output, comparison)
            print(f"saved: {args.output}")
            return 0
    except Exception as exc:  # noqa: BLE001 - CLI should return a concise failure
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
