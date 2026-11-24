"""Evaluation module for comparing filter reduction and LLM model performance."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.collector import (
    DEFAULT_SOURCES_PATH,
    ArticleMetadata,
    fetch_feed_articles,
    filter_and_deduplicate,
    load_sources_config,
)
from src.curator import CuratedArticle, curate_articles, resolve_provider

logger = logging.getLogger(__name__)

DEFAULT_EVAL_PATH = Path(__file__).resolve().parent.parent / "data" / "eval_articles.json"
DEFAULT_BEDROCK_MODELS = ["amazon.nova-micro-v1:0", "amazon.nova-lite-v1:0"]
DEFAULT_GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]


def get_default_compare_models(provider: str | None = None) -> list[str]:
    """Return default model comparison pair for the resolved provider."""
    resolved = resolve_provider(provider=provider)
    if resolved == "gemini":
        return list(DEFAULT_GEMINI_MODELS)
    return list(DEFAULT_BEDROCK_MODELS)


def save_eval_dataset(
    output_path: Path | str = DEFAULT_EVAL_PATH,
    sources_path: Path | str = DEFAULT_SOURCES_PATH,
    hours: int = 48,
) -> dict[str, Any]:
    """Collect RSS articles, compute filter reduction stats, and save to local JSON."""
    config = load_sources_config(sources_path)
    raw_articles: list[ArticleMetadata] = []
    for source in config["sources"]:
        try:
            raw_articles.extend(fetch_feed_articles(source))
        except Exception as exc:
            logger.warning("Error fetching %s: %s", source.get("name"), exc)

    filtered = filter_and_deduplicate(
        raw_articles,
        exclude_keywords=config["exclude_keywords"],
        hours=hours,
    )

    raw_count = len(raw_articles)
    filtered_count = len(filtered)
    reduction_rate = ((raw_count - filtered_count) / raw_count * 100.0) if raw_count > 0 else 0.0

    dataset = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "hours": hours,
        "raw_count": raw_count,
        "filtered_count": filtered_count,
        "reduction_rate_pct": round(reduction_rate, 2),
        "articles": [item.model_dump(mode="json") for item in filtered],
    }

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)
    return dataset


def load_eval_dataset(dataset_path: Path | str = DEFAULT_EVAL_PATH) -> dict[str, Any]:
    """Load the saved evaluation dataset from JSON."""
    path = Path(dataset_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Evaluation dataset not found: {path}. Run `python main.py evaluate --save` first."
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def summarize_model_run(results: list[CuratedArticle]) -> dict[str, Any]:
    """Compute aggregate metrics for a single model's curation results."""
    count = len(results)
    if count == 0:
        return {
            "count": 0,
            "avg_score": 0.0,
            "score_dist": {},
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "avg_latency_ms": 0.0,
        }

    scores = [item.curation.relevance_score for item in results]
    dist = dict(sorted(Counter(scores).items()))
    total_in = sum(item.input_tokens for item in results)
    total_out = sum(item.output_tokens for item in results)
    avg_latency = sum(item.latency_ms for item in results) / count

    return {
        "count": count,
        "avg_score": round(sum(scores) / count, 2),
        "score_dist": dist,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "avg_latency_ms": round(avg_latency, 2),
    }


def compute_lecture_agreement(
    results_a: list[CuratedArticle],
    results_b: list[CuratedArticle],
) -> float:
    """Calculate agreement rate (%) on related_lecture between two model runs by URL."""
    map_a = {item.article.url: item.curation.related_lecture for item in results_a}
    map_b = {item.article.url: item.curation.related_lecture for item in results_b}
    common_urls = [url for url in map_a if url in map_b]
    if not common_urls:
        return 0.0
    matches = sum(1 for url in common_urls if map_a[url] == map_b[url])
    return round(matches / len(common_urls) * 100.0, 2)


def run_evaluation(
    dataset_path: Path | str = DEFAULT_EVAL_PATH,
    models: list[str] | None = None,
    limit: int = 5,
    mock: bool | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Run comparative evaluation across models on the fixed article dataset."""
    dataset = load_eval_dataset(dataset_path)
    articles = [ArticleMetadata.model_validate(item) for item in dataset.get("articles", [])][:limit]
    target_models = models or get_default_compare_models(provider=provider)

    runs: dict[str, list[CuratedArticle]] = {}
    summaries: dict[str, dict[str, Any]] = {}

    for model_id in target_models:
        curated = curate_articles(articles, model_id=model_id, mock=mock, provider=provider)
        runs[model_id] = curated
        summaries[model_id] = summarize_model_run(curated)

    agreement_pct: float | None = None
    if len(target_models) >= 2:
        agreement_pct = compute_lecture_agreement(runs[target_models[0]], runs[target_models[1]])

    return {
        "raw_count": dataset.get("raw_count", 0),
        "filtered_count": dataset.get("filtered_count", 0),
        "reduction_rate_pct": dataset.get("reduction_rate_pct", 0.0),
        "evaluated_articles": len(articles),
        "summaries": summaries,
        "lecture_agreement_pct": agreement_pct,
    }


def format_evaluation_report(report: dict[str, Any]) -> str:
    """Format evaluation metrics into a human-readable comparison table."""
    lines = [
        "=== AIE News Curator Evaluation Report ===",
        f"収集記事数 (フィルタ前): {report['raw_count']} 件",
        f"有効記事数 (フィルタ後): {report['filtered_count']} 件 (削減率: {report['reduction_rate_pct']}%)",
        f"今回評価対象件数: {report['evaluated_articles']} 件",
        "",
        "| モデルID | 平均関連度 | スコア分布 | 入力トークン | 出力トークン | 平均処理時間(ms) |",
        "| --- | ---: | --- | ---: | ---: | ---: |",
    ]
    for model_id, stats in report["summaries"].items():
        lines.append(
            f"| {model_id} | {stats['avg_score']} | {stats['score_dist']} | "
            f"{stats['total_input_tokens']} | {stats['total_output_tokens']} | {stats['avg_latency_ms']} |"
        )
    if report.get("lecture_agreement_pct") is not None:
        lines.append("")
        lines.append(f"関連講義判定の一致率: {report['lecture_agreement_pct']}%")
    return "\n".join(lines)
