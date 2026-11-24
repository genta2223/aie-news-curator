"""Unit tests for the AIE News Curator pipeline (no external network/API calls)."""

from __future__ import annotations

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
from src.curator import CurationResult, CuratedArticle, curate_article, parse_converse_response
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


def test_build_slack_blocks_draft_and_publish_and_limit():
    curated_items = []
    for i in range(7):
        art = _make_article(title=f"Article {i}", url=f"https://example.com/{i}")
        cur = CurationResult(
            relevance_score=min(5, 1 + (i % 5)),
            related_lecture="Day5: RAG・ハイブリッド検索・Tree/GraphRAG",
            summary_ja=f"要約 {i}",
            learning_point=f"学び {i}",
            discussion_prompt=f"問いかけ {i}",
        )
        curated_items.append(CuratedArticle(article=art, curation=cur, model_id="mock"))

    draft_blocks = build_slack_blocks(curated_items, publish=False, top_k=5)
    assert "運営確認用下書き" in draft_blocks[0]["text"]["text"]
    section_blocks = [b for b in draft_blocks if b["type"] == "section"]
    assert len(section_blocks) == 5

    pub_blocks = build_slack_blocks(curated_items, publish=True, top_k=5)
    assert "運営確認用下書き" not in pub_blocks[0]["text"]["text"]
