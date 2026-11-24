# AIE受講生向けニュース共有ツール（PJT03）

AIエンジニア育成講座（AIE）の受講生向けに、最新のAI技術ニュース・論文をRSSから自動収集し、講義カリキュラム（Day1〜Day7）との関連度をAmazon Bedrock（Nova Lite）で判定・日本語要約したうえでSlackへ定期配信するPythonアプリケーションです。

---

## 最終課題での取り組み

### ① 取り組んだ内容
日々のAI関連ニュースや論文の中から、AIE講義の復習・応用に直結する記事を自動抽出し、受講生が「どの講義回の知識と結びつくか」を直感的に把握できるニュースダイジェスト配信パイプラインを構築しました。
著作権およびセキュリティに配慮し、RSSフィードのメタデータのみを取得・要約し、運営確認用チャンネルへの下書き投稿を経て本配信する安全な運用フローを設計しています。

### ② 実装した機能の説明
- **RSS収集・フィルタリング (`src/collector.py`, `config/sources.yaml`)**: `feedparser` を用いて過去48時間以内の記事メタデータ（タイトル・URL・公開日時・RSS概要）を取得し、広告・PR等の除外キーワードフィルタとURL重複排除を行います。
- **Bedrock構造化キュレーション (`src/curator.py`, `config/lectures.yaml`)**: Amazon Bedrock `converse` API（`toolConfig` による構造化出力、デフォルト `amazon.nova-lite-v1:0`）を使用し、Pydanticモデル `CurationResult`（関連度1〜5、関連講義回、150文字程度の日本語要約、学びのポイント、議論の問いかけ）を生成します。ローカル検証用の `--mock` モードも搭載しています。
- **安全設計のSlack Block Kit配信 (`src/notifier.py`)**: 関連度上位5件をSlack Block Kit形式に整形し、デフォルトでは運営確認用チャンネル（`SLACK_REVIEW_WEBHOOK_URL`）へ下書き投稿すると同時に `data/last_digest.json` へ保存します。`publish` コマンド実行時のみ受講生向けチャンネル（`SLACK_WEBHOOK_URL`）へ本配信します。
- **モデル・プロンプト評価 (`src/evaluator.py`)**: 固定記事セットを `data/eval_articles.json` に保存し、除外フィルタ削減率やモデル間（`amazon.nova-micro-v1:0` vs `amazon.nova-lite-v1:0`）の関連講義一致率・スコア分布・トークン数・処理時間を比較レポート出力します。

### ③ 起動手順と定期実行の設定方法
#### 1. 環境構築
```bash
uv sync
```
`.env` ファイルを作成し、以下の環境変数を設定してください（Git管理対象外）。
```env
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=amazon.nova-lite-v1:0
SLACK_REVIEW_WEBHOOK_URL=https://hooks.slack.com/services/...
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

#### 2. CLI実行コマンド
```bash
# モックモードで収集〜#news-review へ下書き投稿
uv run python main.py run --mock

# 本番Bedrockで収集〜#news-review へ下書き投稿
uv run python main.py run

# 確認済み下書きを受講生チャンネル (#ai-news) へ本配信
uv run python main.py publish

# 評価用記事セットの保存と比較評価
uv run python main.py evaluate --save --mock

# ユニットテストの実行
uv run python -m pytest
```

#### 3. 定期実行（GitHub Actions / cron）
- **GitHub Actions**: `.github/workflows/daily_digest.yml` により毎日 08:00 JST（UTC `23:00`）に自動実行されます（`workflow_dispatch` による手動実行も可能）。リポジトリの Secrets に `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `SLACK_REVIEW_WEBHOOK_URL`, `SLACK_WEBHOOK_URL` を登録してください。
- **ローカル cron**: 例として毎日朝8時に実行する場合:
  ```cron
  0 8 * * * cd /path/to/aie-news-curator && uv run python main.py run >> logs/cron.log 2>&1
  ```

### ④ 利用した情報源とその利用条件
本ツールは以下の公開RSS/Atomフィードのみを利用し、**記事ページのWebスクレイピングや記事本文の保存・転載は一切行いません**。Slackには「タイトル（元記事リンク）・関連講義タグ・AI生成の日本語要約・学びのポイント・問いかけ」のみを配信します。

| 情報源 | RSS URL | 利用条件・配慮事項 |
| --- | --- | --- |
| arXiv (`cs.AI+cs.CL+cs.LG`) | `https://rss.arxiv.org/rss/cs.AI+cs.CL+cs.LG` | arXiv API/RSS利用規約に基づきメタデータのみ取得し、原論文リンクを明記 |
| Hugging Face Blog | `https://huggingface.co/blog/feed.xml` | 公式RSSフィードからタイトル・概要のみ参照し、元記事URLへ誘導 |
| Zenn LLM | `https://zenn.dev/topics/llm/feed` | Zenn公開トピックRSSからメタデータのみ取得し、元記事URLへ誘導 |
| ITmedia AI+ | `https://rss.itmedia.co.jp/rss/2.0/aiplus.xml` | ITmedia RSS利用条件に配慮し、本文転載を行わず要約と元記事リンクのみ掲載 |

### ⑤ 特に評価してほしいポイント
1. **誤配信を防ぐ2段階承認フロー**: `run` で必ず `#news-review` へ下書き投稿し、`data/last_digest.json` を経由して `publish` で受講生チャンネルへ配信する安全な運用設計。
2. **定量的なモデル評価基盤**: `src/evaluator.py` により、除外フィルタによる削減率や `Nova Micro` vs `Nova Lite` の関連講義一致率・トークンコスト・レイテンシを同一データセットで客観比較できる点。
3. **著作権・セキュリティの徹底**: スクレイピング禁止、`.env` やローカルキャッシュ（`data/`）の `.gitignore` 除外、Apache License 2.0 の同梱、モックモードを含むユニットテスト完備。
