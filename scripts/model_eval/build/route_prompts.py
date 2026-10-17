"""Building the routing prompts: `evals/datasets/route-domains/<version>/` (specs/014 FR-030, FR-039).

Only PROMPTS are committed. Whether the local model or a stronger one answers each well is measured per operator, with their own
models and judge (`router-label`), and stored in an ignored `outcomes/` directory: a label is a fact about two specific models, not about
the prompt. Prompts are written by the local model across the four domains and three difficulties, then de-duplicated; they are
`reviewed: false` until a person reads them. No real traffic is used (FR-039).
"""
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from scripts.model_eval import datasets as ds
from scripts.model_eval.build.retrieval import (
    parse_questions,  # the same tolerant JSON-list parser, key "questions" or "prompts"
)

OUT = ds.DATASETS_ROOT / "route-domains"
DEV_SHARE_PERCENT = 30
DOMAINS = {
    "ecorp": "an internal company assistant: HR and policy questions, looking up employees, simple calculations",
    "support": "a customer-support copilot: tickets, the knowledge base, refunds, escalations",
    "ops": "an operations assistant: incidents, metrics, runbooks, deploys, on-call",
    "sales": "a sales assistant: leads, quotes, discounts, deal economics, follow-ups",
}
DIFFICULTY = {
    "easy": "a single factual lookup or a short direct question",
    "medium": "needs one or two tool calls, or combining two facts, or a short draft",
    "hard": "multi-step reasoning, a calculation with several inputs, an ambiguous request, or a task with a constraint that is easy to miss",
}
PROMPT = """You are creating test requests for an AI assistant. The assistant is {domain_text}.
Write {n} different, realistic messages a user might send. Difficulty: {difficulty_text}.
Vary the topic and wording. Do not number them. Do not use real people or companies.
Return only JSON of this exact shape: {{"prompts": ["...", "..."]}}"""


def parse_prompts(raw: str) -> list[str]:
    return parse_questions(raw, "prompts")


def build_items(generate: Callable[[str], str], *, calls_per_cell: int = 2, per_call: int = 13) -> tuple[list[dict], dict]:
    items: list[dict] = []
    seen: list[str] = []
    stats = {"generated": 0, "duplicate": 0, "too_short": 0}
    for domain, domain_text in DOMAINS.items():
        for difficulty, difficulty_text in DIFFICULTY.items():
            for _call in range(calls_per_cell):
                for text in parse_prompts(generate(PROMPT.format(domain_text=domain_text, n=per_call, difficulty_text=difficulty_text))):
                    stats["generated"] += 1
                    if len(text) < 15:
                        stats["too_short"] += 1
                    elif ds.near_duplicates([text], seen):
                        stats["duplicate"] += 1
                    else:
                        seen.append(text)
                        items.append({"id": f"route-{len(items) + 1:03d}", "domain": domain, "difficulty": difficulty, "text": text, "reviewed": False})
    return items, stats


def split(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"dev": [], "test": []}
    for it in items:
        out["dev" if int(hashlib.sha256(it["id"].encode()).hexdigest()[:8], 16) % 100 < DEV_SHARE_PERCENT else "test"].append(it)
    return out


def write_dataset(splits: dict[str, list[dict]], *, generator: str, prompt: str, generated_at: str, version: str = "v1", root: Path = OUT) -> Path:
    target = root / version
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1, "name": "route-domains", "version": version, "kind": "route", "licence": ds.REPO_INTERNAL, "domains": sorted(DOMAINS),
        "provenance": {"generator": generator, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "generated_at": generated_at,
                       "note": "prompts only; per-model outcomes are produced per operator into the ignored outcomes/ directory"},
        "splits": {name: {"file": f"{name}.jsonl", "sha256": ds.write_jsonl(target / f"{name}.jsonl", rows), "items": len(rows)} for name, rows in splits.items()},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return target


def main() -> None:
    import sys
    from datetime import UTC, datetime

    from scripts.model_eval.build.retrieval import ollama_generator

    model = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5:7b"
    gen = ollama_generator(model)

    def generate(prompt: str) -> str:  # prompts, unlike short questions, need more output room
        return gen(prompt)

    items, stats = build_items(generate)
    splits = split(items)
    target = write_dataset(splits, generator=f"ollama/{model}", prompt=PROMPT, generated_at=datetime.now(UTC).date().isoformat())
    sys.stdout.write(f"{len(items)} prompts ({stats}); dev {len(splits['dev'])}, test {len(splits['test'])} -> {target}\n")


if __name__ == "__main__":
    main()
