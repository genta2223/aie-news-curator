"""Topic clustering and similarity-based deduplication for RSS articles (O-4)."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from src.collector import ArticleMetadata

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-+_][a-z0-9]+)*")
_CJK_SEQ_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]+")
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "for",
        "to",
        "of",
        "in",
        "on",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "new",
        "ai",
        "llm",
        "how",
        "using",
        "via",
    }
)


class RelatedArticleRef(BaseModel):
    """Reference to a related article merged into a representative topic."""

    source: str
    url: str
    title: str


class ArticleCluster(BaseModel):
    """A cluster of similar articles represented by one primary article."""

    representative: ArticleMetadata
    related_articles: list[RelatedArticleRef] = Field(default_factory=list)


def _extract_tokens(text: str) -> tuple[set[str], set[str]]:
    """Extract normalized ASCII word tokens and combined (ASCII + CJK 2-gram) tokens."""
    lowered = text.lower()
    ascii_tokens = {
        tok for tok in _ASCII_TOKEN_RE.findall(lowered) if len(tok) >= 2 and tok not in _STOPWORDS
    }
    cjk_ngrams: set[str] = set()
    for seq in _CJK_SEQ_RE.findall(lowered):
        if len(seq) == 1:
            cjk_ngrams.add(seq)
        else:
            for idx in range(len(seq) - 1):
                cjk_ngrams.add(seq[idx : idx + 2])
    return ascii_tokens, ascii_tokens | cjk_ngrams


def _jaccard(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity between two token sets."""
    if not set_a or not set_b:
        return 0.0
    union = set_a | set_b
    return len(set_a & set_b) / len(union)


def compute_article_similarity(a: ArticleMetadata, b: ArticleMetadata) -> float:
    """Compute similarity in [0.0, 1.0] using title/summary word & CJK n-gram Jaccard."""
    title_ascii_a, title_all_a = _extract_tokens(a.title)
    title_ascii_b, title_all_b = _extract_tokens(b.title)

    title_jaccard = _jaccard(title_all_a, title_all_b)
    # Handle cross-language (e.g., EN vs JA) articles sharing specific model/project names
    ascii_overlap = len(title_ascii_a & title_ascii_b)
    cross_lang_score = (
        0.85 * _jaccard(title_ascii_a, title_ascii_b) if ascii_overlap >= 2 else 0.0
    )

    _, combined_a = _extract_tokens(f"{a.title} {a.summary}")
    _, combined_b = _extract_tokens(f"{b.title} {b.summary}")
    combined_jaccard = _jaccard(combined_a, combined_b)

    return max(title_jaccard, cross_lang_score, 0.7 * title_jaccard + 0.3 * combined_jaccard)


def cluster_similar_articles(
    articles: list[ArticleMetadata],
    threshold: float = 0.30,
) -> list[ArticleCluster]:
    """Group articles covering the same topic into clusters with one representative."""
    clusters: list[ArticleCluster] = []

    for article in articles:
        placed = False
        for cluster in clusters:
            rep = cluster.representative
            if article.url == rep.url:
                placed = True
                break
            if compute_article_similarity(rep, article) >= threshold:
                existing_urls = {rep.url} | {ref.url for ref in cluster.related_articles}
                if article.url not in existing_urls:
                    cluster.related_articles.append(
                        RelatedArticleRef(
                            source=article.source,
                            url=article.url,
                            title=article.title,
                        )
                    )
                placed = True
                break
        if not placed:
            clusters.append(ArticleCluster(representative=article))

    return clusters
