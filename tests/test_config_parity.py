"""The example files are this project's documentation for configuration, so a
field that exists only in Python is a field nobody will find. These tests are the
`checklist` agent's config checks, moved somewhere CI runs them.

Note what is *not* here: the developer's real agent-loop.config.yaml. It is
gitignored and absent on a CI runner, so only the example is checked.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from agent_loop.config import GithubSourceConfig, Settings

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "agent-loop.config.example.yaml"
EXAMPLE_ENV = ROOT / ".env.example"


def _public_names(model: type[BaseModel]) -> set[str]:
    """Every name a user would write in YAML: the alias where one is declared,
    the field name otherwise — recursing into nested models."""
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(field.alias or name)
        for annotation in (field.annotation, *getattr(field.annotation, "__args__", ())):
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                names |= _public_names(annotation)
    return names


def test_example_config_is_valid_yaml():
    doc = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    assert doc["sources"], "the example must declare at least one source"


def test_example_config_validates_against_the_model():
    # The first source is uncommented and complete; the rest are commented-out
    # variants. If this fails, the example would not start the process.
    doc = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    GithubSourceConfig.model_validate(doc["sources"][0])


def test_every_source_field_appears_in_the_example_config():
    text = EXAMPLE_CONFIG.read_text()
    # `type` and `id` are structural, not settings — they are obviously present.
    missing = sorted(n for n in _public_names(GithubSourceConfig) if n not in text)
    assert not missing, (
        f"fields absent from agent-loop.config.example.yaml: {missing}. "
        "Add them (a commented example counts) — the file is the documentation."
    )


def test_every_env_setting_appears_in_the_example_env():
    text = EXAMPLE_ENV.read_text()
    missing = sorted(n for n in _public_names(Settings) if n not in text)
    assert not missing, f"settings absent from .env.example: {missing}"
