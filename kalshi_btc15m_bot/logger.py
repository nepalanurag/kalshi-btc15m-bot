from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


class JsonlLogger:
    def __init__(self, path: str):
        self.path = Path(path)
        # Ensure directory exists
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        
    def append(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
