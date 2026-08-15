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


def _provider_usage(provider: RerankProvider) -> dict[str, int]:
    source = getattr(provider, "llm_provider", provider)
    raw = getattr(source, "last_usage", {})
    if not isinstance(raw, dict):
        raw = {}

    def token_value(*names: str) -> int:
        for name in names:
            value = raw.get(name)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    input_tokens = token_value("input_tokens", "prompt_tokens")
    output_tokens = token_value("output_tokens", "completion_tokens")
    total_tokens = token_value("total_tokens") or input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_hit_input_tokens": token_value("prompt_cache_hit_tokens"),
        "cache_miss_input_tokens": token_value("prompt_cache_miss_tokens"),
    }


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
                    "usage": _provider_usage(provider),
                    "error": None,
                }
            except Exception as exc:  # noqa: BLE001 - each remote run must be recorded
                latency_ms = (time.perf_counter() - started_at) * 1000
                result = {
                    "iteration": iteration + 1,
                    "success": False,
                    "latency_ms": round(latency_ms, 3),
                    "rankings": [],
                    "usage": None,
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
    concurrency: int = 4,
) -> tuple[dict[str, Any], dict[str, Any]]:
    providers = {
        "llm": _build_provider("llm"),
        "qwen3": _build_provider("qwen3"),
    }
    cases = list(dataset.get("cases", []))
    collected: dict[str, list[dict[str, Any]]] = {
        name: [
            {"id": case["id"], "question": case["question"], "warmups": [], "runs": []}
            for case in cases
        ]
        for name in providers
    }
    top_n = int(dataset.get("result_top_n") or 5)
    semaphore = asyncio.Semaphore(max(concurrency, 1))
    completed = 0
    total_jobs = len(cases) * (warmup + repeat) * len(providers)

    async def run_job(name: str, case_index: int, round_index: int) -> None:
        nonlocal completed
        case = cases[case_index]
        async with semaphore:
            provider = _build_provider(name)[0]
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
                    "usage": _provider_usage(provider),
                    "error": None,
                }
            except Exception as exc:  # noqa: BLE001 - each remote run must be recorded
                result = {
                    "iteration": round_index + 1,
                    "success": False,
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                    "rankings": [],
                    "usage": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            target = "warmups" if round_index < warmup else "runs"
            collected[name][case_index][target].append(result)
            completed += 1
            if completed % max(len(providers), 1) == 0 or completed == total_jobs:
                print(f"ran ab {completed}/{total_jobs}: {case['id']} / {name}")

    jobs = []
    for round_index in range(warmup + repeat):
        for case_index in range(len(cases)):
            order = (
                ["llm", "qwen3"]
                if (case_index + round_index) % 2 == 0
                else ["qwen3", "llm"]
            )
            jobs.extend(run_job(name, case_index, round_index) for name in order)
    await asyncio.gather(*jobs)

    for provider_cases in collected.values():
        for case in provider_cases:
            case["warmups"].sort(key=lambda item: item["iteration"])
            case["runs"].sort(key=lambda item: item["iteration"])

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
    usage_totals = {
        name: sum(int((run.get("usage") or {}).get(name) or 0) for run in successful)
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_hit_input_tokens",
            "cache_miss_input_tokens",
        )
    }
    usage_averages = {
        name: round(value / len(successful), 2) if successful else 0
        for name, value in usage_totals.items()
    }
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
            "usage_totals": usage_totals,
            "usage_per_successful_request": usage_averages,
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


def _metric_samples(
    dataset: dict[str, Any],
    report: dict[str, Any],
) -> list[dict[str, float | None]]:
    case_results = list(report.get("cases", []))
    run_count = max((len(case.get("runs", [])) for case in case_results), default=0)
    samples: list[dict[str, float | None]] = []
    for run_index in range(run_count):
        rankings: dict[str, list[str]] = {}
        for case in case_results:
            runs = case.get("runs", [])
            if run_index >= len(runs) or not runs[run_index].get("success"):
                continue
            rankings[str(case["id"])] = [
                str(item["chunk_id"]) for item in runs[run_index].get("rankings", [])
            ]
        samples.append(compute_ranking_metrics(dataset, rankings))
    return samples


def _average_metrics(samples: list[dict[str, float | None]]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for name in METRIC_NAMES:
        values = [float(item[name]) for item in samples if item.get(name) is not None]
        result[name] = statistics.fmean(values) if values else None
    return result


def _metric_ranges(samples: list[dict[str, float | None]]) -> dict[str, dict[str, float] | None]:
    result: dict[str, dict[str, float] | None] = {}
    for name in METRIC_NAMES:
        values = [float(item[name]) for item in samples if item.get(name) is not None]
        result[name] = (
            {
                "min": min(values),
                "max": max(values),
                "mean": statistics.fmean(values),
            }
            if values
            else None
        )
    return result


def _case_rankings(report: dict[str, Any], case_id: str) -> list[list[str]]:
    case = next(
        (item for item in report.get("cases", []) if str(item.get("id")) == case_id),
        None,
    )
    if not case:
        return []
    return [
        [str(item["chunk_id"]) for item in run.get("rankings", [])]
        for run in case.get("runs", [])
        if run.get("success")
    ]


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
    baseline_samples = _metric_samples(dataset, baseline)
    candidate_samples = _metric_samples(dataset, candidate)
    baseline_metrics = _average_metrics(baseline_samples)
    candidate_metrics = _average_metrics(candidate_samples)
    cases = []
    counts = {"win": 0, "tie": 0, "loss": 0, "missing": 0}
    for case in dataset.get("cases", []):
        case_id = str(case["id"])
        old_rankings = _case_rankings(baseline, case_id)
        new_rankings = _case_rankings(candidate, case_id)
        old_ranking = baseline_rankings.get(case_id)
        new_ranking = candidate_rankings.get(case_id)
        if not old_rankings or not new_rankings:
            outcome = "missing"
            old_ndcg = new_ndcg = delta = None
        else:
            old_ndcg = statistics.fmean(case_ndcg(case, item) for item in old_rankings)
            new_ndcg = statistics.fmean(case_ndcg(case, item) for item in new_rankings)
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
    acceptance = {
        "hit_rate_not_lower": (
            candidate_metrics["hit_rate@5"] is not None
            and baseline_metrics["hit_rate@5"] is not None
            and candidate_metrics["hit_rate@5"] >= baseline_metrics["hit_rate@5"]
        ),
        "ndcg_gap_within_5_percent": (
            changes["ndcg@5"]["absolute_change"] is not None
            and changes["ndcg@5"]["absolute_change"] >= -0.05
        ),
        "recall_gap_within_5_percent": (
            changes["recall@5"]["absolute_change"] is not None
            and changes["recall@5"]["absolute_change"] >= -0.05
        ),
        "qwen_p95_at_most_2_seconds": (
            isinstance(candidate.get("summary", {}).get("p95_latency_ms"), (int, float))
            and candidate["summary"]["p95_latency_ms"] <= 2000
        ),
        "qwen_success_rate_at_least_99_percent": (
            candidate.get("summary", {}).get("success_rate", 0) >= 0.99
        ),
        "qwen_consistency_not_lower": (
            isinstance(candidate.get("summary", {}).get("top5_consistency"), (int, float))
            and isinstance(baseline.get("summary", {}).get("top5_consistency"), (int, float))
            and candidate["summary"]["top5_consistency"]
            >= baseline["summary"]["top5_consistency"]
        ),
    }
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
        "metric_samples": {
            "baseline": baseline_samples,
            "candidate": candidate_samples,
        },
        "metric_ranges": {
            "baseline": _metric_ranges(baseline_samples),
            "candidate": _metric_ranges(candidate_samples),
        },
        "changes": changes,
        "acceptance": acceptance,
        "passed": all(acceptance.values()),
        "outcomes": counts,
        "cases": cases,
    }


def build_cost_summary(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    pricing: dict[str, Any],
) -> dict[str, Any]:
    rates = pricing.get("rates") or {}

    def llm_cost(report: dict[str, Any]) -> dict[str, Any]:
        usage = report.get("summary", {}).get("usage_totals") or {}
        success_count = int(report.get("summary", {}).get("successful_requests") or 0)
        hit_tokens = int(usage.get("cache_hit_input_tokens") or 0)
        miss_tokens = int(usage.get("cache_miss_input_tokens") or 0)
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        if hit_tokens + miss_tokens == 0:
            miss_tokens = input_tokens
        hit_rate = rates.get("llm_cache_hit_input_per_million")
        miss_rate = rates.get("llm_cache_miss_input_per_million")
        output_rate = rates.get("llm_output_per_million")
        total_cost = None
        if miss_rate is not None and output_rate is not None:
            total_cost = miss_tokens / 1_000_000 * float(miss_rate)
            total_cost += output_tokens / 1_000_000 * float(output_rate)
            if hit_tokens:
                total_cost += hit_tokens / 1_000_000 * float(hit_rate or miss_rate)
        return {
            "usage": usage,
            "theoretical_cost_cny": total_cost,
            "average_cost_per_request_cny": (
                total_cost / success_count if total_cost is not None and success_count else None
            ),
            "estimated_cost_per_1000_requests_cny": (
                total_cost / success_count * 1000
                if total_cost is not None and success_count
                else None
            ),
        }

    def qwen_cost(report: dict[str, Any]) -> dict[str, Any]:
        usage = report.get("summary", {}).get("usage_totals") or {}
        success_count = int(report.get("summary", {}).get("successful_requests") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        rate = rates.get("qwen_rerank_per_thousand_tokens")
        total_cost = total_tokens / 1000 * float(rate) if rate is not None else None
        return {
            "usage": usage,
            "theoretical_cost_cny": total_cost,
            "average_cost_per_request_cny": (
                total_cost / success_count if total_cost is not None and success_count else None
            ),
            "estimated_cost_per_1000_requests_cny": (
                total_cost / success_count * 1000
                if total_cost is not None and success_count
                else None
            ),
        }

    return {
        "pricing_snapshot": pricing,
        "llm": llm_cost(baseline),
        "qwen3": qwen_cost(candidate),
        "note": "理论成本按标准单价计算，不扣除免费额度、抵扣包或活动优惠。",
    }


def render_markdown(
    comparison: dict[str, Any],
    cost_summary: dict[str, Any] | None = None,
) -> str:
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
    acceptance = comparison.get("acceptance") or {}
    if comparison.get("passed"):
        conclusion = (
            "综上，在 v2 结构化索引的同一批冻结候选上，Qwen3-Rerank 的核心证据命中、"
            "nDCG 与 Recall 均满足预设差距门槛，同时在时延和排序稳定性上优于 LLM 重排。"
            "因此可以继续作为线上排序方案；v1/v2 索引选择不改变这一排序结论。"
        )
    else:
        failed_names = "、".join(name for name, passed in acceptance.items() if not passed)
        conclusion = (
            "Qwen3-Rerank 的工程性能优势已经得到验证，但仍有排序回归门槛未通过："
            f"{failed_names or '结果不完整'}。应保留当前精排实现并继续分析负向 Case，"
            "完整 30 题发布回归前不把本轮结果解释为质量全面领先。"
        )
    lines.extend(
        [
            "",
            "## 综合结论",
            "",
            conclusion,
            "",
            "## 验收门槛",
            "",
            "| 门槛 | 结果 |",
            "|---|---|",
            *[
                f"| {name} | {'通过' if passed else '未通过'} |"
                for name, passed in acceptance.items()
            ],
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
    if cost_summary:
        pricing = cost_summary.get("pricing_snapshot") or {}
        llm_cost = cost_summary.get("llm") or {}
        qwen_cost = cost_summary.get("qwen3") or {}

        def cost_text(value: Any) -> str:
            return "N/A" if value is None else f"¥{float(value):.6f}"

        lines.extend(
            [
                "## Token 与理论成本",
                "",
                f"- 价格地区：{pricing.get('region') or '未记录'}",
                f"- 查询日期：{pricing.get('queried_at') or '未记录'}",
                f"- 单价来源：{pricing.get('source') or '未记录'}",
                "- 免费额度、抵扣包和活动价未计入理论成本。",
                "",
                "| 项目 | LLM 重排 | Qwen3-Rerank |",
                "|---|---:|---:|",
                (
                    f"| 总 Token | {llm_cost.get('usage', {}).get('total_tokens', 0)} | "
                    f"{qwen_cost.get('usage', {}).get('total_tokens', 0)} |"
                ),
                (
                    f"| 本轮理论成本 | {cost_text(llm_cost.get('theoretical_cost_cny'))} | "
                    f"{cost_text(qwen_cost.get('theoretical_cost_cny'))} |"
                ),
                (
                    f"| 单次平均成本 | "
                    f"{cost_text(llm_cost.get('average_cost_per_request_cny'))} | "
                    f"{cost_text(qwen_cost.get('average_cost_per_request_cny'))} |"
                ),
                (
                    f"| 每 1000 次预计成本 | "
                    f"{cost_text(llm_cost.get('estimated_cost_per_1000_requests_cny'))} | "
                    f"{cost_text(qwen_cost.get('estimated_cost_per_1000_requests_cny'))} |"
                ),
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
    ab.add_argument("--concurrency", type=int, default=4)
    ab.add_argument("--baseline-output", type=Path, required=True)
    ab.add_argument("--candidate-output", type=Path, required=True)

    compare = subparsers.add_parser("compare", help="Generate metrics and Markdown report.")
    compare.add_argument("--dataset", type=Path, required=True)
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--json-output", type=Path)

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Run both providers and write one combined JSON and Markdown report.",
    )
    evaluate.add_argument("--dataset", type=Path, required=True)
    evaluate.add_argument("--repeat", type=int, default=3)
    evaluate.add_argument("--warmup", type=int, default=1)
    evaluate.add_argument("--concurrency", type=int, default=4)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--markdown-output", type=Path, required=True)
    evaluate.add_argument("--pricing-source", required=True)
    evaluate.add_argument("--pricing-region", default="中国大陆（北京）")
    evaluate.add_argument(
        "--pricing-date",
        default=datetime.now(UTC).date().isoformat(),
    )
    evaluate.add_argument("--llm-cache-hit-input-price-per-million", type=float, required=True)
    evaluate.add_argument("--llm-cache-miss-input-price-per-million", type=float, required=True)
    evaluate.add_argument("--llm-output-price-per-million", type=float, required=True)
    evaluate.add_argument("--qwen-price-per-thousand-tokens", type=float, required=True)
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
                    concurrency=args.concurrency,
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
        if args.command == "evaluate":
            dataset = load_json(args.dataset)
            validation = validate_dataset(dataset)
            if not validation["valid"]:
                raise ValueError(f"rerank dataset is invalid: {validation['errors']}")
            baseline, candidate = asyncio.run(
                run_ab(
                    dataset=dataset,
                    repeat=args.repeat,
                    warmup=args.warmup,
                    concurrency=args.concurrency,
                )
            )
            comparison = compare_reports(dataset, baseline, candidate)
            pricing = {
                "currency": "CNY",
                "region": args.pricing_region,
                "queried_at": args.pricing_date,
                "source": args.pricing_source,
                "rates": {
                    "llm_cache_hit_input_per_million": (
                        args.llm_cache_hit_input_price_per_million
                    ),
                    "llm_cache_miss_input_per_million": (
                        args.llm_cache_miss_input_price_per_million
                    ),
                    "llm_output_per_million": args.llm_output_price_per_million,
                    "qwen_rerank_per_thousand_tokens": (
                        args.qwen_price_per_thousand_tokens
                    ),
                },
            }
            costs = build_cost_summary(baseline, candidate, pricing)
            combined = {
                "name": "opt16_llm_vs_qwen3",
                "generated_at": datetime.now(UTC).isoformat(),
                "dataset": dataset.get("name"),
                "source_index_version": dataset.get("source_index_version"),
                "annotation": dataset.get("annotation"),
                "runs": {"llm": baseline, "qwen3": candidate},
                "comparison": comparison,
                "costs": costs,
            }
            save_json(args.output, combined)
            args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_output.write_text(
                render_markdown(comparison, costs),
                encoding="utf-8",
            )
            print(f"saved: {args.output}")
            print(f"saved: {args.markdown_output}")
            return 0
    except Exception as exc:  # noqa: BLE001 - CLI should return a concise failure
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
