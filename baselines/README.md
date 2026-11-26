# Regression baselines

`main.json` is a saved `eval_report.json` of the **self-test** gate (built-in reference agent). CI compares every new
run against it (`--baseline baselines/main.json`) and fails if a quality metric drops by more than the tolerance
(default 0.02) or any case flips from pass to fail.

```bash
# refresh deliberately, in its own PR, when you INTEND behaviour to change
python -m app.cli --no-cache --save-baseline baselines/main.json
```

For your *own* agent keep a separate baseline (e.g. `baselines/staging-agent.json`) created with
`--target-mode live --save-baseline ...` and pass it with `--baseline`. Baselines contain model outputs: make sure they
hold no real personal data before committing.
