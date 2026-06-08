# Story Gold Set

Checked-in JSON fixtures for deterministic story quality gates.

Each file contains:

- `members`: source documents with `news_id` and `body`
- `payload`: the synthesized story JSON payload
- `expect_verifier_ok`: expected result from `verify_story_payload`

Run:

```bash
uv run python scripts/eval_story_gold.py
```

This directory is the regression harness for the verifier. Add fixtures by
hand when you want a specific synth shape locked in.
