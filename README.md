# HOKUSAI

**HOKUSAI** = **H**uman-**O**rchestrated **K**nowledge & **U**nified **S**ystem for **A**I **I**ntegration

LangGraph-based AI development workflow automation with Claude Code integration.

[日本語版 README はこちら](./README_JP.md)

## Overview

HOKUSAI orchestrates a 10-phase development workflow that automates research, planning, implementation, verification, review, and pull-request management. It is built on [LangGraph](https://github.com/langchain-ai/langgraph) and integrates tightly with [Claude Code](https://claude.com/claude-code) and the GitHub CLI (`gh`).

The name reflects the design philosophy: **humans orchestrate** decisions and review, while a **unified system** integrates AI tooling for the heavy lifting. Each phase can pause for human input, and the unified review loop in Phase 8 handles Copilot and human review comments in any order — making the workflow safe and predictable for **Human-in-the-Loop (HITL)** development.

## Why HOKUSAI?

HOKUSAI is a human-centered AI workflow system designed for organizations where **trust, accountability, and control matter**.

In industries like finance, payments, and enterprise systems, AI cannot operate unchecked. Every decision must be **explainable, auditable, and ultimately owned by a human**.

HOKUSAI bridges this gap.

It transforms fragmented AI usage into a structured, repeatable workflow where:

- **AI accelerates execution**
- **Humans retain control and responsibility**
- **Knowledge and processes are standardized**
- **Every step is traceable and auditable**

Rather than replacing humans, HOKUSAI orchestrates AI around them.

It provides a unified framework to integrate AI into real-world operations — safely, transparently, and at scale.

## The Problem

AI adoption in enterprise environments is fragmented and difficult to control.

- AI usage is inconsistent across teams
- Prompts and workflows are not standardized
- Outputs are not always traceable or auditable
- Human responsibility is unclear

In regulated industries such as finance and payments, this makes it difficult to safely scale AI usage.

## The Solution

HOKUSAI provides a structured, human-in-the-loop workflow for AI integration.

It transforms ad-hoc AI usage into a repeatable and controlled process where:

- AI accelerates execution
- Humans retain decision-making authority
- Knowledge and processes are standardized
- Every step is traceable and auditable

## Workflow

HOKUSAI is built around a simple but powerful workflow:

1. **Research** — Investigate the task scope and existing code
2. **Design** — Plan the architecture and approach
3. **Plan** — Build a step-by-step execution checklist
4. **Implement** — Execute changes via Claude Code
5. **Verify** — Run tests and lint to confirm correctness
6. **Review** — Final review against quality checklists
7. **Branch hygiene** — Confirm scope and base-branch consistency
8. **PR draft → Unified review loop** — Create a draft PR and handle Copilot / human review comments in any order
9. **Approval** — Human approves the PR for merge
10. **Record** — Persist outcomes for traceability and audit

Each phase can pause for human input. Humans approve transitions, request revisions, or override at any point — keeping responsibility clearly on the human side while AI handles execution.

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
