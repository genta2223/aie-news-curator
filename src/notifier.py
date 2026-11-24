"""Slack notifier module using Slack Block Kit and Incoming Webhooks."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from slack_sdk.webhook import WebhookClient

from src.curator import CuratedArticle

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_DIGEST_PATH = Path(__file__).resolve().parent.parent / "data" / "last_digest.json"


def select_top_articles(articles: list[CuratedArticle], top_k: int = 5) -> list[CuratedArticle]:
    """Return top-k articles sorted by relevance_score descending."""
    sorted_items = sorted(articles, key=lambda item: item.curation.relevance_score, reverse=True)
    return sorted_items[:top_k]


def build_slack_blocks(
    articles: list[CuratedArticle],
    publish: bool = False,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Build Slack Block Kit blocks for review draft or student channel publish."""
    selected = select_top_articles(articles, top_k=top_k)
    header_text = (
        "📰 AIE 本日のAIニュースダイジェスト"
        if publish
        else "📝 【運営確認用下書き】AIE 本日のAIニュースダイジェスト"
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"関連度上位 *{len(selected)}件* を配信します。"
                        if publish
                        else f"関連度上位 *{len(selected)}件* の下書きです。問題なければ `python main.py publish` で受講生チャンネルへ配信してください。"
                    ),
                }
            ],
        },
        {"type": "divider"},
    ]

    for idx, item in enumerate(selected, start=1):
        art = item.article
        cur = item.curation
        stars = "★" * cur.relevance_score + "☆" * (5 - cur.relevance_score)
        body_text = (
            f"*{idx}. <{art.url}|{art.title}>*\n"
            f"🏷️ `{cur.related_lecture}` | 関連度: {stars} ({cur.relevance_score}/5) | 出典: {art.source}\n\n"
            f"*要約:*\n{cur.summary_ja}\n\n"
            f"*💡 学びのポイント:*\n{cur.learning_point}\n\n"
            f"*💬 議論の問いかけ:*\n{cur.discussion_prompt}"
        )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": body_text},
            }
        )
        blocks.append({"type": "divider"})

    return blocks


def save_digest(articles: list[CuratedArticle], path: Path | str = DEFAULT_DIGEST_PATH) -> Path:
    """Persist curated articles to local JSON for review-then-publish workflow."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [item.model_dump(mode="json") for item in articles]
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_path


def load_digest(path: Path | str = DEFAULT_DIGEST_PATH) -> list[CuratedArticle]:
    """Load previously saved curated articles from local JSON."""
    in_path = Path(path)
    if not in_path.exists():
        raise FileNotFoundError(f"Digest file not found: {in_path}")
    with in_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return [CuratedArticle.model_validate(item) for item in raw]


def send_slack_digest(
    articles: list[CuratedArticle],
    publish: bool = False,
    top_k: int = 5,
    webhook_url: str | None = None,
    digest_path: Path | str = DEFAULT_DIGEST_PATH,
) -> list[dict[str, Any]]:
    """Send curated digest to Slack review channel (default) or publish channel."""
    selected = select_top_articles(articles, top_k=top_k)
    if not selected:
        logger.warning("No curated articles to post to Slack.")
        return []

    if not publish:
        save_digest(selected, path=digest_path)

    env_key = "SLACK_WEBHOOK_URL" if publish else "SLACK_REVIEW_WEBHOOK_URL"
    resolved_url = (webhook_url or os.getenv(env_key, "")).strip()
    if not resolved_url:
        raise ValueError(f"Slack webhook URL is not configured (env: {env_key}).")

    blocks = build_slack_blocks(selected, publish=publish, top_k=top_k)
    fallback_text = (
        f"AIE 本日のAIニュースダイジェスト ({len(selected)}件)"
        if publish
        else f"【運営確認用下書き】AIE 本日のAIニュースダイジェスト ({len(selected)}件)"
    )

    client = WebhookClient(resolved_url)
    response = client.send(text=fallback_text, blocks=blocks)
    if response.status_code != 200:
        raise RuntimeError(f"Slack webhook failed ({response.status_code}): {response.body}")

    logger.info("Successfully posted %d articles to Slack (%s).", len(selected), env_key)
    return blocks
