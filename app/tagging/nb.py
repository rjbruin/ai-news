"""Lightweight keyword/Naive-Bayes-style tag scorer.

Each tag is represented by a pseudo-document built from its name, keywords and
explanation. We TF-IDF-vectorise these alongside the news item and score by
cosine similarity. This needs no labelled training data (it works from the
keywords alone), while still being a proper vector-space classifier that
improves as keywords are refined. When confirmed examples exist they are
folded into the tag's pseudo-document to sharpen it.

We expose ``score_item`` returning {tag_id: confidence in 0..1}.
"""
from __future__ import annotations

from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


@dataclass
class TagDoc:
    tag_id: int
    name: str
    keywords: list[str]
    explanation: str
    examples: list[str]  # confirmed item texts

    def as_text(self) -> str:
        parts = [self.name] * 3  # weight the name
        parts += self.keywords * 3  # weight keywords heavily
        if self.explanation:
            parts.append(self.explanation)
        parts += self.examples
        return " ".join(parts)


class Scorer:
    """Fits TF-IDF once on a background corpus for reuse across many items.

    Passing a background_corpus (all existing news item texts) gives realistic
    IDF weights that scale with the database size, so the same threshold works
    whether the DB has 10 or 10 000 items.
    """

    def __init__(
        self, tag_docs: list[TagDoc], background_corpus: list[str] | None = None
    ):
        self.tag_docs = tag_docs
        tag_texts = [td.as_text() for td in tag_docs]
        self.vectorizer = TfidfVectorizer(stop_words="english", min_df=1)
        # Fit on background items first so IDF reflects actual news vocabulary;
        # always include tag texts so tag-specific terms are in the vocabulary.
        fit_corpus = list(background_corpus or []) + tag_texts
        if not fit_corpus:
            self._ready = False
            return
        try:
            self.vectorizer.fit(fit_corpus)
            self.tag_matrix = self.vectorizer.transform(tag_texts)
            self._ready = True
        except ValueError:
            self._ready = False

    def score(self, item_text: str) -> dict[int, float]:
        if not self._ready or not item_text.strip():
            return {}
        try:
            item_vec = self.vectorizer.transform([item_text])
            sims = cosine_similarity(item_vec, self.tag_matrix)[0]
            return {td.tag_id: float(sim) for td, sim in zip(self.tag_docs, sims)}
        except Exception:
            return {}


def score_item(item_text: str, tag_docs: list[TagDoc]) -> dict[int, float]:
    """Convenience wrapper for scoring a single item with no background corpus."""
    return Scorer(tag_docs).score(item_text)
