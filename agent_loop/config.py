"""Configuration: agent/backend + storage from the environment, source
definitions from a YAML file.

Global env (agent backend, DB paths, the DeepSeek secret) is read with
pydantic-settings. The list of sources lives in a YAML file; secrets there are
referenced as ${VAR} and resolved from the same environment, so nothing secret
is committed to the YAML. Telegram has been removed — the only source type is
`github`.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


# ── Global env ────────────────────────────────────────────────────────────
class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # DeepSeek cloud API (OpenAI-compatible). Key from platform.deepseek.com.
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="deepseek-v4-flash", alias="DEEPSEEK_MODEL")

    # Max agent steps per react-loop before it is force-stopped (cost guard).
    agent_max_turns: int = Field(default=100, alias="AGENT_MAX_TURNS")

    db_path: str = Field(default="./data/agent-loop.db", alias="DB_PATH")
    agent_loop_config: str = Field(default="./agent-loop.config.yaml", alias="AGENT_LOOP_CONFIG")


settings = Settings()

DB_PATH = Path(settings.db_path).resolve()
CONFIG_PATH = Path(settings.agent_loop_config).resolve()

if not settings.deepseek_api_key:
    print("\n✖ DEEPSEEK_API_KEY is empty. Set it in .env.\n", file=sys.stderr)
    sys.exit(1)


# ── Per-source config (validated after YAML parse + ${VAR} expansion) ──────
class MergePolicy(BaseModel):
    allowed_globs: list[str] = Field(default=["src/**", "public/**", "docs/**"], alias="allowedGlobs")
    max_changed_lines: int = Field(default=300, alias="maxChangedLines")

    model_config = SettingsConfigDict(populate_by_name=True)


class Labels(BaseModel):
    bug: str = "bug"
    feature: str = "enhancement"
    question: str = "question"


class ContainerConfig(BaseModel):
    """Opt-in Docker execution. When present, the agent's Bash/build/test
    commands run via `docker exec` in an ephemeral per-task container; when
    absent, they run on the host (path-gate is the only boundary)."""

    image: str
    # Extra `docker run` args (e.g. ["--network", "host"]).
    run_args: list[str] = Field(default_factory=list, alias="runArgs")

    model_config = SettingsConfigDict(populate_by_name=True)


class GithubSourceConfig(BaseModel):
    type: Literal["github"]
    id: str
    repo: str  # owner/name
    labels: Labels = Field(default_factory=Labels)
    poll_interval_sec: int = Field(default=120, alias="pollIntervalSec")
    auto_merge: bool = Field(default=False, alias="autoMerge")
    # When auto-merge is on, also require human approval before merging — not
    # just green CI. Approval is a PR label (the owner can add it to their own
    # PR; GitHub blocks self-approving reviews) or an approving review from a
    # different reviewer.
    require_approval: bool = Field(default=True, alias="requireApproval")
    approve_label: str = Field(default="auto-merge", alias="approveLabel")
    clone_dir: str = Field(default="./data/repos", alias="cloneDir")
    policy: MergePolicy = Field(default_factory=MergePolicy)
    container: ContainerConfig | None = None
    # Commands the verify node runs inside the checkout (host or container).
    verify_commands: list[str] = Field(
        default=["npm run lint", "npm run build", "npx playwright test"],
        alias="verifyCommands",
    )
    # Add an "agent-loop" trailer to commit messages and a byline to PR bodies.
    # Some repos forbid AI attribution in git artifacts (goals-app's AGENTS.md
    # does) — turn this off there. Issue/PR *comments* keep their 🤖 prefix
    # regardless: that is how the bot recognises its own comments later.
    git_attribution: bool = Field(default=True, alias="gitAttribution")
    # Optional security scanners run by the diff-security node (after critic).
    # Empty by default — the node still does an LLM review of the diff. Any
    # command exiting non-zero is treated as a security finding.
    security_commands: list[str] = Field(default_factory=list, alias="securityCommands")

    model_config = SettingsConfigDict(populate_by_name=True)

    @field_validator("id", "repo")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must not be empty")
        return v


_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}", re.IGNORECASE)


def _expand_env_refs(value: Any) -> Any:
    """Replace every ${VAR} in string values with os.environ[VAR]; raise if a
    referenced variable is unset, so misconfigured secrets fail loudly."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            v = os.environ.get(name)
            if not v:
                raise ValueError(f"config references ${{{name}}} but that env var is unset")
            return v

        return _VAR_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env_refs(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env_refs(v) for k, v in value.items()}
    return value


def load_sources() -> list[GithubSourceConfig]:
    """Load, expand, and validate the YAML source definitions. Exits on error."""
    if not CONFIG_PATH.exists():
        print(
            f"\n✖ Config file not found: {CONFIG_PATH}\n"
            "  Create it (see agent-loop.config.example.yaml) or set AGENT_LOOP_CONFIG.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        doc = _expand_env_refs(yaml.safe_load(CONFIG_PATH.read_text()))
    except Exception as err:
        print(f"\n✖ Failed to load {CONFIG_PATH}: {err}\n", file=sys.stderr)
        sys.exit(1)

    raw_sources = (doc or {}).get("sources")
    if not raw_sources:
        print(f"\n✖ {CONFIG_PATH}: config must define at least one source under `sources`.\n", file=sys.stderr)
        sys.exit(1)

    sources: list[GithubSourceConfig] = []
    for i, entry in enumerate(raw_sources):
        if entry.get("type") != "github":
            print(
                f"\n✖ {CONFIG_PATH}: sources[{i}].type={entry.get('type')!r} is not supported "
                "(only `github`; telegram was removed).\n",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            sources.append(GithubSourceConfig.model_validate(entry))
        except ValidationError as err:
            print(f"\n✖ Invalid {CONFIG_PATH} sources[{i}]:\n{err}\n", file=sys.stderr)
            sys.exit(1)

    ids = [s.id for s in sources]
    dup = next((x for x in ids if ids.count(x) > 1), None)
    if dup:
        print(f"\n✖ Duplicate source id {dup!r} in {CONFIG_PATH} — ids must be unique.\n", file=sys.stderr)
        sys.exit(1)

    return sources
