# Project memory

Operational guide for agents: `AGENTS.md`. Architecture of the tool plugins and
shared infrastructure: `docs/DESIGN_tool_plugin_architecture.md`.

## Standing rules

- **Modularity is key.** If something can be shared between tools, it should be.
  Put shared logic on the `ToolPlugin`/`BaseTool` contract or in shared
  infrastructure (`services/subprocess_runner.py`, `services/tools/registry.py`)
  rather than copy-pasting per tool, and prefer registry-driven behavior over
  branching on tool identity or hard-coded extensions.
- **Document it.** When you add or change shared machinery, document it in the
  proper doc under `docs/` — the plugin contract / shared infrastructure in
  `docs/DESIGN_tool_plugin_architecture.md`, and user-facing changes in
  `docs/RELEASE_NOTES.md`.

## Build / test / lint

- Tests: `PYTHONPATH=app python -m pytest -q`
- Lint: `ruff check .` (max line length 100, matching `.pylintrc` / Codacy)
- Version lives in `package.json`; releases also update `docs/RELEASE_NOTES.md`.
