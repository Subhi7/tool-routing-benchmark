# Tool-routing benchmark: Jev vs Laya vs GLiClass

Can small "System 1" decision models pick the right tool — or correctly pick
none — without an LLM? This repo benchmarks three of them on BFCL Live tool
selection, with accuracy, abstention, calibration, latency and cost.

## Benchmark (frozen)

Source: [BFCL](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard)
v4 Live data (Apache-2.0), pinned to the Gorilla commit in
`data/bfcl_raw/GORILLA_COMMIT`. Raw files are vendored unchanged in `data/bfcl_raw/`.

| Slice | Examples | Gold |
|---|---|---|
| `live_multiple` | 1,053 | the one tool BFCL's ground truth calls |
| `live_irrelevance` | 880 | `NO_TOOL` |
| **Total** | **1,933** | |

- 4 irrelevance examples with zero tools are excluded (only `NO_TOOL` could be
  offered). Every exclusion is listed in `data/manifest.json`.
- Candidate tools per example range from 1 to 37 (see `n_tools_hist` in the manifest).
- 87 examples are multi-message (system prompt or prior turns); the full
  conversation is shown verbatim.
- `data/manifest.json` records the SHA-256 of `benchmark_full.jsonl`. Any change
  to the benchmark after results exist must be called out explicitly.

```bash
python3 -m bench.convert
```

## Canonical model input (`bench/render.py`)

```text
User request:
<original request>

Available tools:

A
Name: ...
Description: ...
Parameters: {...verbatim JSON schema...}

B
...

C
NO_TOOL
None of the available tools should be used.
```

- **`NO_TOOL` is offered on every example**, including `multiple` ones, so its
  presence never reveals the category.
- `NO_TOOL` is always the last choice. Tools are shuffled with a per-example seed
  (`order="seeded"`); `order="reversed"` reverses the tool order for the
  choice-order sensitivity check.
- Models choose a letter; `choices` maps letters back to tool names. Adapters that
  need per-choice label text (e.g. GLiClass) get `choice_text`, which is exactly
  the block under that letter, so all models receive the same information.

## Protocol

1. Freeze benchmark ✅
2. Canonical input ✅
3. Lock models: Jev (TypeSafe `/v1/systemone`), Laya `Router`, `knowledgator/gliclass-large-v1.0`. No fine-tuning, no BFCL examples in prompts.
4. 100-example pilot (`data/pilot_100.jsonl`, 50 + 50, seed 20260928); inspect every disagreement.
5. Choice-order sensitivity: seeded vs reversed on the pilot.
6. Full run; log raw responses, per-choice probabilities, latency, tokens.
7. Metrics: accuracy (overall / tool / no-tool), false tool-call rate, macro-F1, ECE, Brier, accuracy-vs-coverage.
8. Breakdowns by number of candidate tools.
9. Cost: Jev from billed input tokens × published price. Laya/GLiClass run locally on a MacBook M5 Pro; we report their throughput on that machine and no dollar cost.
10. Latency reported as observed end-to-end in this setup, separate from cost.

Frozen decisions and pilot findings: [PILOT.md](PILOT.md).
