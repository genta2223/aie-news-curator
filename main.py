"""CLI entry point for AIE News Curator (PJT03)."""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from src.collector import collect_articles
from src.curator import curate_articles
from src.evaluator import (
    DEFAULT_EVAL_PATH,
    format_evaluation_report,
    run_evaluation,
    save_eval_dataset,
)
from src.notifier import DEFAULT_DIGEST_PATH, load_digest, send_slack_digest

load_dotenv()


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="aie-news-curator",
        description="AIE受講生向けニュース収集・要約・Slack配信CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="RSS収集 -> LLMキュレーション -> Slack確認用チャンネルへ下書き投稿",
    )
    run_parser.add_argument("--hours", type=int, default=48, help="収集対象の過去時間 (デフォルト: 48)")
    run_parser.add_argument("--top-k", type=int, default=5, help="配信する上位記事数 (デフォルト: 5)")
    run_parser.add_argument(
        "--provider",
        choices=["gemini", "bedrock", "mock"],
        default=None,
        help="LLMプロバイダ (未指定時は環境変数から自動判定)",
    )
    run_parser.add_argument("--model", type=str, default=None, help="モデルIDを上書き")
    run_parser.add_argument("--mock", action="store_true", help="LLMをモックモードで実行")
    run_parser.add_argument(
        "--publish",
        action="store_true",
        help="確認用チャンネルではなく受講生配信用チャンネルへ直接投稿する",
    )

    pub_parser = subparsers.add_parser(
        "publish",
        help="直前の確認済み下書き (data/last_digest.json) を受講生向けチャンネルへ本配信",
    )
    pub_parser.add_argument("--top-k", type=int, default=5, help="配信する上位記事数 (デフォルト: 5)")
    pub_parser.add_argument(
        "--digest-path",
        type=str,
        default=str(DEFAULT_DIGEST_PATH),
        help="下書きJSONファイルのパス",
    )

    eval_parser = subparsers.add_parser(
        "evaluate",
        help="評価用記事セットの保存およびモデル・プロンプト比較評価を実行",
    )
    eval_parser.add_argument("--save", action="store_true", help="RSS記事を収集して評価用JSONに保存")
    eval_parser.add_argument("--hours", type=int, default=48, help="収集対象の過去時間")
    eval_parser.add_argument("--limit", type=int, default=5, help="比較評価する記事数 (デフォルト: 5)")
    eval_parser.add_argument(
        "--provider",
        choices=["gemini", "bedrock", "mock"],
        default=None,
        help="比較評価に使用するLLMプロバイダ",
    )
    eval_parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="比較対象のモデルID一覧 (未指定時はプロバイダのデフォルト比較セットを使用)",
    )
    eval_parser.add_argument("--mock", action="store_true", help="モックモードで比較評価を実行")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute the selected CLI subcommand."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        articles = collect_articles(hours=args.hours)
        if not articles:
            print("No articles collected within the specified time window.")
            return 0
        # Curate up to max(top_k * 2, 10) recent candidates to keep latency/cost predictable
        candidates = articles[: max(args.top_k * 2, 10)]
        curated = curate_articles(
            candidates,
            model_id=args.model,
            mock=args.mock,
            provider=args.provider,
        )
        send_slack_digest(curated, publish=args.publish, top_k=args.top_k)
        target = "受講生配信用チャンネル" if args.publish else "運営確認用チャンネル (#news-review)"
        print(f"Posted top {min(len(curated), args.top_k)} articles to {target}.")
        return 0

    if args.command == "publish":
        curated = load_digest(args.digest_path)
        send_slack_digest(curated, publish=True, top_k=args.top_k)
        print(f"Published {min(len(curated), args.top_k)} approved articles to SLACK_WEBHOOK_URL.")
        return 0

    if args.command == "evaluate":
        if args.save:
            dataset = save_eval_dataset(output_path=DEFAULT_EVAL_PATH, hours=args.hours)
            print(
                f"Saved {dataset['filtered_count']} articles to {DEFAULT_EVAL_PATH} "
                f"(raw={dataset['raw_count']}, reduction={dataset['reduction_rate_pct']}%)."
            )
            if not args.mock and not DEFAULT_EVAL_PATH.exists():
                return 0
        report = run_evaluation(
            dataset_path=DEFAULT_EVAL_PATH,
            models=args.models,
            limit=args.limit,
            mock=args.mock,
            provider=args.provider,
        )
        print(format_evaluation_report(report))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
