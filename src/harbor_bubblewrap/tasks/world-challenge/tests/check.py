"""Exit 0 only if the evidence file records a correct answer."""

import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text().splitlines() if path.is_file() else []
answers = [json.loads(line) for line in lines if line.strip()]
sys.exit(0 if any(answer.get("correct") is True for answer in answers) else 1)
