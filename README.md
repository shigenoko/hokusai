# Hokusai

LangGraph-based AI development workflow automation with Claude Code integration.

[日本語版 README はこちら](./README_JP.md)

## Overview

Hokusai orchestrates a 10-phase development workflow that automates research, planning, implementation, verification, review, and pull-request management. It is built on [LangGraph](https://github.com/langchain-ai/langgraph) and integrates tightly with [Claude Code](https://claude.com/claude-code) and the GitHub CLI (`gh`).

The workflow is designed for **Human-in-the-Loop (HITL)** development: each phase can pause for review, and the unified review loop in Phase 8 handles Copilot and human review comments in any order.

## Features

### Standard

- 10-phase LangGraph workflow (research → design → plan → implement → verify → review → branch hygiene → PR draft → unified review loop → record)
- CLI commands: `start`, `continue`, `status`, `list`, `cleanup`, `pr-status`
- Web dashboard (`scripts/dashboard.py`) for monitoring runs
- SQLite-based persistence and LangGraph checkpointing
- Claude Code integration for autonomous implementation
- GitHub integration via the `gh` CLI
- GitHub Issue task backend
- Phase 7.5 branch hygiene checks (file scope, base-branch sync)
- Customizable prompts in `prompts/`

### Experimental

The following components are present in the codebase but are not enabled by default. Behavior may change without notice.

- **Notion task backend** — set `HOKUSAI_SKIP_NOTION=1` to skip Notion access.
- **Multiple repositories** (mono-repo style) — single-repository setup is the default.
- **Cross-LLM review** — requires multi-LLM setup.
- **Jira / Linear / GitLab / Bitbucket integrations** — interfaces exist but are unfinished.

## Prerequisites

- **Python**: 3.11 or later
- **`gh` CLI**: authenticated with `repo` scope (required for PR management and review-comment handling)
- **Claude Code CLI**: installed and configured (used to drive autonomous implementation)
- **Git**: 2.30 or later

The Phase 8 unified review loop replies to PR review comments via `gh`, so the authenticated user must have write access to the target repository.

## Installation

```bash
# Using uv (recommended)
uv pip install hokusai-flow

# Or using pip
pip install hokusai-flow
```

> Note: the GitHub repository name is `hokusai`, but the PyPI distribution is `hokusai-flow` because `hokusai` on PyPI is held by an unrelated project.

## Quick Start

```bash
# Start a new workflow from a GitHub issue URL
hokusai -c configs/example-github-issue.yaml start https://github.com/your-org/your-repo/issues/1

# List workflows
hokusai list

# Resume a workflow that paused for review
hokusai continue <workflow-id>

# Inspect status
hokusai status <workflow-id>

# Open the dashboard
python scripts/dashboard.py
```

State is stored under `~/.hokusai/` by default (`workflow.db`, `checkpoint.db`, `logs/`). Override with the `data_dir` config option if needed.

## Configuration

See `configs/example-github-issue.yaml` and `configs/example-gitlab.yaml` for sample configurations. A minimal configuration looks like:

```yaml
project_root: ~/repos/my-project
base_branch: main

task_backend:
  type: github_issue

git_hosting:
  type: github
```

## Documentation

- Implementation prompts: `prompts/`
- Phase node sources: `hokusai/nodes/`
- Configuration model: `hokusai/config/models.py`

## Limitations

- The unified review loop in Phase 8 currently assumes GitHub-based pull requests. GitLab/Bitbucket support is experimental.
- The CLI is single-user; concurrent workflows on the same task URL are not supported.
- Prompts in `prompts/` are tuned for Japanese-language tasks; English-language tuning is under way.

## License

Apache License 2.0. See [LICENSE](./LICENSE).

## Contributing

This project is in alpha. Issues and pull requests are welcome — please open an issue first to discuss substantial changes.
