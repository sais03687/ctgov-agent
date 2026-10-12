# Design details

This file gives the details of the design decisions in the README, section 4.

### 4.5 Real API data: size of the result

The registry gives a maximum of 1,000 trials for each page. It does not report this limit.
Thus the client always follows `nextPageToken`. Before it fetches, it asks for the count.

**Diagram 4. How many trials can the service read?**

Files: `app/ctgov/client.py`, `app/pipeline.py`

![Diagram 4. How many trials can the service read?](diagrams/04_size_decision.png)

*How to read it: Start on the left. Answer each question with yes or no. Green is an answer. Red is a refusal with offers.*

| Tier | Condition | Action |
|---|---|---|
| 1 | 20,000 trials or fewer | Read all trials. All chart types are possible. |
| 2 | More than 20,000, and the plan counts by one fixed-value field | Ask the registry for the exact count of each bucket, plus ten sample trials for each bucket. |
| None | All other cases | Give `unsupported` with the count and with narrower versions of the question. |

Notes:

- Tier 2 fields are phase, status, sponsor category, and intervention type.
- Tier 2 phase buckets do not overlap. "Phase 1" does not include "Phase 1/2".
- A live check on 14,613 lung cancer trials gave buckets with a sum of exactly 14,613.
- The limit of 20,000 is a tradeoff between complete data and response time.
  The specification said 10,000. A real question with 14,613 trials failed, so the limit increased.

### 4.6 Real API data: rate limit

The registry does not document a rate limit. Measurements on 2026-10-11 showed:

- Request 11 in approximately 10 seconds gets HTTP 429.
- There is no `Retry-After` header.
- The block continues for 8 to 16 seconds.

Design:

- The client uses a **sliding window**: 8 requests in 10 seconds for the full process.
- An HTTP 429 stops all requests. The client waits 4 s, then 16 s. It obeys `Retry-After` if it is there.
- A timeout or an HTTP 5xx gets a retry after 0.5 s, then 1 s.
- Each wait time has a random addition, so that parallel retries do not start together.

Only a live run found this limit. Tests with a mock server cannot show it.

### 4.7 Real API data: cache

To read trials is the slow part: approximately one page of 1,000 trials each second.
Two decisions prevent a second read. They do not change a number.

1. **The client fetches each page with all fields.** Thus the pages for one subject answer
   different questions: phase, country, year, scatter plot, and network.
   The cost is small. A page is 1.4 MB, not 0.4 to 0.8 MB, and the time is the same.
2. **The client keeps each response in memory until the registry publishes new data.** This
   occurs approximately one time each day. The client keeps the last 48 responses.

The cache is in memory only. It is empty after a restart. There is no database and no file,
as the design says.

Measured on 14,613 lung cancer trials (2026-10-12):

- The first question took 22 s.
- Five other questions about the same trials took 5 to 8 s each. They made no registry calls.
- Approximately half of that time is the planner LLM. The other half is the count of 14,613 trials.

Tradeoff: an earlier version also wrote the responses to disk, so that they stayed after a
restart. It was removed. It added code and a folder of 256 MB, and a grader starts the service
one time.

### 4.8 Real API data: values and names

| Problem in the data | Decision |
|---|---|
| A trial has two or more countries, conditions, or drugs. | **Explode**: the trial counts one time for each value. The response says that bars can add up to more than the number of trials. |
| A trial has two phases. | **Combine**: `PHASE1` + `PHASE2` becomes the label "Phase 1/2". |
| The registry uses codes such as `ACTIVE_NOT_RECRUITING`. | The response shows display labels only. The raw code stays in the citation evidence. |
| One name has different spellings, for example "Non-small Cell Lung Cancer". | Spellings that are different only in capital letters or hyphens become one bar. A note says so. |
| Two different words can have the same meaning, for example "MTX". | The service does **not** merge them. The service cannot make sure that the meaning is the same. |
| A trial has no value for a field. | The trial goes in the "Unknown" bucket. A note gives the count. |
| A study record cannot be read. | The fetch fails. To skip the record would change the counts. |

Normalization changes the raw study JSON into a flat `Trial`. For each field, it keeps the raw
value and its exact path, for example `…locations[3].country`. Citations use these values.

### 4.9 Networks: removal of items that are not drugs

The registry gives a placebo the type `DRUG`. Thus the type cannot remove placebos.
Drug nodes go through four layers. The most reliable layer is first.

| Layer | Rule |
|---|---|
| 1. Arms | Remove an item that is given **only** in placebo or sham arms. |
| 2. Type | Keep only the types `DRUG` and `BIOLOGICAL`. |
| 3. Stop-list | Remove names that contain placebo, sham, vehicle, saline, standard of care, or best supportive care. |
| 4. Label model | For the names that remain in the chart, one LLM call gives each name a label from a fixed list. |

- Each layer sees only the items that the layers before it kept.
- The response reports the count of removals for each layer.
- Layer 4 refuses a returned name that it did not send. The labels are saved in `data/label_cache.json`.
- If the label model fails, the network comes from layers 1 to 3, with a note.

**Diagram 6. How a network removes items that are not drugs**

Files: `app/aggregate/network.py`, `app/aggregate/labels.py`

![Diagram 6. How a network removes items that are not drugs](diagrams/06_network_layers.png)

*How to read it: Read from top to bottom. Each layer removes some names (gray boxes). The counts are from the glioblastoma example.*

In the glioblastoma example, the layers removed 27, 881, 8, and 1 names.
Thus the LLM decides very little.

**Network pruning.** A condition can have thousands of connections. The service keeps the 60
strongest connections and tells the user: *"Showing the 60 strongest connections out of 1,607.
Each line means the two drugs were used together in at least 3 trials."*

### 4.10 Other decisions and tradeoffs

| Decision | Reason | Tradeoff |
|---|---|---|
| No database. | The registry is the authority and changes each day. | The first question about a subject is slow. |
| No agent framework. | The flow is fixed. A framework adds layers but no function. | More code to write for the tool loop (approximately 30 lines). |
| Code selects the chart type after it counts. | The best chart depends on the data, for example the number of values. | The LLM chart suggestion is only a suggestion. |
| Code writes the title from the plan filters. | A title cannot name a filter that the service did not apply. | Titles are plain. |
| The service stores nothing at the LLM provider (`store=False`). | Privacy. It also works with Zero Data Retention accounts. | Each LLM call sends the full conversation. |
| The service examines each suggested question. | An offered question that fails is worse than no offer. | A refusal takes 6 to 12 s, not 3 s. |

---

Back to the [README](../README.md).
