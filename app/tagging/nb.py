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


def score_item(item_text: str, tag_docs: list[TagDoc]) -> dict[int, float]:
    """Return {tag_id: similarity} for the item against each tag doc."""
    if not tag_docs or not item_text.strip():
        return {}

    corpus = [td.as_text() for td in tag_docs] + [item_text]
    vectorizer = TfidfVectorizer(stop_words="english", min_df=1)
    try:
        matrix = vectorizer.fit_transform(corpus)
    except ValueError:
        # Empty vocabulary (e.g. all stop words).
        return {}

    item_vec = matrix[-1]
    tag_matrix = matrix[:-1]
    sims = cosine_similarity(item_vec, tag_matrix)[0]
    return {td.tag_id: float(sim) for td, sim in zip(tag_docs, sims)}
