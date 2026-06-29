"""Seed / fixture source for local debugging.

Returns pre-built news items — no LLM call or IMAP credentials required.
Activate by setting DEBUG_SEED=true in .env (or using DebugConfig).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .base import ExtractedItem, NewsSource, RawDocument

# ---------------------------------------------------------------------------
# Pre-built items.  days_ago is relative to the moment fetch() is called.
# ---------------------------------------------------------------------------
_ITEMS: list[dict] = [
    {
        "days_ago": 5,
        "title": "Anthropic Releases Claude 4 with Extended Thinking Mode",
        "one_liner": "New model exposes step-by-step reasoning and scores top on GPQA and MATH-500 benchmarks.",
        "summary": (
            "Anthropic has shipped Claude 4, its most capable model to date, introducing an "
            "'extended thinking' mode that lets the model spend additional compute exploring "
            "reasoning chains before answering. In internal benchmarks it reaches 92% on GPQA "
            "and 96% on MATH-500, surpassing GPT-4o on both. The model is available immediately "
            "through the API and Claude.ai; context window is 200K tokens."
        ),
        "url": "https://www.anthropic.com/news/claude-4",
        "item_type": "announcement",
    },
    {
        "days_ago": 5,
        "title": "DeepMind's AlphaGeometry 2 Solves 83% of IMO Problems",
        "one_liner": "Successor system combines a neuro-symbolic solver with a language model co-pilot to crack hard olympiad geometry.",
        "summary": (
            "Google DeepMind's AlphaGeometry 2 solved 83% of International Mathematical Olympiad "
            "geometry problems — up from 54% in the first version — by pairing a symbolic deduction "
            "engine with a Gemini-based language model that proposes auxiliary constructions. "
            "The system operates without any human-generated proof hints. Full details appear in "
            "a Nature paper published today."
        ),
        "url": "https://deepmind.google/research/publications/alphageometry2",
        "item_type": "paper",
    },
    {
        "days_ago": 4,
        "title": "Meta Open-Sources Llama 4 Scout and Maverick",
        "one_liner": "Two new open-weight models — a 17B MoE and a 109B MoE — beat Gemma 3 and Mistral Large on most public evals.",
        "summary": (
            "Meta released Llama 4 Scout (17B active parameters, 16 experts) and Llama 4 Maverick "
            "(109B active, 128 experts) under a permissive open-weights license. Both are "
            "natively multimodal, accepting text and images. Maverick outperforms GPT-4o on the "
            "MMLU, HumanEval, and DocVQA benchmarks according to Meta's internal evals. Weights "
            "are available on Hugging Face."
        ),
        "url": "https://ai.meta.com/blog/llama-4-scout-maverick",
        "item_type": "announcement",
    },
    {
        "days_ago": 4,
        "title": "Chain-of-Draft Prompting Reduces Token Use by 80% With Minimal Accuracy Loss",
        "one_liner": "Instead of verbose chain-of-thought, models write terse intermediate notes that retain reasoning quality at a fraction of the cost.",
        "summary": (
            "Researchers from UC San Diego and Stanford propose Chain-of-Draft (CoD), a prompting "
            "strategy where the model produces minimal, keyword-like reasoning steps rather than "
            "full sentences. Across GSM8K, ARC-Challenge, and StrategyQA, CoD cuts output tokens "
            "by 78–83% compared to standard chain-of-thought while losing less than 2 percentage "
            "points of accuracy. The technique works zero-shot and needs no fine-tuning."
        ),
        "url": "https://arxiv.org/abs/2502.18600",
        "item_type": "paper",
    },
    {
        "days_ago": 3,
        "title": "Cursor AI Raises $500M Series C at $9B Valuation",
        "one_liner": "The AI code editor has hit 4 million monthly active developers a year after launch.",
        "summary": (
            "Cursor, the AI-first code editor built on VS Code, has closed a $500M Series C led "
            "by Andreessen Horowitz, valuing the two-year-old company at $9B. The raise comes as "
            "the product reports 4 million monthly active developers, up from 1 million six months "
            "ago. The company plans to use the capital to expand its model portfolio and add "
            "multi-file agentic editing capabilities."
        ),
        "url": "https://techcrunch.com/2025/cursor-series-c",
        "item_type": "news",
    },
    {
        "days_ago": 3,
        "title": "Mistral Releases Codestral 2.0: Beats GPT-4o on HumanEval",
        "one_liner": "The new 32B coding model also introduces a 256K context window suitable for whole-repository tasks.",
        "summary": (
            "Mistral AI has shipped Codestral 2.0, a 32B parameter model fine-tuned specifically "
            "for code generation, completion, and repair. It achieves 92.3% pass@1 on HumanEval — "
            "above GPT-4o's 90.2% — and introduces a 256K token context window that lets it "
            "reason across entire codebases. The model supports 80+ programming languages and "
            "is available via the Mistral API with competitive pricing."
        ),
        "url": "https://mistral.ai/news/codestral-2",
        "item_type": "announcement",
    },
    {
        "days_ago": 2,
        "title": "Hugging Face Releases SmolLM 3: 1.7B Model Matches 7B Competitors",
        "one_liner": "A new training recipe combining synthetic data and RLHF closes the quality gap between small and mid-size open models.",
        "summary": (
            "Hugging Face's SmolLM 3 is a 1.7B parameter language model that scores within 2% of "
            "Llama 3.2 7B on standard academic benchmarks while running on a single consumer GPU. "
            "The model was trained using a mix of FineWeb-Edu, synthetic instruction data, and a "
            "lightweight RLHF stage. Weights, training code, and dataset recipes are all released "
            "under Apache 2.0."
        ),
        "url": "https://huggingface.co/blog/smollm3",
        "item_type": "tool",
    },
    {
        "days_ago": 2,
        "title": "OpenAI Publishes o3 System Card Including Safety Evaluations",
        "one_liner": "The document reveals the model passed most dangerous-capability thresholds but triggered new 'preparedness' mitigations for CBRN tasks.",
        "summary": (
            "OpenAI has released the system card for o3, detailing results across uplift evals "
            "for biological, chemical, radiological, and nuclear (CBRN) risks. The model scored "
            "'medium uplift' on bio and chem threat categories — the first time OpenAI has "
            "disclosed such a rating — triggering additional deployment restrictions. The card "
            "also covers persuasion, autonomous replication, and cyberoffense evaluations."
        ),
        "url": "https://openai.com/index/o3-system-card",
        "item_type": "blog",
    },
    {
        "days_ago": 1,
        "title": "Agentic AI Pipelines Are Quietly Becoming Production Infrastructure",
        "one_liner": "More companies are running LLM agents 24/7 in critical workflows, but best practices for reliability and observability are still nascent.",
        "summary": (
            "An analysis of over 200 production AI deployments found that 38% now include at "
            "least one autonomous agent loop — up from 9% eighteen months ago. The most common "
            "use cases are customer support triage, code review, and data pipeline monitoring. "
            "However, fewer than a fifth of teams have implemented structured logging, cost caps, "
            "or rollback mechanisms for their agent systems, creating operational risk."
        ),
        "url": "https://newsletter.pragmaticengineer.com/p/agentic-pipelines",
        "item_type": "blog",
    },
    {
        "days_ago": 0,
        "title": "Opinion: We Should Stop Calling Everything an 'Agent'",
        "one_liner": "Overloading the term 'agent' to mean everything from a simple tool-call to a fully autonomous system is obscuring real engineering trade-offs.",
        "summary": (
            "The author argues that the AI industry's conflation of rule-based automation, "
            "retrieval-augmented chatbots, and genuinely autonomous goal-pursuing systems under "
            "the single label 'agent' is creating confusion in product decisions and safety "
            "discussions. A proposed taxonomy distinguishes reactors (single-turn), orchestrators "
            "(multi-step, human-in-the-loop), and autonomons (persistent, self-directed), each "
            "with distinct reliability and oversight requirements."
        ),
        "url": "https://www.aisnakeoil.com/p/not-all-agents",
        "item_type": "opinion",
    },
]


class SeedSource(NewsSource):
    """Returns pre-built fixture items; never calls the LLM or any external API."""

    type_key = "seed"
    label = "Debug seed data"
    description = "Pre-built fixture items for local development — no credentials needed."
    config_schema = {}

    def fetch(self, since: datetime | None) -> list[RawDocument]:
        now = datetime.now(tz=timezone.utc)
        docs = []
        for i, item in enumerate(_ITEMS):
            received_at = now - timedelta(days=item["days_ago"])
            # Respect `since` so repeated polls don't duplicate if since is set
            if since is not None:
                since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
                if received_at <= since_aware:
                    continue
            docs.append(
                RawDocument(
                    external_id=f"seed-{i}",
                    text="",
                    received_at=received_at,
                    subject=item["title"],
                )
            )
        return docs

    def extract(self, doc: RawDocument) -> list[ExtractedItem]:
        idx = int(doc.external_id.split("-")[-1])
        data = _ITEMS[idx]
        return [
            ExtractedItem(
                title=data["title"],
                one_liner=data["one_liner"],
                summary=data["summary"],
                url=data["url"],
                item_type=data["item_type"],
                published_at=doc.received_at,
            )
        ]
