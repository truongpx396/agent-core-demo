"""Entry point for `python -m scripts.ai_review`, the command ai-review.yml runs."""
import os
import sys

from scripts.ai_review.review import run

if __name__ == "__main__":
    sys.exit(run(os.environ))
