# Evaluation and tests

This file gives the details of the tests and the measurements in the README, sections 5 and 6.

### 5.4 Prompt rules are general

Each prompt rule is a general principle. No rule is a patch for one failure.
Example: an early prompt had three sentences of the type "never refuse for X".
One rule replaced them: write a plan when the question names something to count.
Refuse only when there is no subject, or when trial records do not contain the answer.

### 5.5 Evaluation of the planner

`evals/run_evals.py` gives a score for **planning only**, because the model selection changes
only that part. A question passes when each expected property of the plan is correct, and the
plan has no extra filter.

The set has 29 questions in [`evals/questions.yaml`](../evals/questions.yaml):

- 9 from the assignment appendix.
- 8 that are not in the appendix.
- 3 that the planner must refuse.
- 9 with more difficult words.

| Model | Passed | Median time | Slowest | Retries | Tool calls | Tokens for each question (in / out) | Estimated cost for each question |
|---|---|---|---|---|---|---|---|
| `gpt-5.4-nano` | 29/29 | 2.6 s | 7.2 s | 0 | 8 | 5,330 / 311 | $0.0015 |
| `gpt-5.4-mini`, first run | 29/29 | 2.5 s | 5.2 s | 0 | 3 | 4,840 / 295 | $0.0050 |
| `gpt-5.4-mini`, second run | 28/29 | 2.3 s | 104.5 s | 0 | 3 | 5,264 / 351 | $0.0055 |
| `gpt-5.4` | 29/29 | 3.7 s | 6.8 s | 0 | 3 | 4,840 / 291 | $0.0165 |

How to read these results:

- **The set does not show a difference between the models.** The conclusion is about cost and
  speed: the smallest model is sufficient for planning.
- **A score of 100% does not mean that planning has no errors.** Each model ran one time.
  The same model can answer the same question differently in two runs.
- **The second run of the default model failed one question** (H6: "Map which sponsors run
  sickle cell disease trials in which countries"). The planner gave a two-field count
  (sponsor and country), and the test expects a network. In 7 runs of that question, 5 gave a
  network and 2 gave the two-field count. The two plans are valid and give a chart.
  `evals/results_gpt-5.4-mini.json` holds the second run.
- **The questions were not used to write the prompt.** They use drugs and conditions that are not
  in the prompt examples.
- **The cost is an estimate.** It uses measured tokens and list prices from October 2026.

### 6.2 Tests

The suite has 804 automated tests. They do not use the network. They do not need a key.

| Subject | Examples of what the tests cover |
|---|---|
| Aggregation | Each rule: explode, combine, unknown values, sort, limit, complete grids. |
| Validator | Each rule, for each plan mode. |
| Planner | The limit of 3 tool calls. The retry. Output that is not a plan. |
| Registry client | Pages, tiers, cache, rate limit, each retry wait time, each failure type. |
| Citations | Evidence compared with the raw fixture JSON. Each ranking rule. |
| Failure table | Each row, through the real HTTP endpoint. |
| Suggestions | An offered question that fails is not shown. Zero trials gives wider questions. |

The fixtures are real registry responses without changes.

### 6.3 Behavior on failure

**Diagram 8. From a failure to the next step**

Files: `app/pipeline.py`, `app/static/app.js`

![Diagram 8. From a failure to the next step](diagrams/08_failure_next_step.png)

*How to read it: Read from top to bottom. Each result that is not a chart ends with a next step for the user.*

"Automated" tests use the real HTTP endpoint. Only the registry and the LLM are fakes.
"Live" is the output of the real services, recorded in
[`evals/manual_failure_runs.md`](../evals/manual_failure_runs.md).

| Condition | Expected result | Live result (2026-10-12) | Test |
|---|---|---|---|
| Zero trials | `ok`, no data, a note, and wider questions in `meta.suggestions` | ✅ | Automated and live |
| Misspelled drug or brand name | `ok`, with the corrected name in `meta.search_terms` | ✅ "pembrolizumub" → pembrolizumab. "Keytruda" finds the same 2,974 trials. | Automated and live |
| Vague question | `unsupported`, asks for a subject | ✅ | Automated and live |
| Question that trial records cannot answer | `unsupported`, with the cause | ✅ "Trial registry records do not show which treatment works best…" | Automated and live |
| Plan not valid after the retry | `unsupported`, with the errors in plain words | ✅ in the automated test. A real model refuses such a question itself, so there is no live case. | Automated only |
| Too many trials | `unsupported`, with the count and narrower versions | ✅ "123,915 trials matched…". Offers: recruiting only (18,769), Phase 3 only (11,035). | Automated and live |
| Registry not available | `error`, HTTP 503, after 3 tries | ✅ | Automated and live |
| One series fails | `ok`, with the other series and a note | ✅ | Automated and live |
| LLM not available | `error`, HTTP 503, with the cause | ✅ "The OpenAI API key was rejected." | Automated and live |

One row failed on the first live run. The service refused a nonsense drug name in one run of two.
The cause was the prompt. After the general rule in 5.4, ten runs of ten were `ok`.
This is a rate and not a guarantee.

### 6.4 Other checks with the live registry

- **Citation links:** for 29 bars in five charts, the link count was equal to the bar for all 29.
- **Tier 2 buckets:** the sum of the buckets was equal to the registry total (14,613 of 14,613).
- **Placebo removal:** a check of 114 intervention records from the 50 largest placebo trials
  judged 110 decisions correct (96.5%). The list is in [`evals/placebo_spot_check.md`](../evals/placebo_spot_check.md).
  The AI assistant made this judgment. A person did not examine it.
- **Demo page:** the AI assistant ran more than 50 different questions in a browser. See 11.

---

Back to the [README](../README.md).
