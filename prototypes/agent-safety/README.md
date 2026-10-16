# Throwaway prototypes

These files are not part of Quietroom. Nothing in `quietroom/` imports them.
`pyproject.toml` packages only `quietroom`, and pytest collects only `tests/`.

They answer design questions for `AGENTS-PLAN.md`. Delete them when the real
changes land. Do not copy them into the package.

Run from the repo root:

```
uv run python prototypes/agent-safety/run.py
```

The script prints one JSON object. The plan quotes that object. Re-run the
script instead of trusting the plan's numbers.
