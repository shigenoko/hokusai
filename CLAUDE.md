# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

HOKUSAI (`hokusai-flow` on PyPI, `hokusai` on GitHub) is a LangGraph-based, human-in-the-loop AI development workflow automation tool. It orchestrates a fixed **10-phase workflow** — research → design → plan → implement → verify → review → branch hygiene → PR draft → unified review loop → record — driving an external LLM coding agent (Claude Code by default) plus the `gh` CLI to take a task (GitHub issue / Notion page) all the way to a reviewed pull request. Every phase can pause for human approval and resume via `hokusai continue`.

State lives in SQLite under `~/.hokusai/` (`workflow.db` for app state, `checkpoint.db` for LangGraph checkpoints) and can be isolated per project/account via **profiles**.

The codebase is bilingual: **comments, docstrings, prompts, and the CHANGELOG are predominantly Japanese**; identifiers and CLI strings are English. Match the surrounding language when editing — Japanese inline comments are the norm, not an exception.

## Common commands

This project uses **uv** (not bare pip) for dependency management.

```bash
# Install dependencies (including dev tools)
uv sync --extra dev

# Run the full test suite (matches CI)
uv run pytest tests/ -q

# Run a single test file / test / by keyword
uv run pytest tests/test_phase5_implement.py -q
uv run pytest tests/test_operations.py::test_execute_operation -q
uv run pytest -k "llm_gateway and config" -q

# Lint (matches CI; ruff with E/F/I/W, line-length 100, E501 ignored)
uv run ruff check hokusai/ scripts/

# Auto-fix lint + format (also runs via pre-commit on commit)
uv run ruff check --fix hokusai/ scripts/
uv run ruff format hokusai/ scripts/

# Run the CLI from source
uv run hokusai --help
```

CI (`.github/workflows/ci.yml`) runs ruff on `hokusai/` + `scripts/` and pytest on Python **3.11 and 3.12**. `pytest` is configured with `asyncio_mode = auto`, so async tests need no explicit marker. `pre-commit` runs `ruff-check --fix` and `ruff-format`.

## Architecture

### The workflow graph (the heart of the system)

- **`hokusai/graph.py`** — builds the LangGraph `StateGraph`: registers one node per phase, wires sequential edges, and adds conditional edges via router functions. `create_compiled_workflow()` attaches a `SqliteSaver` checkpointer (WAL mode). Read this first to see the control flow.
- **`hokusai/state.py`** — `WorkflowState` (a `TypedDict`) is the single state object threaded through every node, plus all the typed sub-structures (`PhaseStatus`, `ReviewComment`, `RepositoryState`, `VerificationErrorEntry`, etc.) and pure helpers (`create_initial_state`, `update_phase_status`, `add_audit_log`, `should_skip_phase`). Multi-repo runs track per-repository progress in `RepositoryState`.
- **`hokusai/nodes/`** — one module per phase (`phase1_prepare.py` … `phase10_record.py`). Each exposes a `phaseN_*_node(state) -> state` function. `nodes/router.py` holds the conditional-edge functions (`should_retry_implementation`, `should_retry_review`, `should_fix_any_review_issues`, `should_continue_review_loop`). Verify/review failures route **back to phase 5 implement** up to a retry cap, then fail-close to `END`.
- **`hokusai/nodes/phase8/`** — Phase 8 is decomposed into sub-modules (`pr_creation`, `review_wait`, `review_check`, `review_fix`, `ready_for_review`, `comment_handler`, `complete`). The **unified review loop** (`phase8b/c/d_unified_*`) handles Copilot, human, and Devin.ai review comments in any order. The older `phase8*_copilot_*` / `phase8*_human_*` nodes are kept **only for backward-compatible migration** of in-flight workflows — they are registered but not wired into the active graph. Don't extend them; build on the `_unified_` path.

### Orchestration layer

- **`hokusai/workflow.py`** — `WorkflowRunner` is the runtime driver around the compiled graph: it implements `start` / `continue_workflow` / `status` / `sync_pr_status`, streams graph events, detects loops/human-pauses, resolves the resume node from persisted state, and drains pending Notion sync work (review issues, work items, workflow gates, project memory) into the dashboard outbox. This is the bridge between the CLI and the graph.
- **`hokusai/cli_main.py`** — the single (large) argparse entrypoint (`hokusai = hokusai.cli_main:main`). `_build_parser()` defines every subcommand; each has a `_handle_*` function. Subcommands include `start`, `continue`, `status`, `list`, `cleanup`, `pr-status`, `connect`, `notion-setup`, `notion-migrate-schema`, `profile`, `prime`, `dashboard`, `audit`, `operations`, `graph`, `eval`, `backfill`, `backup`, `restore`.

### Configuration & profiles

- **`hokusai/config/`** — `models.py` (dataclasses: `WorkflowConfig`, `TaskBackendConfig`, `GitHostingConfig`, `CrossReviewConfig`, `RepositoryConfig`), `loaders.py` (YAML parsing), `manager.py` (`get_config` / `set_config` / `reset_config` — a process-global singleton), and `profiles.py` (the profile registry that isolates `data_dir`, DBs, worktrees, dashboard ports, and env-var names per project). When `--profile`/`-c` is omitted, the registry's `default_profile` is auto-resolved, falling back to a legacy `claude-workflow.yaml` search.
- Sample configs live in `configs/` (`example-*.yaml` = illustrative; `*-template.yaml` = copy-and-fill with `<TODO:...>` markers).

### Integrations (pluggable, factory-driven)

- **`hokusai/integrations/factory.py`** — `get_task_client()` / `get_git_hosting_client()` instantiate the right backend from config and cache it as a singleton. Backends are swappable by config `type`.
  - `task_backend/` — `github_issue.py`, `notion.py` implemented; `jira.py` / `linear.py` are stubs that `raise NotImplementedError`.
  - `git_hosting/` — `github.py` is primary; `gitlab.py` / `bitbucket.py` exist but Phase 8 is GitHub-first.
  - LLM coding agents: `claude_code.py` (default), `codex.py`, `gemini.py` — used for autonomous implementation and optional cross-LLM review.
  - `design/` — Figma/Miro read context + opt-in `writeback/` (with an SQLite outbox + idempotency).
  - `notion_dashboard/` — operations console sync; `notifications/slack.py` — webhook notifications (failures never abort the workflow).

### LLM Gateway

- **`hokusai/llm_gateway/`** — every LLM call routes through `dispatch.py` (`dispatch_via_gateway`), which records `provider`/`model`/`purpose`/`policy_hits` to the `audit_logs` SQLite table. **Prompt bodies are never stored — only a hash + length.** Phase 1 is audit-log-only by default; Phase 2 enforcement (`llm_gateway.log_only=false`) raises `LLMGatewayBlockedError` on policy violation. Gateway-internal failures **fail open** (a gateway bug must never block the workflow) — only explicit policy decisions block. Toggle quickly with `HOKUSAI_LLM_GATEWAY_ENABLED=1`.

### Prompts

- Prompts are **externalized**, not hardcoded. `prompts/registry.yaml` maps prompt IDs (e.g. `phase2.task_research`) to template files under `prompts/<phase>/` with declared `variables`. Load via `hokusai/prompts/loader.py` (`get_prompt(id)`). Cross-review prompts use a backward-compat proxy in `constants.py`. Edit the template files, not string literals in nodes. Prompts are currently tuned for Japanese-language tasks.

### Persistence & operations

- **`hokusai/persistence/`** — `sqlite_store.py` (`SQLiteStore` for app state, plus a read-only variant) and `backup.py` (`hokusai backup`/`restore` via the SQLite online backup API with verify-then-swap restore).
- **`hokusai/operations.py` + `operations_http.py`** — an Operation Registry: read-only operations registered once and invoked through a single sink by the CLI (`hokusai operations`), the dashboard, and a dependency-free stdlib HTTP admin server. Scope guard rejects mutating ops.
- **`hokusai/eval_capture.py`** — decisions/LLM-call capture for `hokusai eval` (export → capture → gate → replay) drift detection.

### Cross-cutting conventions

- **`@phase_node(phase=N, action="...")`** (`hokusai/utils/phase_decorator.py`) wraps phase nodes to auto-handle skip checks, status updates, and audit logging — write only the real work inside the decorated function.
- **`hokusai/constants.py`** centralizes phase names (JP), status icons, and magic-number thresholds (`BRANCH_NAME_LIMIT`, `COMMIT_THRESHOLD`, `MAX_WORKFLOW_EVENTS`, …). Reuse these rather than redefining literals.
- `scripts/dashboard.py` is intentionally included in the wheel (`packages = ["hokusai", "scripts"]`) because `hokusai.dashboard` imports it.

## Testing conventions

- Tests live in `tests/` (flat, named `test_<area>.py`), with `tests/integration/` and `tests/integrations/` for cross-component cases. Tests mirror phases (`test_phase1_prepare.py` … `test_phase8_pr.py`) and subsystems (gateway, notion, design, profiles, backup, …).
- `tests/conftest.py` provides shared fixtures (e.g. `minimal_state`) and an autouse fixture that clears `HOKUSAI_LLM_GATEWAY_ENABLED` so env state doesn't leak between tests. Prefer the existing fixtures and the pure helpers in `state.py` when constructing test state.

## Git workflow

- Active development branch for this work: `claude/claude-md-docs-i9pa2m`. Create it locally if missing; commit with clear messages; push with `git push -u origin <branch>`. Do **not** open a PR unless explicitly asked.
- Keep the `CHANGELOG.md` `## [Unreleased]` section up to date for user-facing changes — entries are detailed and reference the relevant `docs/` design note and PR. Version is set in `pyproject.toml` and `hokusai/__init__.py` (`__version__`).
- The project is alpha (`Development Status :: 3 - Alpha`); minor version bumps may include breaking changes.

## Where to look

- Design notes & implementation plans: `docs/` (per-issue plans, operation guides, the GBrain roadmap, dogfooding findings).
- Config model reference: `hokusai/config/models.py`.
- README: `README.md` (English) / `README_JP.md` (Japanese) — feature list and operational details.
