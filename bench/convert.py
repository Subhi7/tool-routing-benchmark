"""Step 1: freeze the benchmark.

Converts BFCL v4 Live `multiple` + `irrelevance` into one frozen JSONL file of
routing decisions and draws a seeded 50/50 pilot.

    python -m bench.convert

Original messages and tool definitions are kept verbatim. Exclusions are
logged in data/manifest.json, never dropped silently.
"""
import hashlib
import json
import os
import random
from collections import Counter

from bench.render import NO_TOOL, render

RAW = "data/bfcl_raw"
OUT = "data"
SEED = 20260928
PILOT_PER_CATEGORY = 50


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def flatten(question):
    # BFCL `question` is a list of turns, each a list of messages. Live data is single-turn.
    return [m for turn in question for m in turn]


def convert():
    examples, excluded = [], []
    answers = {a["id"]: a["ground_truth"] for a in
               read_jsonl(f"{RAW}/possible_answer/BFCL_v4_live_multiple.json")}

    for ex in read_jsonl(f"{RAW}/BFCL_v4_live_multiple.json"):
        gt = answers[ex["id"]]
        names = [t["name"] for t in ex["function"]]
        gold = next(iter(gt[0])) if len(gt) == 1 else None
        if gold is None or gold not in names:
            excluded.append(dict(id=ex["id"], reason="gold_missing_or_not_single_call"))
            continue
        if names.count(gold) > 1:
            excluded.append(dict(id=ex["id"], reason="gold_name_duplicated"))
            continue
        examples.append(dict(id=ex["id"], category="multiple", messages=flatten(ex["question"]),
                             tools=ex["function"], gold=gold))

    for ex in read_jsonl(f"{RAW}/BFCL_v4_live_irrelevance.json"):
        if not ex["function"]:
            # Only NO_TOOL would be offered: a forced, uninformative decision.
            excluded.append(dict(id=ex["id"], reason="zero_tools"))
            continue
        examples.append(dict(id=ex["id"], category="irrelevance", messages=flatten(ex["question"]),
                             tools=ex["function"], gold=NO_TOOL))

    for e in examples:
        e["n_tools"] = len(e["tools"])
        e["multi_message"] = len(e["messages"]) > 1
        e["user_request"] = [m["content"] for m in e["messages"] if m["role"] == "user"][-1]
    return examples, excluded


def main():
    examples, excluded = convert()
    full_path = f"{OUT}/benchmark_full.jsonl"
    write_jsonl(full_path, examples)

    rng = random.Random(SEED)
    pilot = []
    for cat in ("multiple", "irrelevance"):
        pilot += rng.sample([e for e in examples if e["category"] == cat], PILOT_PER_CATEGORY)
    write_jsonl(f"{OUT}/pilot_100.jsonl", pilot)

    # Human-readable pilot prompts, exactly as models will see them.
    with open(f"{OUT}/pilot_100_rendered.md", "w", encoding="utf-8") as f:
        for e in pilot:
            r = render(e, seed=SEED)
            f.write(f"## {e['id']}  ({e['category']}, {e['n_tools']} tools)\n\n"
                    f"**Gold:** {r['gold_id']} = `{e['gold']}`\n\n```text\n{r['prompt']}\n```\n\n")

    lengths = sorted(len(render(e, seed=SEED)["prompt"]) for e in examples)
    n_tools = Counter((e["category"], e["n_tools"]) for e in examples)
    manifest = dict(
        source="ShishirPatil/gorilla bfcl_eval/data @ " + open(f"{RAW}/GORILLA_COMMIT").read().strip(),
        seed=SEED,
        total=len(examples),
        counts=dict(Counter(e["category"] for e in examples)),
        multi_message=sum(e["multi_message"] for e in examples),
        excluded=excluded,
        n_tools_hist={f"{c}:{n}": k for (c, n), k in sorted(n_tools.items())},
        prompt_chars=dict(p50=lengths[len(lengths) // 2], p95=lengths[int(len(lengths) * .95)],
                          max=lengths[-1]),
        benchmark_sha256=hashlib.sha256(open(full_path, "rb").read()).hexdigest(),
        pilot_ids=[e["id"] for e in pilot],
    )
    with open(f"{OUT}/manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(json.dumps({k: v for k, v in manifest.items() if k != "pilot_ids"}, indent=2))


if __name__ == "__main__":
    main()
