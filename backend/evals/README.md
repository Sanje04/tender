# Tool-selection eval

Measures the one thing this agent has to get right before anything else matters:
**given a user message, does the model reach for the correct tool?**

Unit tests already cover what happens once a tool is chosen — dispatch, argument
validation, the failure paths (`tests/test_mcp_server_tools.py`,
`tests/test_mcp_tool_calling.py`). None of them can tell you whether the model
picks `get_spending_summary` over `search_transactions` for "how much did I
spend on groceries", because that is a property of the model and the prompt, not
of the code. That gap is what this fills.

## Running it

Needs a local Ollama with the model in `backend/.env`. From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m evals.runner              # one pass, ~38 cases
.\.venv\Scripts\python.exe -m evals.runner --repeat 3   # three passes + spread
.\.venv\Scripts\python.exe -m evals.runner --group totals
.\.venv\Scripts\python.exe -m evals.runner --model llama3.1:8b
```

Reports land in `evals/reports/` as timestamped JSON, one file per run, with
every case's prompt, expected tool, chosen tool, and timing. They are committed
on purpose: a number in the README that nobody can trace back to a run is not
evidence of anything.

## What it actually drives

The real `agent.run()` — same `SYSTEM_PROMPT`, same one-round tool loop, same
delete gate. Two things are replaced:

| Replaced | Why |
|---|---|
| `mcp_client.ensure_tools` | Returns schemas translated from `mcp_server.TOOL_DEFINITIONS` through the real `_to_ollama_schema`, so no MCP server process is needed and the model sees exactly the production schemas |
| `mcp_client.call_tool` | Returns a canned result and records the invocation, so no MongoDB is needed and no delete is ever real |

**Ollama is not mocked.** That is the whole point.

By default the second Ollama call — the one that turns a tool result into prose —
is skipped, because nothing here scores the prose and it roughly doubles the
wall clock. The tool decision is made entirely by the first call, and the delete
gate and invocation record both run before the second one. `--full-reply` turns
it back on when you want to read the answers.

## The two numbers

**Selection accuracy** — did the model's choice match the label, across 38
prompts in six groups. `None` is a valid label: "Should I open a Roth IRA?"
should call nothing, and a model that reaches for `get_spending_summary` there
is failing in a way that a dataset of only tool-shaped prompts would hide.

**Delete-gate behaviour** — reported separately, because it is a property of our
code rather than the model's. Every unconfirmed delete must produce zero MCP
invocations. The report splits that into *the model declined on its own* and
*the gate had to stop it*, since "the gate held" is trivially true on a run where
the model never tried.

The runner records both what the model **asked for** and what actually **reached**
the tool layer, because those differ on exactly the cases worth caring about.

## Reading the accuracy number honestly

- **It is sampled.** One pass over 38 prompts has real variance. Use `--repeat`
  and quote the mean with its spread; a single pass is a spot check.
- **It is per-model.** The number belongs to whatever `OLLAMA_MODEL` names, and
  models differ enormously at tool selection. The report records the model.
- **Prompts are hand-written by the same person who wrote the tool
  descriptions**, which is a real bias. It measures whether the descriptions and
  system prompt hold up against phrasings we thought of — not against a user who
  has never seen them.
- **The traps are the interesting part.** Six cases are written to pull the wrong
  way, most of them totals phrased like lookups ("add up everything I spent at
  Amazon last month"). Group accuracy on `totals` is a better signal of prompt
  quality than the headline.

## Adding cases

Append an `EvalCase` to `CASES` in `dataset.py`. Label what the agent *should*
do, not what it currently does — a dataset edited to match observed behaviour
stops being a measurement. If a case fails and the label turns out to be wrong,
fix the label and say so in a comment; `dataset.py` carries one such correction
already, on the unconfirmed-delete cases.
