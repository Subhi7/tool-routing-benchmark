"""Metrics over runs/<split>_<order>/<model>.jsonl.

    python3 -m bench.report --split pilot

Definitions
- tool_acc:        accuracy on `multiple` (a tool is required)
- no_tool_acc:     accuracy on `irrelevance` (abstention)
- false_call_rate: 1 - no_tool_acc (called a tool when none was appropriate)
- balanced_acc:    mean of tool_acc and no_tool_acc (insensitive to the category mix).
                   Stands in for macro-F1, which is ill-defined when every example
                   has its own label set.
- abstain_f1:      F1 of predicting NO_TOOL, treating NO_TOOL as the positive class
- ece:             10 equal-width bins on max probability
- brier:           multi-class Brier, sum over choices of (p - onehot)^2
- acc@cov80:       accuracy on the 80% most confident predictions
- order flip:      share of examples whose predicted TOOL changes between seeded and
                   reversed choice order
"""
import argparse
import json
import math
import os

MODELS = ["jev", "laya", "gliclass"]


def load(split, order, model):
    path = f"runs/{split}_{order}/{model}.jsonl"
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else None


def wilson(k, n, z=1.96):
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def ece(rows, bins=10):
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        x = [r for r in rows if lo < r["max_probability"] <= hi or (b == 0 and r["max_probability"] == 0)]
        if x:
            conf = sum(r["max_probability"] for r in x) / len(x)
            acc = sum(r["correct"] for r in x) / len(x)
            total += len(x) / len(rows) * abs(conf - acc)
    return total


def brier(rows):
    return sum(sum((p - (cid == r["gold_id"])) ** 2 for cid, p in r["probabilities"].items())
               for r in rows) / len(rows)


def acc_at_coverage(rows, coverage):
    ranked = sorted(rows, key=lambda r: -r["max_probability"])
    kept = ranked[:max(1, round(len(ranked) * coverage))]
    return sum(r["correct"] for r in kept) / len(kept)


def metrics(rows):
    tool = [r for r in rows if r["category"] == "multiple"]
    none = [r for r in rows if r["category"] == "irrelevance"]
    acc = lambda x: sum(r["correct"] for r in x) / len(x) if x else math.nan
    pred_none = [r for r in rows if r["predicted_tool"] == "NO_TOOL"]
    tp = sum(r["gold_tool"] == "NO_TOOL" for r in pred_none)
    prec = tp / len(pred_none) if pred_none else 0.0
    rec = tp / len(none) if none else 0.0
    lat = sorted(r["latency_ms"] for r in rows)
    return dict(
        n=len(rows),
        acc=acc(rows), acc_ci=wilson(sum(r["correct"] for r in rows), len(rows)),
        tool_acc=acc(tool), no_tool_acc=acc(none), false_call_rate=1 - acc(none),
        balanced_acc=(acc(tool) + acc(none)) / 2,
        abstain_f1=2 * prec * rec / (prec + rec) if prec + rec else 0.0,
        ece=ece(rows), brier=brier(rows),
        acc_cov80=acc_at_coverage(rows, 0.8), acc_cov50=acc_at_coverage(rows, 0.5),
        latency_p50=lat[len(lat) // 2], latency_p95=lat[int(len(lat) * .95)],
        input_tokens=sum(r.get("input_tokens") or 0 for r in rows),
    )


def by_n_tools(rows):
    buckets = {"1": lambda n: n == 1, "2": lambda n: n == 2, "3": lambda n: n == 3,
               "4": lambda n: n == 4, "5-7": lambda n: 5 <= n <= 7, "8+": lambda n: n >= 8}
    out = {}
    for cat in ("multiple", "irrelevance"):
        for name, f in buckets.items():
            x = [r for r in rows if r["category"] == cat and f(r["n_tools"])]
            if x:
                out[f"{cat}:{name}"] = (sum(r["correct"] for r in x) / len(x), len(x))
    return out


def order_sensitivity(a, b):
    tb = {r["example_id"]: r for r in b}
    pairs = [(r, tb[r["example_id"]]) for r in a if r["example_id"] in tb]
    flips = sum(x["predicted_tool"] != y["predicted_tool"] for x, y in pairs)
    return dict(n=len(pairs), flip_rate=flips / len(pairs),
                acc_seeded=sum(x["correct"] for x, _ in pairs) / len(pairs),
                acc_reversed=sum(y["correct"] for _, y in pairs) / len(pairs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="pilot")
    args = ap.parse_args()
    report = {}
    for m in MODELS:
        seeded = load(args.split, "seeded", m)
        if not seeded:
            continue
        rev = load(args.split, "reversed", m)
        report[m] = dict(metrics=metrics(seeded), by_n_tools=by_n_tools(seeded),
                         order=order_sensitivity(seeded, rev) if rev else None)

    cols = ["acc", "tool_acc", "no_tool_acc", "balanced_acc", "abstain_f1", "ece", "brier",
            "acc_cov80", "latency_p50"]
    print(f"{'model':10}" + "".join(f"{c:>13}" for c in cols))
    for m, r in report.items():
        print(f"{m:10}" + "".join(f"{r['metrics'][c]:>13.3f}" if c != "latency_p50"
                                  else f"{r['metrics'][c]:>11.0f}ms" for c in cols))
    print("\n95% CI on overall acc:", {m: tuple(round(v, 3) for v in r["metrics"]["acc_ci"])
                                       for m, r in report.items()})
    print("\nChoice-order sensitivity (seeded vs reversed):")
    for m, r in report.items():
        if r["order"]:
            o = r["order"]
            print(f"  {m:10} flip {o['flip_rate']:.0%}   acc {o['acc_seeded']:.2f} -> {o['acc_reversed']:.2f}")
    print("\nAccuracy by number of candidate tools (acc, n):")
    for m, r in report.items():
        print(f"  {m:10}", {k: (round(v[0], 2), v[1]) for k, v in r["by_n_tools"].items()})

    os.makedirs("results", exist_ok=True)
    with open(f"results/{args.split}_report.json", "w") as f:
        json.dump(report, f, indent=2)


if __name__ == "__main__":
    main()
