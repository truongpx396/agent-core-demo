"""Writes each domain's REAL system prompt (app/domains/*/domain.py) into a
promptfoo chat-message prompt file under promptfoo/prompts/ — run before
`promptfoo eval`/`promptfoo redteam run` (see Makefile's `promptfoo`/
`promptfoo-redteam` targets) so the prompts promptfoo tests are always
whatever this app's own domain modules currently define, never a
hand-copied string that can drift out of sync the moment a domain prompt
changes. The generated files are git-ignored build output, the same
relationship this app's `scripts/index_skills.py`-built Qdrant index has to
`skills/`'s own on-disk SKILL.md files: disk (here, the Python source) is
truth, the generated artifact is just a cache of it in the shape the
consuming tool (promptfoo) needs.

Each file is a JSON chat-message array — `[{"role": "system", "content":
"<the real prompt>"}, {"role": "user", "content": "{{user_message}}"}]` —
because promptfoo's `ollama:chat:<model>` provider (see
promptfoo/support.yaml etc.) takes a prompt in exactly this shape;
`json.dumps` handles the real prompt text's own quotes/newlines correctly,
which hand-writing this JSON by hand would not.

Run with: `python -m promptfoo.dump_prompts` (see Makefile).
"""
import json
from pathlib import Path

from app.domains.ops.domain import OPS_SYSTEM_PROMPT
from app.domains.sales.domain import SALES_SYSTEM_PROMPT
from app.domains.support.domain import SUPPORT_SYSTEM_PROMPT

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_DOMAIN_PROMPTS = {
    "support": SUPPORT_SYSTEM_PROMPT,
    "ops": OPS_SYSTEM_PROMPT,
    "sales": SALES_SYSTEM_PROMPT,
}


def main() -> None:
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, system_prompt in _DOMAIN_PROMPTS.items():
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "{{user_message}}"},
        ]
        (PROMPTS_DIR / f"{name}.json").write_text(json.dumps(messages, indent=2))
        print(f"wrote {PROMPTS_DIR / f'{name}.json'}")


if __name__ == "__main__":
    main()
