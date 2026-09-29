# Contributing

Use Python 3.11 or later and `uv sync --locked` to install development dependencies.

Before submitting a change, run:

```sh
uv run ruff format --check .
uv run ruff check .
uv run pytest -q
uv build
```

Keep tests isolated: use temporary databases, synthetic fixtures, and fake GitHub, PostHog, and Herdr responses. Automated tests must not open live agent sessions or access real support accounts.

Source refresh must remain read-only. Preserve local decisions, unsaved drafts, revision conflict checks, and audit history. Add regression tests for behavior changes and document changes to CLI options or configuration.

Do not include credentials, local configuration, customer data, database files, reports, or agent transcripts in a pull request. Use fictional project IDs and support views in examples and fixtures.

By contributing, you agree that your contributions are licensed under the project's MIT license.
