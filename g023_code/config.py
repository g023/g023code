"""
Configuration and environment for g023 Code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def get_home() -> Path:
    """Directory containing the g023-code installation (where K.dat lives)."""
    env = os.environ.get("G023_HOME")
    if env:
        return Path(env).resolve()
    # Fallback: package parent
    return Path(__file__).resolve().parent.parent


def get_project_root() -> Path:
    """Current project working directory (where the user launched from)."""
    env = os.environ.get("G023_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def get_scratch_dir() -> Path:
    """Per-project .g023/ scratch folder."""
    root = get_project_root() / ".g023"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_cache_db() -> Path:
    return get_scratch_dir() / "cache.db"


# ---------------------------------------------------------------------------
# API Key
# ---------------------------------------------------------------------------

def load_api_key() -> str:
    """Load DeepSeek API key from K.dat inside the program folder."""
    key_file = get_home() / "K.dat"
    if not key_file.exists():
        raise FileNotFoundError(
            f"API key file not found: {key_file}\n"
            "Create K.dat in the g023-code folder and put your DeepSeek API key on the first line."
        )
    key = key_file.read_text(encoding="utf-8").strip()
    if not key or key == "YOUR_DEEPSEEK_API_KEY_HERE":
        raise ValueError(
            f"Please put a valid DeepSeek API key into {key_file}\n"
            "Get one at https://platform.deepseek.com/"
        )
    return key


# ---------------------------------------------------------------------------
# Model & Runtime Settings
# ---------------------------------------------------------------------------

ModelId = Literal["deepseek-v4-flash", "deepseek-v4-pro"]
ReasoningEffort = Literal["low", "high", "max"]


@dataclass
class Settings:
    # Models
    orchestrator_model: ModelId = "deepseek-v4-flash"
    subagent_model: ModelId = "deepseek-v4-flash"
    reasoning_effort: ReasoningEffort = "high"
    thinking_enabled: bool = True

    # API
    base_url: str = "https://api.deepseek.com"
    beta_base_url: str = "https://api.deepseek.com/beta"
    max_tokens: int = 8192
    temperature: float = 0.2

    # Context
    max_context_tokens: int = 900_000  # leave headroom under 1M
    compact_threshold: float = 0.85

    # Subagents / Tools
    max_search_matches: int = 12
    file_summary_max_tokens: int = 400
    permission_default: str = "ask"  # allow | ask | block

    # Vision (external)
    vision_backend: str = "none"  # none | glm | openai | local
    vision_api_key: Optional[str] = None

    # Runtime
    stream: bool = True
    verbose: bool = False
    project_root: Path = field(default_factory=get_project_root)
    home: Path = field(default_factory=get_home)

    def __post_init__(self):
        self.project_root = Path(self.project_root).resolve()
        self.home = Path(self.home).resolve()


# Global settings instance (mutable at runtime via slash commands)
settings = Settings()
