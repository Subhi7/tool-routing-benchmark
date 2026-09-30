"""Laya via its recommended `Router` (pip `laya`), default settings.

Laya caps each choice description at 48 tokens (hard-coded) and squeezes all
choices into the checkpoint's `head_max_len` (192 English, 256 multilingual). Its `usage["truncated"]` only covers the
state, so we recompute how many tokens of each choice it actually read and log
that as `option_tokens_seen` / `option_tokens_full`.
"""
import time

import laya
from laya import Router

QUESTION = "route"
OPTION_CAP = 48      # laya/common.py: build_sequence, max_length=48


class Laya:
    name = "laya"

    def __init__(self, device=None):
        self.router = Router(device=device)
        self.tok = None
        # Warm-up (download, load, first-kernel compile) so logged latency is steady-state.
        self.router.predict("warm up", {"q": {"type": "choice", "instructions": "Pick one.",
                                              "criteria": {"A": "a", "B": "b"}}})

    def _option_budget(self, q, head_max_len):
        full = [len(self.tok(" " + t, add_special_tokens=False)["input_ids"]) for t in q["criteria"].values()]
        seen = [min(OPTION_CAP, n) + 1 for n in full]  # +1 for the [MASK] marker
        if head_max_len - sum(seen) < 16:
            per = max(4, (head_max_len - 16) // len(seen))
            seen = [min(s, per) for s in seen]
        return [s - 1 for s in seen], full

    def predict(self, r):
        q = {QUESTION: {"type": "choice", "instructions": r["instruction"],
                        "criteria": {c["id"]: c["choice_text"] for c in r["choices"]}}}
        t0 = time.perf_counter()
        raw = self.router.predict(r["context"], q)
        latency_ms = (time.perf_counter() - t0) * 1000
        # The exact tokenizer of the checkpoint the Router picked.
        agent = next(a for k, a in self.router._agents.items() if raw["routing"]["model"] in str(k))
        self.tok = agent.tok
        seen, full = self._option_budget(q[QUESTION], agent.cfg.get("head_max_len", 192))
        ans = raw["answers"][QUESTION]
        return dict(
            probabilities=ans["probabilities"],
            predicted_id=ans["choice"],
            latency_ms=latency_ms,
            input_tokens=raw["usage"]["input_tokens"],
            output_tokens=raw["usage"]["output_tokens"],
            model_version=f"laya-{laya.__version__}/{raw['routing']['repo']}",
            state_truncated=raw["usage"]["truncated"],
            option_tokens_seen=seen,
            option_tokens_full=full,
            raw=raw,
        )
