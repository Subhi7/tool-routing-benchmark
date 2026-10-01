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

## Models

| Model | Adapter | Notes |
|---|---|---|
| Jev | `bench/models/jev.py` | TypeSafe API `POST /v1/systemone`, `model="jev-latest"`. Requires `TYPESAFE_API_KEY` in `.env` (billed per call; see Cost below). No input-length cap on our side. |
| Laya | `bench/models/laya.py` | `laya.Router()` defaults. Each candidate tool description is hard-capped at 48 tokens by the library itself (`laya/common.py`, unconditional, no exposed override in the latest release) — logged per row as `option_tokens_seen` / `option_tokens_full`, not silently absorbed into `usage.truncated`. |
| GLiClass (default) | `bench/models/gliclass.py` | `knowledgator/gliclass-large-v1.0`, single-label softmax, one combined sequence right-truncated at 1,024 tokens. The library used the simplest way — representative of an out-of-the-box deployment, truncation and all. |
| GLiClass (chunked) | `bench/models/gliclass_chunked.py` | Same checkpoint, using the library's own `ZeroShotClassificationWithChunkingPipeline` to score candidates in token-budget-safe chunks instead of one truncated sequence — see the module's own docstring for why a naive `labels_chunk_size=1` doesn't work and what does. Use this one for any "did GLiClass actually see the full tool definitions" comparison; use the default adapter for an out-of-the-box baseline. |

No fine-tuning, no BFCL examples in any prompt, for any model.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-lock.txt
cp .env.example .env   # fill in TYPESAFE_API_KEY (only needed for the jev model)
```

Laya and GLiClass run locally (CPU/CUDA/Apple MPS auto-detected); Jev calls the TypeSafe API.

## Running the benchmark

```bash
# one-time: build the frozen benchmark + pilot split
python3 -m bench.convert

# smoke test any model on 1 example before committing to a full run
python3 -m bench.run --model jev --split pilot --order seeded --limit 1

# pilot (100 examples) or full (1,933), per model, per choice order
python3 -m bench.run --model <jev|laya|gliclass|gliclass_chunked> --split <pilot|full> --order <seeded|reversed>

# blind irrelevance sub-labeling (no_relevant_tool vs relevant_but_uncallable) — see
# bench/tag_irrelevance.py's own docstring for the rule and how TAU was picked
python3 -m bench.tag_irrelevance --split full

# metrics table + results/<split>_report.json
python3 -m bench.report --split <pilot|full>
```

`bench.run` is resumable: it skips `example_id`s already present in the output file, so a
rerun only fills in what's missing. To force a genuine rerun, move or delete the existing
`runs/<split>_<order>/<model>.jsonl` first.

## Protocol

1. Freeze benchmark ✅
2. Canonical input ✅
3. Lock models: Jev, Laya, GLiClass (default + chunked). No fine-tuning, no BFCL examples in prompts. ✅
4. 100-example pilot (`data/pilot_100.jsonl`, 50 + 50, seed 20260928); inspect every disagreement. ✅
5. Choice-order sensitivity: seeded vs reversed, on the pilot and on the full run. ✅
6. Full run (1,933 examples, both orders, all four adapters); raw responses, per-choice probabilities, latency, and tokens logged per row. ✅
7. Metrics: accuracy (overall / tool / no-tool), false-call rate, balanced accuracy, abstain-F1, ECE, Brier, accuracy-vs-coverage. ✅ — `bench/report.py`.
8. Breakdowns by number of candidate tools. ✅ — `bench/report.py`'s `by_n_tools`.
9. Cost: Jev from billed input tokens (logged per row as `input_tokens`). Laya/GLiClass run
   locally with no rented GPU (explicit decision) — reported as latency/throughput on the
   machine they ran on, not converted to a dollar figure.
10. Latency reported as observed end-to-end in this setup (API round-trip for Jev, on-device
    inference for Laya/GLiClass) — not a portable cross-model speed comparison. ✅
