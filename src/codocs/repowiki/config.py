"""configuration management for repowiki."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_CONFIG_DIR = Path.home() / ".repowiki"
_CONFIG_FILE = _CONFIG_DIR / "config.json"

# short aliases for common model names; any other name is passed through unchanged
MODEL_ALIASES = {
    "gpt": "gpt-5.4",
    "gpt-mini": "gpt-5.4-mini",
}


def resolve_model(name: str) -> str:
    return MODEL_ALIASES.get(name, name)


@dataclass
class Config:
    model: str = "gpt-5.4"
    api_key: str = ""
    api_base: str = ""
    language: str = "en"
    max_file_size: int = 200 * 1024  # 200 KB
    max_files: int = 1000
    output_dir: str = "./wiki"
    concurrency: int = 5

    @classmethod
    def load(cls) -> Config:
        """Load config from file, then override with env vars."""
        data: dict = {}
        if _CONFIG_FILE.exists():
            try:
                data = json.loads(_CONFIG_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

        # env overrides; key and endpoint use the standard OpenAI variables
        if val := os.getenv("OPENAI_API_KEY"):
            cfg.api_key = val
        if val := os.getenv("OPENAI_BASE_URL"):
            cfg.api_base = val
        if val := os.getenv("REPOWIKI_MODEL"):
            cfg.model = val
        if val := os.getenv("REPOWIKI_LANG"):
            cfg.language = val

        cfg.model = resolve_model(cfg.model)
        return cfg

    def save(self) -> None:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "model": self.model,
            "api_key": self.api_key,
            "api_base": self.api_base,
            "language": self.language,
        }
        # don't persist empty values
        data = {k: v for k, v in data.items() if v}
        _CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}
