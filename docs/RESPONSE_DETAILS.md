# Response details

This file gives the details of the response schema in the README, section 3.

Each `meta.trace` entry is `{step, ms, …details}`:

| `step` | Details |
|---|---|
| `plan` | `model`, `llm_calls`, `tool_calls`, `retries`, `input_tokens`, `output_tokens` |
| `validate` | `errors`: the list sent to the LLM for the retry |
| `interpretation` | Shown only when code replaced the LLM sentence: `replaced`, `original` |
| `fetch` | `api_calls`, `cache_hits`, `tier`, `trials_matched`, result for each series |
| `suggest` | `offered`: the number of suggestions |
| `aggregate` | `data_points`, `trials_used`, `dropped` (cause → count), label details for networks |
| `cite` | `data_points`, `trials_cited`, `from_sample` |
| `build` | `chart`, `chart_overridden` |

### 3.4 `error`

| `status` | `error.code` | Condition |
|---|---|---|
| `unsupported` | `unsupported_question` | The question has no subject, or trial records do not contain the answer. |
| `unsupported` | `plan_invalid` | The plan failed validation two times. `details` lists the problems. |
| `unsupported` | `too_many_trials` | More trials agree than the service can read, and the question needs all trials. |
| `unsupported` | `invalid_request` | The request has a list of drugs and a list of conditions together. |
| `error` | `ctgov_unavailable` | The registry failed after the retries. |
| `error` | `request_rejected` | The registry refused the search (HTTP 4xx). |
| `error` | `llm_unavailable` | The service cannot use the LLM. The message gives the cause. |
| `error` | `internal_error` | An unexpected error. The detail is in the trace. |

### 3.5 Suggestions

A user must be able to get a good answer with one click after a failure.
Thus the service gives **suggestions** in two places:

- `error.suggestions`, for each `unsupported` response.
- `meta.suggestions`, for an `ok` response with zero trials.

Each suggestion is `{label, query, request, trials_matched}`.

| Type | Content | Use |
|---|---|---|
| A different question | `query` contains the full new question. | Send `query` as the new question. Remove the request filters. |
| The same question for fewer trials | `request` contains the fields to add, for example `{"status": ["RECRUITING"]}`. | Send the same question with these fields. |

Rules:

- The service does not apply a suggestion automatically. The user makes the decision.
- The service **examines each suggested question before it offers it**. It plans the question,
  validates the plan, and counts the trials. A question that fails is not offered.
- `trials_matched` is the real count from the registry.
- For zero trials, the service removes one filter at a time and counts the result.
  Example: "Phase 4 nivolumab trials by phase (without the country) — 17 trials".

---

Back to the [README](../README.md).
