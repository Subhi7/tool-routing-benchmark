"""Run one model over a split, one JSONL row per prediction. Resumable.

    python3 -m bench.run --model jev --split pilot --order seeded
    python3 -m bench.run --model jev --split pilot --limit 1   # smoke test

Output: runs/<split>_<order>/<model>.jsonl
"""
import argparse
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from bench.convert import SEED
from bench.render import render

SPLITS = {"pilot": "data/pilot_100.jsonl", "full": "data/benchmark_full.jsonl"}


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k, v)


def get_model(name):
    if name == "jev":
        from bench.models.jev import Jev
        return Jev()
    if name == "laya":
        from bench.models.laya import Laya
        return Laya()
    if name == "gliclass":
        from bench.models.gliclass import GLiClass
        return GLiClass()
    raise ValueError(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--split", default="pilot", choices=SPLITS)
    ap.add_argument("--order", default="seeded", choices=["seeded", "reversed", "original"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    if args.model != "jev":
        args.workers = 1  # local models: sequential, so latency is per-request
    load_env()

    examples = [json.loads(l) for l in open(SPLITS[args.split])][:args.limit]
    out_dir = f"runs/{args.split}_{args.order}"
    os.makedirs(out_dir, exist_ok=True)
    out_path = f"{out_dir}/{args.model}.jsonl"
    done = set()
    if os.path.exists(out_path):
        done = {json.loads(l)["example_id"] for l in open(out_path)}
    todo = [e for e in examples if e["id"] not in done]
    print(f"{args.model} {args.split}/{args.order}: {len(done)} done, {len(todo)} to run")

    model = get_model(args.model)
    lock = threading.Lock()

    def one(e):
        r = render(e, order=args.order, seed=SEED)
        p = model.predict(r)
        tool_of = {c["id"]: c["tool"] for c in r["choices"]}
        missing = set(tool_of) - set(p["probabilities"])
        return dict(
            example_id=e["id"], model=args.model, category=e["category"], n_tools=e["n_tools"],
            order=args.order, gold_tool=e["gold"], gold_id=r["gold_id"],
            predicted_id=p["predicted_id"], predicted_tool=tool_of.get(p["predicted_id"]),
            correct=p["predicted_id"] == r["gold_id"],
            probabilities=p["probabilities"], max_probability=max(p["probabilities"].values()),
            missing_choice_probs=sorted(missing),
            choice_map=tool_of, **{k: v for k, v in p.items()
                                   if k not in ("probabilities", "predicted_id")},
        )

    n_ok = n_err = 0
    with open(out_path, "a", encoding="utf-8") as f, ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(one, e): e for e in todo}
        for fut in as_completed(futures):
            try:
                row = fut.result()
            except Exception as ex:  # log and continue; rerun resumes missing ids
                n_err += 1
                print(f"ERROR {futures[fut]['id']}: {ex}")
                continue
            with lock:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
            n_ok += 1
    print(f"wrote {n_ok}, errors {n_err} -> {out_path}")


if __name__ == "__main__":
    main()
