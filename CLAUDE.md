# CLAUDE.md — ClinicalTrials.gov Query-to-Visualization Agent

You are building a take-home assignment for an **Agent Engineering Intern** role. This file is the
complete spec. Follow it exactly. When something here is ambiguous, stop and ask; do not guess.

---------------------------------------------------------------------------------------------------
## 0. How to work (read first, applies to every phase)

1. **Build in phases (section 12), one phase per session turn.** At the end of each phase, STOP and
   print the Phase Report (section 13). Do not start the next phase until the user says "next".
2. **Contracts first.** `app/models.py` and `app/registry.py` are written in Phase 1 and are the only
   source of shared types. Never redefine a shared type inside another module.
3. **Every phase ends with green tests.** Run `uv run pytest -q` and paste the summary line in the
   Phase Report. A phase with failing tests is not done.
4. **Keep `TRACEABILITY.md` current.** After each phase, update the status, file, and evidence columns
   for every requirement ID you touched (section 14). Never mark a row Done without naming a test,
   example output, or README section as evidence.
5. **Keep `AI_USAGE.md` current.** One row per file: `generated`, `generated then edited`, or
   `hand-written`, plus one line on what was checked. Default every file you write to `generated`;
   the user will change rows they edit.
6. **No silent changes to the spec.** If real API data contradicts this file, report it in the Phase
   Report under "Spec deviations" and propose the fix before applying it.
7. **Tests never hit the network.** Unit and failure tests use `tests/fixtures/*.json` (real API
   responses already saved by `scripts/fetch_fixtures.py`) or hand-built `Trial` objects. Only the
   eval harness and example-run script call live services.
8. Platform: Windows, Python 3.12, `uv`. Use `pathlib`, never hard-coded `/` paths.

---------------------------------------------------------------------------------------------------
## 1. The assignment (verbatim requirements you must satisfy)

- Build a backend that converts clinical-trial questions into **structured visualization outputs**
  backed by **ClinicalTrials.gov API** data. The system must: (1) interpret the question,
  (2) retrieve relevant data from ClinicalTrials.gov, (3) identify if a visualization is needed and
  which type suits the question, (4) produce a visualization specification that answers it.
- Input: required `query` (string, natural language). Optional structured fields are ours to define
  and **must be documented** (field names, types, optional/required, validation).
- Output: a structured response with (1) a visualization spec: `type`, `title`, `encoding`, `data`,
  and (2) response metadata: fields the frontend needs (units, sorting, time granularity, grouping
  choices) and optional notes about assumptions, filters applied, query interpretation. The response
  schema **must be documented** so a frontend engineer can build a renderer without guessing.
- Support **multiple** visualization types: bar / grouped bar, timeline / time series, scatter,
  histogram, network graph. Design goal: cover as many query types as possible with a **single
  coherent approach**; meaningful network graphs score higher.
- Bonus: **deep citations**. Each visualized datum references the trial records that contributed to
  it; each reference has `nct_id` and an **exact text excerpt or specific field/value** from the API.
- Submission: zip with code, README (how to run: install, configure, start; request/response schema
  docs; key design decisions and tradeoffs; limitations and future improvements), **3–5 example
  queries with the actual JSON outputs**, optional demo.
- Integrity: README must say which AI tools were used, how correctness was validated, and which parts
  were designed/implemented deliberately vs generated and adapted. They reward "evidence of
  thoughtful construction, testing, and iteration."

### Grading rubric (every item maps to requirement IDs in section 14)
| Weight | Criterion | Sub-points |
|---|---|---|
| 35% | System Design | clear, rational design decisions · maintainable structure and extensibility · sensible handling of real-world API data |
| 20% | AI / Agent Design | avoid hallucination-prone steps · include validation or constraints · sensible planning and reasoning steps, with appropriate tools |
| 20% | Code Quality | readability, organization, documentation · correctness and robustness |
| 15% | Query & Viz Coverage | breadth of query types · multiple question classes without one-off hacks · richer visualizations (meaningful network graphs) |
| 10% | I/O Design | well-structured, unambiguous schemas · frontend-friendly visualization spec |
| Bonus | Deep citations | traceability to source records |

### Appendix query classes (all must work; the eval set must include each)
- Time trends: "How has the number of trials for [drug] changed per year since 2015?" ·
  "How many trials started each year for [condition]?"
- Distributions: "How are [condition] trials distributed across phases?" ·
  "What are the most common intervention types for [drug/condition] trials?"
- Comparisons: "Compare phases for trials involving Drug A vs Drug B." ·
  "Compare sponsor categories across two conditions."
- Geographic: "Which countries have the most recruiting trials for [condition]?"
- Networks: "Show a network of sponsors ↔ drugs for [condition] trials." ·
  "Which drugs frequently co-occur in combination studies (drug ↔ drug network)?"
The appendix says these are examples only; the design must generalize past them.

---------------------------------------------------------------------------------------------------
## 2. Design principles (hard rules; cite these in the README)

P1. **LLM plans, code computes.** The LLM only turns language into a validated `QueryPlan`. It never
    fetches data, counts, or writes any number or citation. (Rubric: avoid hallucination-prone steps.)
P2. **Validate every plan before it runs.** Fields, enums, limits come from fixed lists in the
    registry. Invalid plan → one LLM retry with the error list → still invalid → `status:"unsupported"`.
P3. **Structured fields first, simple rules second, LLM last and verified.** Applies to placebo
    detection and name cleanup.
P4. **Never add a filter the user didn't ask for.** Every assumption, drop, trim, override is reported
    in `meta.notes` in plain language (in terms of trials, not internal settings).
P5. **Composable pieces, no one-off handlers.** Generality = filters × series × group_by × mode. No
    `if "network" in query` style branching anywhere.
P6. **Honest failure beats a confident wrong chart.** Partial fetches are never used to compute counts.
P7. **No agent framework.** Use the `openai` SDK directly with structured outputs and a hand-written
    bounded tool loop. Explicit typed `RunState`. Reason: the flow is fixed; a framework adds
    indirection without capability. (Job posting asks to reason about state and failure modes.)

---------------------------------------------------------------------------------------------------
## 3. Verified API facts (checked against the live API on 2026-10-11)

Endpoint: `GET https://clinicaltrials.gov/api/v2/studies`
- Params that work: `query.cond`, `query.intr`, `query.spons`, `query.locn`, `query.term`,
  `filter.overallStatus` (comma list), `filter.advanced`, `fields`, `pageSize`, `pageToken`,
  `countTotal=true`, `sort` (e.g. `LastUpdatePostDate:desc`).
- `filter.advanced` examples that work: `AREA[Phase]PHASE3`,
  `AREA[Phase]PHASE1 AND AREA[Phase]PHASE2`, `AREA[StartDate]RANGE[2015-01-01,MAX]`.
- **`pageSize` silently caps at 1000** (asking 2000 returns 1000, no error). Always page with
  `nextPageToken`; never assume page size.
- `fields` accepts short names: `NCTId,Phase,OverallStatus,StartDate,CompletionDate,
  LastUpdatePostDate,LeadSponsorName,LeadSponsorClass,EnrollmentCount,LocationCountry,Condition,
  InterventionName,InterventionType,InterventionArmGroupLabel,ArmGroupLabel,ArmGroupType,HasResults`
  and returns the normal nested structure.
- **Drug synonyms are built in**: pembrolizumab, Keytruda, MK-3475 each → 2,974 trials. LLM synonym
  expansion is only a fallback.
- **Placebo is typed `DRUG`.** Intervention type cannot remove it. Arm-type rule is required
  (NCT04003636: Placebo only in a `PLACEBO_COMPARATOR` arm; gemcitabine in both arms → keep).
- Zero results: `{"totalCount": 0, "studies": []}`, no `nextPageToken`.
- Dates appear as `YYYY-MM` or `YYYY-MM-DD`; date structs carry `type: ACTUAL | ESTIMATED`.
- Sizes for planning: lung cancer 14,613 (recruiting 2,296); pembrolizumab 2,974; Pfizer 6,091.
- Rate limits are undocumented → retry 429/5xx with exponential backoff (3 attempts) and cap
  concurrency at 4.

---------------------------------------------------------------------------------------------------
## 4. Project layout and module contracts

```
ctgov-agent/
  app/
    models.py          # ALL shared Pydantic types (sections 5–8)
    registry.py        # field registry (section 6)
    config.py          # env settings (pydantic-settings)
    planner/
      prompt.py        # system prompt + few-shot examples
      tools.py         # list_fields, count_trials (read-only)
      planner.py       # bounded tool loop -> QueryPlan
    validator.py       # QueryPlan -> QueryPlan | list[ValidationIssue]
    ctgov/
      client.py        # async httpx client: size check, paging, tiers, cache, retries
      normalize.py     # raw study JSON -> Trial (+ FieldEvidence)
    aggregate/
      aggregate.py     # mode=aggregate (1–2 group_by, series)
      histogram.py     # mode=histogram
      points.py        # mode=points (scatter, gantt)
      network.py       # mode=network (pairs, placebo filter, pruning)
      common.py        # explode/combine helpers, zero-fill, Unknown bucket
    citations.py       # rows + trials -> citation map (ranking rules)
    spec_builder.py    # rows + citations + plan -> VisualizationResponse (chart check, notes)
    pipeline.py        # Request -> Response; owns RunState; wires all modules
    api.py             # FastAPI: POST /query, GET /health, GET /schema
  data/label_cache.json   # LLM name labels, committed pre-filled
  evals/questions.yaml    # ~20 questions + expected plan properties
  evals/run_evals.py
  examples/               # 5 actual request/response JSON pairs
  scripts/fetch_fixtures.py (exists) · scripts/run_examples.py
  tests/ (unit, failure, fixtures/)
  README.md · TRACEABILITY.md · AI_USAGE.md · .env.example · pyproject.toml
```

Module boundaries (each module imports only from `models`, `registry`, `config`, and what is listed):
| Module | Input | Output |
|---|---|---|
| planner | `QueryRequest`, tools | draft `QueryPlan` + tool-call trace |
| validator | `QueryPlan`, registry | valid `QueryPlan` or `list[ValidationIssue]` |
| ctgov.client | plan filters per series, field list | `FetchResult` (trials, totals, tier, api_calls) |
| ctgov.normalize | raw study dict, field list | `Trial` |
| aggregate.* | `list[Trial]` per series, plan | `AggregateResult` (rows + per-row contributing nct_ids + drop counts) |
| citations | rows, contributing ids, trials, plan | `dict[str, CitationSet]` |
| spec_builder | AggregateResult, citations, plan | `VisualizationResponse` |
| pipeline | `QueryRequest` | `VisualizationResponse` (never raises; errors become envelopes) |

---------------------------------------------------------------------------------------------------
## 5. Request schema (`QueryRequest`) — `POST /query`

| Field | Type | Req | Validation |
|---|---|---|---|
| query | str | yes | 3–500 chars, stripped |
| drug_name | str \| list[str] | no | ≤5; >1 becomes series |
| condition | str \| list[str] | no | ≤5; >1 becomes series |
| sponsor | str | no | non-empty |
| trial_phase | list[Phase] | no | EARLY_PHASE1, PHASE1, PHASE2, PHASE3, PHASE4, NA |
| status | list[OverallStatus] | no | API overall-status enum values |
| country | str | no | non-empty |
| start_year, end_year | int | no | 1990 ≤ y ≤ current+5; start ≤ end |
| chart_type | ChartType | no | suggestion; still checked against data |
| max_citations | int | no | 0–50, default 10 |
| debug | bool | no | default true; include `meta.trace` |

Explicit fields override the LLM's extraction; conflicts are noted in `meta.notes`.

---------------------------------------------------------------------------------------------------
## 6. Field registry (`app/registry.py`)

One `FieldDef` per field: `name, api_field (fields= short name), path (dotted path in study JSON),
kind (id|categorical|numeric|time|date|boolean), multi (single|explode|combine), filter_param
(which API param or advanced AREA, or None), enum (allowed values or None), display_name`.
Adding a field = one entry, zero pipeline changes (extensibility evidence).

| name | path (under protocolSection unless noted) | kind | multi |
|---|---|---|---|
| nct_id | identificationModule.nctId | id | single |
| phase | designModule.phases | categorical | combine (PHASE1+PHASE2 → "Phase 1/2") |
| status | statusModule.overallStatus | categorical | single |
| start_year | statusModule.startDateStruct.date | time(year) | single |
| completion_date | statusModule.completionDateStruct.date | date | single |
| last_updated | statusModule.lastUpdatePostDateStruct.date | date | single |
| country | contactsLocationsModule.locations[].country | categorical | explode + dedupe |
| condition | conditionsModule.conditions | categorical | explode |
| intervention | armsInterventionsModule.interventions[].name | categorical | explode + dedupe |
| intervention_type | armsInterventionsModule.interventions[].type | categorical | explode + dedupe |
| sponsor | sponsorCollaboratorsModule.leadSponsor.name | categorical | single, normalized |
| sponsor_class | sponsorCollaboratorsModule.leadSponsor.class | categorical | single |
| enrollment | designModule.enrollmentInfo.count | numeric | single |
| duration_months | derived: completion − start | numeric | single |
| has_results | hasResults (top level) | boolean | single |

---------------------------------------------------------------------------------------------------
## 7. Query plan (`QueryPlan`) — the ONLY thing the LLM produces

```json
{
  "mode": "aggregate | histogram | points | network",
  "filters": {"condition": "...", "drug": "...", "sponsor": "...", "country": "...",
              "status": ["RECRUITING"], "phase": ["PHASE3"], "start_year_min": 2015, "start_year_max": null},
  "series": [{"label": "Pembrolizumab", "filters": {"drug": "Pembrolizumab"}}],
  "group_by": ["phase"],
  "bin": {"field": "enrollment", "bin_width": 50},
  "x_field": null, "y_field": null, "start_field": null, "end_field": null,
  "network": {"source_field": "sponsor", "target_field": "intervention", "max_edges": 60},
  "sort": "desc | asc | natural", "limit": 10,
  "chart_type": "bar_chart",
  "search_terms": {"drug": []},
  "interpretation": "one sentence restating the question as understood",
  "unsupported_reason": null
}
```
- `series`: 1–5 groups the USER names. Buckets that come from data go in `group_by`, never series.
  No comparison = exactly one series with label "All" and empty filters.
- Validation (validator.py, before any API call): every field name exists in registry; enums exact;
  numeric group_by requires `bin`; x/y fields numeric; mode-required keys present and other modes'
  keys absent; 1 ≤ series ≤ 5; 1 ≤ len(group_by) ≤ 2; 1 ≤ limit ≤ 50; network fields categorical.
  Each failure is a `ValidationIssue(path, message)` in plain English (it is sent back to the LLM).
- LLM may set `unsupported_reason` (vague or out-of-scope question) → pipeline returns
  `status:"unsupported"` without fetching.

### Planner (planner/)
- Model from env: `PLANNER_MODEL=gpt-5.4-mini` (reasoning effort `low`); `LABEL_MODEL=gpt-5.4-nano`
  (effort `none`). Model is configurable so the eval can compare models.
- Use the OpenAI SDK's structured output with the `QueryPlan` schema (check current SDK docs for the
  exact call; do not invent parameters).
- Read-only tools, bounded loop, **max 3 tool calls**, then a plan is mandatory:
  - `list_fields()` → registry names, kinds, enum values.
  - `count_trials(filters)` → `totalCount` via `countTotal=true&pageSize=1`.
- System prompt includes: registry summary, the series-vs-group_by rule, "never add filters the user
  didn't ask for", and 5–6 few-shot examples covering aggregate, comparison, histogram, points, network,
  unsupported. Keep the prompt in `prompt.py` as a constant, not inline.
- Every tool call and LLM call is appended to `RunState.trace`.

---------------------------------------------------------------------------------------------------
## 8. Data, aggregation, citations, response

### 8.1 Normalization (ctgov/normalize.py)
`Trial` = flat typed fields from the registry + `evidence: dict[str, FieldEvidence]` where
`FieldEvidence(path: str, raw: Any)` keeps the **exact raw API value and its path** for citations
(e.g. path `protocolSection.designModule.phases`, raw `["PHASE1","PHASE2"]` while `trial.phase =
"Phase 1/2"`). For explode fields store **one evidence entry per distinct value** with its indexed
path, e.g. `contactsLocationsModule.locations[3].country = "United States"` (not all 40 sites).
Normalization rules are fixed per field; only the field list varies by question.

### 8.2 Fetching (ctgov/client.py)
- Push every supported filter to the API. Request only the plan's fields via `fields`.
- Size check first (`countTotal=true&pageSize=1`) per series. Cap `MAX_TRIALS=10000`.
  - Tier 1 (≤ cap): page through everything (`pageSize=1000`, follow `nextPageToken`).
  - Tier 2 (> cap, mode=aggregate, single group_by with an enum: phase/status/sponsor_class/
    intervention_type): one `countTotal` call per bucket for exact counts + fetch 10 sample trials per
    bucket for citations (`is_sample: true`).
  - Otherwise → `unsupported` with matched count and "narrow your question". Never use partial pages.
- In-memory TTL cache keyed by full URL (1 h). Retries: 429/5xx/timeouts, exponential backoff, 3 tries.
  async `httpx`, series fetched concurrently, semaphore 4.
- One series failing while others succeed → return successful series + a note naming the failed one.

### 8.3 Aggregation rules (aggregate/)
- Explode fields: count each trial once per distinct value (dedupe within trial first). When bar
  totals can exceed trial count, add note automatically ("Trials in multiple countries are counted
  once per country, so the bars add up to more than the N trials found.").
- Combine fields: join to one label ("Phase 1/2").
- Missing categorical → explicit "Unknown" bucket. Missing numeric / points → drop and count drops.
- Time series: zero-fill missing years between min and max. Note ESTIMATED (future) start dates.
- Histogram: `bin_start`, `bin_end`, `trial_count`.
- Sponsor names: normalize for matching (lowercase, strip Inc./Ltd/LLC/Corp/punctuation); display the
  most common original spelling.
- Sort/limit applied by code after counting. Every row records contributing `nct_ids` for citations.

### 8.4 Networks (aggregate/network.py)
- For each trial, +1 for every pair of its entities (source_field × target_field; same field twice =
  co-occurrence, unordered pairs). Node `trial_count`, edge `shared_trials`.
- Non-drug removal, cheapest first: (1) drop an intervention appearing **only** in
  PLACEBO_COMPARATOR or SHAM_COMPARATOR arms; (2) drug networks keep DRUG + BIOLOGICAL types;
  (3) name stop-list (placebo, sham, vehicle, saline, standard of care, best supportive care);
  (4) remaining unclear distinct names → ONE batched `LABEL_MODEL` call, labels from fixed enum,
  code verifies returned names ⊆ sent names, results cached forever in `data/label_cache.json`.
- Pruning: keep top `max_edges` (60) by `shared_trials`; drop weight-1 edges unless dataset < 50
  trials; drop isolated nodes. `meta.pruning = {edges_shown, edges_total, min_shared_trials, message}`
  with a plain-language message ("Showing the 60 strongest connections out of 2,314. Each line means
  the two drugs were tested together in at least 9 trials...").

### 8.5 Citations (citations.py) — deterministic, never the LLM
`citations: dict[datum_id, CitationSet]`, `CitationSet = {total, shown, is_sample, search_url,
items:[{nct_id, url, bucket_label, evidence:[{field, value}]}]}`.
- `total` exact; items capped at `max_citations`. Evidence = raw values from `Trial.evidence` for the
  field(s) the datum is about (1 line for a bar/node, 2 for an edge, x+y for a dot).
- `search_url` reproduces the filter on clinicaltrials.gov.
- Ranking (then max 2 per sponsor): recruiting filter → last_updated, enrollment; completed filter →
  has_results, enrollment; time-series bucket → enrollment, has_results; else → has_results,
  enrollment, last_updated.

### 8.6 Response (`VisualizationResponse`)
```json
{
  "status": "ok | unsupported | error",
  "query": "...",
  "visualization": {"type": "...", "title": "...", "encoding": {...}, "data": [...]},
  "citations": {"d1": {...}},
  "meta": {"plan": {...}, "interpretation": "...", "filters_applied": {...}, "search_terms": {},
           "sort": "...", "time_granularity": "year | null", "units": {"y": "trials"},
           "trials_matched": 0, "trials_used": 0, "tier": 1, "chart_overridden": false,
           "pruning": null, "notes": ["..."], "source": "clinicaltrials.gov",
           "data_timestamp": "...", "retrieved_at": "...", "trace": [...]},
  "error": null
}
```
Encoding per type (data rows always flat with `id`, except network):
| type | encoding | data row |
|---|---|---|
| bar_chart | x, y | {id, <x>, trial_count} |
| grouped_bar_chart | x, y, series | {id, <x>, series, trial_count} |
| stacked_bar_chart | x, y, stack | {id, <x>, <stack>, trial_count} |
| heatmap | x, y, color | {id, <x>, <y>, trial_count} |
| time_series | x(year), y, series? | {id, year, series?, trial_count} |
| histogram | x(bin), y | {id, bin_start, bin_end, trial_count} |
| scatter_plot | x, y | {id, nct_id, <x>, <y>} |
| gantt | y(label), start, end | {id, nct_id, label, start, end} |
| network_graph | nodes, edges | data = {nodes:[{id,label,type,trial_count}], edges:[{id,source,target,shared_trials}]} |
Each encoding channel: `{field, type: categorical|quantitative|temporal, title, unit?}`.
Chart selection (code, after counting; override LLM suggestion with a note when it doesn't fit):
1 group_by + 1 series → bar (time field → time_series); 1 group_by + 2–5 series → grouped_bar
(time → multi-series time_series); 2 group_by → stacked_bar if stack dim ≤ 8 values else heatmap.
`GET /schema` returns the JSON Schema of request and response (generated from Pydantic).

### 8.7 RunState / trace
One `RunState` passed through the pipeline; each step appends `{step, ms, ...details}`: plan
(tool_calls, retries, model, tokens), validate (errors), fetch (api_calls, tier, trials_matched,
cache_hits), aggregate (dropped counts), build (chart_overridden). Returned in `meta.trace`.

### 8.8 Failure behaviors (each needs a test or a manual-run record)
| Situation | status | content |
|---|---|---|
| zero trials | ok | empty data, note "No trials matched these filters", filters echoed |
| misspelled/brand drug | ok | count_trials-guided correction; terms in meta.search_terms |
| vague ("show me trials") | unsupported | asks for drug/condition/sponsor |
| out of scope ("which drug works best?") | unsupported | registry data can't answer efficacy |
| plan invalid after retry | unsupported | validation errors in plain words |
| over cap & not tier-2 eligible | unsupported | matched count + narrow suggestion |
| API down after retries | error | "ClinicalTrials.gov didn't respond; try again" |
| one series fails | ok | other series + note |
| LLM provider down | error | reason |

---------------------------------------------------------------------------------------------------
## 9. Testing and evaluation
- Unit tests (fixtures/hand-built Trials): normalize (phase combine, country dedupe, evidence paths),
  every aggregation rule, zero-fill, binning, network pairs + placebo removal on NCT04003636 fixture,
  pruning, citation ranking + caps, chart selection, validator with ≥8 deliberately bad plans.
- Failure tests: fake transport (`httpx.MockTransport`) for zero results, timeout, 500s, one series
  failing; LLM stubbed.
- Eval harness `evals/run_evals.py`: ~20 questions (all 9 appendix classes + 6 beyond-appendix + 3
  should-be-unsupported) with expected plan **properties** (mode, group_by, series count, key
  filters, chart type), not exact JSON. Prints pass rate, latency, approx cost; `--model` flag;
  writes `evals/results_<model>.json`. Run it for gpt-5.4-nano, gpt-5.4-mini, gpt-5.4 and put the
  comparison table in README.
- Manual: placebo filter spot-check on ~50 trials, accuracy reported in README.

---------------------------------------------------------------------------------------------------
## 10. Persistence
No database (API is authoritative). In-memory TTL cache + `data/label_cache.json`. README notes:
production would use Redis for shared cache and Postgres for traces.

## 11. Config / run
`.env`: `OPENAI_API_KEY`, `PLANNER_MODEL=gpt-5.4-mini`, `LABEL_MODEL=gpt-5.4-nano`,
`MAX_TRIALS=10000`, `CACHE_TTL_SECONDS=3600`. Start: `uv sync` then
`uv run uvicorn app.api:app --reload`. Must run from clean clone in under 2 minutes.

---------------------------------------------------------------------------------------------------
## 12. Phases (stop after each; acceptance criteria must all pass)

**Phase 1 — Skeleton & contracts.** pyproject (fastapi, uvicorn, httpx, pydantic, pydantic-settings,
openai, pyyaml, pytest, pytest-asyncio, respx optional), `models.py` (all types in sections 5–8 incl.
encodings per chart type as discriminated union on `type`), `registry.py` (full table), `config.py`,
`api.py` with `/health` and `/schema`, `.env.example`, empty TRACEABILITY.md/AI_USAGE.md from section 14.
Accept: `/schema` returns JSON Schema; tests import models; registry test asserts every path resolves
on `pembrolizumab_page.json` for at least one study.

**Phase 2 — Normalize + client.** normalize.py, client.py (size check, paging, tiers, cache,
retries, concurrency). Accept: normalize tests on all fixtures; paging test proves pageSize cap
handling; tier selection tests on count fixtures; retry/timeout tests with MockTransport.

**Phase 3 — Aggregate mode + first vertical slice (no LLM).** aggregate.py, common.py, spec_builder
(bar/grouped/stacked/heatmap/time_series), pipeline with a hand-written plan. Accept: hand plan
"pembrolizumab trials by phase" → valid `bar_chart` JSON from fixture; zero-fill test; explode note
test.

**Phase 4 — Citations.** citations.py, evidence wiring, search_url, ranking, caps, tier-2 samples.
Accept: every datum id has a CitationSet; evidence values equal raw fixture values byte-for-byte;
ranking tests per rule.

**Phase 5 — Planner + validator.** prompt, tools, bounded loop, retry-on-invalid, unsupported path,
trace. Accept: validator tests (≥8 bad plans); planner tests with stubbed LLM (tool loop stops at 3;
retry once; unsupported); one live smoke run printed in Phase Report.

**Phase 6 — Breadth.** histogram.py, points.py (scatter + gantt), chart-selection overrides.
Accept: tests per mode; overrides produce notes.

**Phase 7 — Networks.** network.py with 4-layer non-drug removal, label cache, pruning, meta.pruning.
Accept: NCT04003636 keeps gemcitabine/cisplatin, drops placebo; pruning test; label verification test
(model returns an unknown name → rejected).

**Phase 8 — Failure hardening.** All rows of 8.8. Accept: one test per automatable row; manual rows
recorded in `evals/manual_failure_runs.md` with actual output.

**Phase 9 — Evals, examples, README.** questions.yaml, run_evals.py, model comparison, 5 example runs
saved to `examples/` (one each: time series, grouped-bar comparison, geographic bar, histogram or
scatter, network), README (section 15). Accept: README checklist in section 15 fully ticked; eval
table present; `TRACEABILITY.md` has no row without evidence.

If time runs short, cut in this order: gantt → heatmap → eval set to 10 questions. Never cut
citations, trace, failure table, or README.

---------------------------------------------------------------------------------------------------
## 13. Phase Report (print this at the end of every phase, exactly these headings)
```
## Phase N Report
### Built
- file — one line on what it does
### Tests
<pytest summary line>  (+ list of new test names)
### Requirement IDs touched
R-xx: status → evidence
### Spec deviations
(none) or: what real data showed, proposed fix
### Decisions made (for README "design decisions")
- decision — why — alternative rejected
### Open questions for the user
```

---------------------------------------------------------------------------------------------------
## 14. Traceability (create TRACEABILITY.md with this table in Phase 1; update every phase)

Columns: `ID | Requirement (source) | Implemented in | Evidence (test / example / README §) | Status`

| ID | Requirement | Source |
|---|---|---|
| R-01 | Accept required `query` string | Inputs 3.1 |
| R-02 | Optional structured fields defined and documented (names, types, req/opt, validation) | Inputs |
| R-03 | Interpret question → plan | Overview 1 |
| R-04 | Retrieve data from ClinicalTrials.gov API (authoritative) | Overview 2, Data Source |
| R-05 | Decide viz needed + suitable type | Overview 3 |
| R-06 | Visualization spec: type, title, encoding, data | Outputs 3.2 |
| R-07 | Metadata: units, sorting, time granularity, grouping choices | Outputs 3.2 |
| R-08 | Notes: assumptions, filters applied, interpretation | Outputs 3.2 |
| R-09 | Response schema documented for frontend | Outputs 3.2 |
| R-10 | bar / grouped bar | Viz 4 |
| R-11 | timeline / time series | Viz 4 |
| R-12 | scatter plot | Viz 4 |
| R-13 | histogram | Viz 4 |
| R-14 | meaningful network graph | Viz 4, Rubric 15% |
| R-15 | single coherent approach, no one-off hacks | Viz 4, Rubric 15% |
| R-16 | all 9 appendix query classes work | Appendix |
| R-17 | queries beyond appendix work | Appendix |
| R-18 | per-datum citations with nct_id + exact field/value | Bonus 5 |
| R-19 | clear, rational design decisions documented | Rubric 35% |
| R-20 | maintainable structure + extensibility (registry) | Rubric 35% |
| R-21 | real-world API data handling (missing, multi-value, dates, paging cap, size tiers) | Rubric 35% |
| R-22 | avoid hallucination-prone steps (LLM plans only) | Rubric 20% |
| R-23 | validation / constraints on LLM output | Rubric 20% |
| R-24 | sensible planning + appropriate tools (bounded tool loop) | Rubric 20% |
| R-25 | readability, organization, documentation | Rubric 20% |
| R-26 | correctness and robustness (tests, failure behaviors) | Rubric 20% |
| R-27 | well-structured, unambiguous schemas | Rubric 10% |
| R-28 | frontend-friendly spec | Rubric 10% |
| R-29 | README: how to run (install, configure, start) | Submission 2 |
| R-30 | README: request/response schema docs | Submission 2 |
| R-31 | README: design decisions and tradeoffs | Submission 2 |
| R-32 | README: limitations + future work | Submission 2 |
| R-33 | 3–5 example queries with actual JSON outputs | Submission 3 |
| R-34 | README: AI tools used | Integrity 8 |
| R-35 | README: how correctness was validated | Integrity 8 |
| R-36 | README: deliberate vs generated/adapted | Integrity 8 |
| R-37 | evidence of testing and iteration (eval scores, failure table, model comparison) | Integrity 8 |
| R-38 | explicit state + trace/observability | Job posting |
| R-39 | failure cases + incomplete evidence tested | Job posting |

AI_USAGE.md columns: `File | Origin (generated / generated then edited / hand-written) | What was checked`.

---------------------------------------------------------------------------------------------------
## 15. README checklist (Phase 9 must tick every box)
- [ ] One-paragraph overview + pipeline diagram (Mermaid)
- [ ] Quick start: prerequisites, `uv sync`, `.env`, start command, sample curl
- [ ] Request schema table (section 5) + JSON example
- [ ] Response schema: envelope, encoding-per-type table, citations, meta, trace, error shapes
- [ ] Design decisions & tradeoffs: P1–P7, series vs group_by, two-tier fetch, explode vs combine,
      evidence-preserving normalization, placebo layers, network pruning, no DB, no framework, model choice
- [ ] Supported query classes (appendix + beyond) and chart types table
- [ ] Failure behaviors table with Expected / Actual / How tested (keep any ❌ with reason)
- [ ] Evaluation: pass rate per model (nano/mini/full), latency, cost; placebo spot-check accuracy
- [ ] Examples: links to the 5 files in examples/
- [ ] Limitations & future work (honest)
- [ ] AI usage & integrity: tools used, validation approach, link to AI_USAGE.md
