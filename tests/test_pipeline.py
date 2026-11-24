"""Unit tests for the AIE News Curator pipeline (no external network/API calls)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.collector import (
    ArticleMetadata,
    clean_summary,
    fetch_feed_articles,
    filter_and_deduplicate,
    is_excluded,
)
from src.curator import (
    CurationResult,
    CuratedArticle,
    build_curation_prompt,
    curate_article,
    curate_articles,
    load_lectures_config,
    parse_converse_response,
    resolve_provider,
)
from src.deduplicator import RelatedArticleRef, cluster_similar_articles
from src.evaluator import get_default_compare_models
from src.notifier import build_slack_blocks


def _make_article(
    title: str = "GraphRAG improves retrieval",
    url: str = "https://example.com/1",
    source: str = "arXiv",
    hours_ago: int = 2,
    summary: str = "A new hybrid RAG method.",
) -> ArticleMetadata:
    return ArticleMetadata(
        title=title,
        url=url,
        source=source,
        published_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        summary=summary,
    )


def test_clean_summary_strips_html():
    raw = "<p>Hello <b>World</b> &amp; AI!</p>"
    assert clean_summary(raw) == "Hello World & AI!"


def test_fetch_feed_articles_parses_mocked_feed():
    fake_feed = SimpleNamespace(
        bozo=False,
        entries=[
            {
                "title": "MCP Server Guide",
                "link": "https://example.com/mcp",
                "summary": "<p>Using Model Context Protocol safely.</p>",
                "published_parsed": datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc).timetuple(),
            }
        ],
    )
    with patch("src.collector.feedparser.parse", return_value=fake_feed):
        items = fetch_feed_articles({"name": "Zenn LLM", "url": "https://example.com/rss", "max_items": 5})
    assert len(items) == 1
    assert items[0].title == "MCP Server Guide"
    assert items[0].url == "https://example.com/mcp"
    assert items[0].summary == "Using Model Context Protocol safely."


def test_is_excluded_keywords_and_word_boundary():
    excludes = ["PR", "広告", "ウェビナー"]
    assert is_excluded("【PR】最新AIツール紹介", "詳細はこちら", excludes) is True
    assert is_excluded("無料ウェビナー開催", "参加登録", excludes) is True
    # "PR" should not match "prompt"
    assert is_excluded("Prompt Engineering Best Practices", "LLM guide", excludes) is False


def test_filter_and_deduplicate_removes_old_excluded_and_duplicates():
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    articles = [
        ArticleMetadata(title="Valid 1", url="https://example.com/1", source="A", published_at=now - timedelta(hours=5)),
        ArticleMetadata(title="Valid 1 dup", url="https://example.com/1", source="A", published_at=now - timedelta(hours=4)),
        ArticleMetadata(title="【広告】Promo", url="https://example.com/2", source="A", published_at=now - timedelta(hours=3)),
        ArticleMetadata(title="Too Old", url="https://example.com/3", source="A", published_at=now - timedelta(hours=72)),
    ]
    filtered = filter_and_deduplicate(articles, exclude_keywords=["広告"], hours=48, now=now)
    assert len(filtered) == 1
    assert filtered[0].url == "https://example.com/1"


def test_curation_result_validation_and_parse_converse():
    with pytest.raises(ValidationError):
        CurationResult(
            relevance_score=6,
            related_lecture="Day1",
            summary_ja="要約",
            learning_point="ポイント",
            discussion_prompt="問いかけ",
        )

    mock_response = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "submit_curation",
                            "input": {
                                "relevance_score": 5,
                                "related_lecture": "Day5: RAG・ハイブリッド検索・Tree/GraphRAG",
                                "summary_ja": "GraphRAGとハイブリッド検索を組み合わせた新しい検索手法の解説記事です。",
                                "learning_point": "ベクトル検索と知識グラフの併用による精度向上を確認できます。",
                                "discussion_prompt": "業務データでGraphRAGを導入する際の課題は何でしょうか？",
                            },
                        }
                    }
                ]
            }
        }
    }
    parsed = parse_converse_response(mock_response)
    assert parsed.relevance_score == 5
    assert "Day5" in parsed.related_lecture


def test_curate_article_mock_mode():
    art = _make_article(title="GraphRAG and Hybrid Search", summary="RAG evaluation")
    result = curate_article(art, mock=True)
    assert result.curation.relevance_score in range(1, 6)
    assert "(mock)" in result.model_id


def test_cluster_similar_articles_merges_same_topic():
    art_en = _make_article(
        title="OpenAI releases GPT-5 reasoning model",
        url="https://openai.com/blog/gpt-5",
        source="OpenAI Blog",
        summary="GPT-5 improves reasoning and tool use.",
    )
    art_ja = _make_article(
        title="OpenAIが新推論モデルGPT-5を発表、ツール利用を強化",
        url="https://itmedia.co.jp/gpt-5",
        source="ITmedia AI+",
        summary="OpenAIはGPT-5を公開した。",
    )
    art_other = _make_article(
        title="QLoRA quantization benchmark for Japanese LLMs",
        url="https://zenn.dev/qlora",
        source="Zenn LLM",
        summary="4bit quantization experiments on local GPUs.",
    )

    clusters = cluster_similar_articles([art_en, art_ja, art_other])
    assert len(clusters) == 2
    assert clusters[0].representative.url == "https://openai.com/blog/gpt-5"
    assert len(clusters[0].related_articles) == 1
    assert clusters[0].related_articles[0].source == "ITmedia AI+"
    assert clusters[0].related_articles[0].url == "https://itmedia.co.jp/gpt-5"

    curated = curate_articles([art_en, art_ja, art_other], mock=True)
    assert len(curated) == 2
    merged_item = next(c for c in curated if c.article.url == "https://openai.com/blog/gpt-5")
    assert len(merged_item.curation.related_articles) == 1


def test_build_slack_blocks_draft_and_publish_and_related_links():
    curated_items = []
    for i in range(7):
        art = _make_article(title=f"Article {i}", url=f"https://example.com/{i}")
        related = (
            [
                RelatedArticleRef(
                    source="ITmedia AI+",
                    url="https://itmedia.example.com/related",
                    title="Related",
                ),
                RelatedArticleRef(
                    source="Zenn LLM",
                    url="https://zenn.example.com/related",
                    title="Related 2",
                ),
            ]
            if i == 6
            else []
        )
        cur = CurationResult(
            relevance_score=min(5, 1 + (i % 5)),
            related_lecture="Day5: RAG・ハイブリッド検索・Tree/GraphRAG",
            summary_ja=f"要約 {i}",
            learning_point=f"学び {i}",
            discussion_prompt=f"問いかけ {i}",
            related_articles=related,
        )
        curated_items.append(CuratedArticle(article=art, curation=cur, model_id="mock"))

    draft_blocks = build_slack_blocks(curated_items, publish=False, top_k=5)
    assert "運営確認用下書き" in draft_blocks[0]["text"]["text"]
    section_blocks = [b for b in draft_blocks if b["type"] == "section"]
    assert len(section_blocks) == 5

    # Item 6 has score 2, so ensure a high-score item with related_articles renders links
    high_score_item = CuratedArticle(
        article=_make_article(title="Top Topic", url="https://example.com/top"),
        curation=CurationResult(
            relevance_score=5,
            related_lecture="Day5: RAG・ハイブリッド検索・Tree/GraphRAG",
            summary_ja="要約",
            learning_point="学び",
            discussion_prompt="問いかけ",
            related_articles=[
                RelatedArticleRef(source="ITmedia AI+", url="https://itmedia.jp/1", title="R1"),
                RelatedArticleRef(source="Zenn LLM", url="https://zenn.dev/1", title="R2"),
            ],
        ),
        model_id="mock",
    )
    blocks_with_links = build_slack_blocks([high_score_item], publish=True, top_k=5)
    assert "運営確認用下書き" not in blocks_with_links[0]["text"]["text"]
    section_text = blocks_with_links[3]["text"]["text"]
    assert "あわせて読みたい関連記事" in section_text
    assert "<https://itmedia.jp/1|ITmedia AI+>" in section_text
    assert "<https://zenn.dev/1|Zenn LLM>" in section_text


def test_load_enriched_lectures_and_external_detail_json(tmp_path):
    # Fallback when external detail JSON does not exist
    lectures = load_lectures_config(detail_path=tmp_path / "nonexistent.json")
    assert len(lectures) == 7
    assert "summary" in lectures[0]
    assert "use_cases" in lectures[0]

    # Merging external lectures_detail.json when present
    detail_file = tmp_path / "lectures_detail.json"
    detail_file.write_text(
        json.dumps(
            {
                "Day5": {
                    "details": "BM25と密ベクトルのRRF統合およびRAPTOR階層要約の実装詳細。",
                    "exercises": ["Hybrid RAG構築演習", "Reranker評価"],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    enriched = load_lectures_config(detail_path=detail_file)
    day5 = next(lec for lec in enriched if lec["id"] == "Day5")
    assert "RRF統合" in day5["details"]
    assert "Hybrid RAG構築演習" in day5["exercises"]

    prompt = build_curation_prompt(_make_article(), enriched)
    assert "RRF統合" in prompt
    assert "Hybrid RAG構築演習" in prompt


def test_resolve_provider_auto_and_explicit(monkeypatch):
    monkeypatch.delenv("USE_MOCK_LLM", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)

    assert resolve_provider() == "bedrock"
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    assert resolve_provider() == "gemini"
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "dummy-aws")
    assert resolve_provider() == "bedrock"
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert resolve_provider() == "gemini"
    assert resolve_provider(mock=True) == "mock"


def test_curate_article_gemini_structured_output_and_retry(monkeypatch):
    monkeypatch.delenv("USE_MOCK_LLM", raising=False)
    expected = CurationResult(
        relevance_score=5,
        related_lecture="Day5: RAG・ハイブリッド検索・Tree/GraphRAG",
        summary_ja="Geminiによるテスト要約です。",
        learning_point="ハイブリッド検索の重要性を学べます。",
        discussion_prompt="実務での適用箇所はどこですか？",
    )
    fake_response = SimpleNamespace(
        parsed=expected,
        text=expected.model_dump_json(),
        usage_metadata=SimpleNamespace(prompt_token_count=120, candidates_token_count=45),
    )

    call_count = 0

    class FakeModels:
        def generate_content(self, model, contents, config):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            assert model == "gemini-3.5-flash-lite"
            assert config.response_schema is CurationResult
            assert getattr(config, "temperature", None) is None
            return fake_response

    fake_client = SimpleNamespace(models=FakeModels())
    with patch("src.curator.time.sleep") as mock_sleep:
        result = curate_article(
            _make_article(),
            provider="gemini",
            gemini_client=fake_client,
        )

    assert call_count == 2
    mock_sleep.assert_called_once()
    assert result.model_id == "gemini-3.5-flash-lite"
    assert result.input_tokens == 120
    assert result.output_tokens == 45
    assert result.curation.relevance_score == 5


def test_evaluator_default_models_for_gemini():
    assert get_default_compare_models(provider="gemini") == [
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
    ]
    assert get_default_compare_models(provider="bedrock") == [
        "amazon.nova-micro-v1:0",
        "amazon.nova-lite-v1:0",
    ]
