"""
Config scopes — machine-level config.json vs the project's .g023/config.json.

The machine file next to K.dat holds the global defaults; a project file, when
present, overrides them for that project only. `/config global` promotes the
current settings to the machine file so every new project inherits them.
"""

from __future__ import annotations

import json

from g023_code import config
from g023_code.config import (
    PERSISTED_KEYS,
    Settings,
    get_config_file,
    get_project_config_file,
    load_config,
    save_config,
    settings,
)


def _write(path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_project_config_overrides_global(monkeypatch):
    global_path = get_config_file()
    project_path = get_project_config_file()
    _write(global_path, {"vision_backend": "deepseek", "verbose": "low"})
    _write(project_path, {"vision_backend": "ollama"})

    fresh = Settings()
    applied = load_config(fresh)
    assert fresh.vision_backend == "ollama"  # project wins
    assert fresh.verbose == "low"  # global fills the rest
    assert applied["vision_backend"] == "ollama"
    assert applied["verbose"] == "low"


def test_save_auto_prefers_project_when_it_exists(monkeypatch):
    project_path = get_project_config_file()
    _write(project_path, {})
    settings.vision_backend = "deepseek"
    path = save_config()  # auto scope
    assert path == project_path
    data = json.loads(project_path.read_text())
    assert data["vision_backend"] == "deepseek"


def test_save_auto_falls_back_to_global_without_project(monkeypatch):
    project_path = get_project_config_file()
    if project_path.exists():
        project_path.unlink()
    global_path = get_config_file()
    _write(global_path, {})
    settings.verbose = "high"
    path = save_config()  # auto scope → global
    assert path == global_path
    data = json.loads(global_path.read_text())
    assert data["verbose"] == "high"


def test_save_global_writes_machine_file_even_with_project(monkeypatch):
    _write(get_project_config_file(), {"vision_backend": "ollama"})
    _write(get_config_file(), {})
    settings.vision_backend = "deepseek"
    path = save_config(scope="global")
    assert path == get_config_file()
    data = json.loads(get_config_file().read_text())
    assert data["vision_backend"] == "deepseek"
    # The project file is untouched.
    proj = json.loads(get_project_config_file().read_text())
    assert proj["vision_backend"] == "ollama"


def test_all_persisted_keys_survive_a_round_trip():
    fresh = Settings()
    for key in PERSISTED_KEYS:
        assert hasattr(fresh, key), f"Settings lacks persisted key {key}"


def test_vision_backend_choices_are_the_new_set():
    # The persisted config must reject backends the CLI cannot describe.
    from g023_code.commands import BACKEND_CHOICES

    assert BACKEND_CHOICES == ("none", "ollama", "deepseek", "openrouter")
    fresh = Settings()
    fresh.vision_backend = "deepseek"
    fresh.vision_model = None
    assert fresh.effective_vision_model == fresh.deepseek_vision_model
    assert fresh.effective_vision_model == "deepseek-v4-flash-vision-exp"


def test_openrouter_backend_effective_model_defaults():
    from g023_code.config import OPENROUTER_DEFAULT_MODEL

    fresh = Settings()
    fresh.vision_backend = "openrouter"
    fresh.vision_model = None
    assert fresh.effective_vision_model == OPENROUTER_DEFAULT_MODEL
    assert fresh.effective_vision_model == "google/gemma-4-26b-a4b-it:free"


def test_vision_enabled_requires_key_file(monkeypatch, tmp_path):
    fresh = Settings()
    fresh.vision_backend = "deepseek"
    # K.dat absent in the isolated tmp home → not enabled.
    assert fresh.vision_enabled is False
    (tmp_path / "K.dat").write_text("sk-test\n", encoding="utf-8")
    assert fresh.vision_enabled is True
