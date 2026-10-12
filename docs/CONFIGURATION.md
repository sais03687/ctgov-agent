# Configuration, tests, and the run log

### Configuration

Put these variables in the `.env` file. Only the key is mandatory.

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | none | The planner needs this key. Without it, `/query` gives an `error` response with the cause. |
| `PLANNER_MODEL` | `gpt-5.4-mini` | The model that writes the query plan. |
| `LABEL_MODEL` | `gpt-5.4-nano` | The model that labels names that are not clear in a network. |
| `MAX_TRIALS` | `20000` | The largest set of trials that the service reads fully. |
| `CACHE_TTL_SECONDS` | `3600` | The life of a registry response in memory when the service cannot read the registry data timestamp. `0` stops the cache. |
| `RUN_LOG_PATH` | `logs/last_run.txt` | The text log of the last question. |

### Tests and tools

```bash
uv run pytest -q                                        # 804 tests. No network and no key are necessary.
uv run python evals/run_evals.py --model gpt-5.4-mini   # Planner evaluation. Uses the live LLM.
uv run python evals/manual_failure_runs.py              # Failure table with the live services.
uv run python scripts/run_examples.py                   # Makes the files in examples/ again.
```

### Run log

The file `logs/last_run.txt` shows all the work for the last question in plain text.
The service erases the file when a new question arrives. The file contains:

- The request.
- Each pipeline step and its time.
- All text sent to each LLM, and the exact output of each LLM. JSON has new indentation only.
- How the system used the LLM output.
- The plan that the service ran.
- The searches sent to the registry.
- The result.

To see the file change, use `Get-Content logs\last_run.txt -Wait` (PowerShell) or `tail -F logs/last_run.txt`.

---

Back to the [README](../README.md).
