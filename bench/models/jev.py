"""Jev via TypeSafe POST /v1/systemone (spec vendored in docs/vendor/)."""
import json
import os
import time
import urllib.error
import urllib.request

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
QUESTION = "route"


class Jev:
    name = "jev"

    def __init__(self, model=MODEL):
        self.model = model
        self.key = os.environ["TYPESAFE_API_KEY"]

    def request_body(self, r):
        return {
            "model": self.model,
            "state": r["context"],
            "questions": {QUESTION: {
                "type": "choice",
                "instructions": r["instruction"],
                "criteria": {c["id"]: c["choice_text"] for c in r["choices"]},
            }},
        }

    def predict(self, r, retries=5):
        body = json.dumps(self.request_body(r)).encode()
        for attempt in range(retries):
            req = urllib.request.Request(URL, data=body, method="POST", headers={
                "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = json.loads(resp.read())
                latency_ms = (time.perf_counter() - t0) * 1000
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"HTTP {e.code}: {e.read()[:500]!r}") from e
        ans = raw["answers"][QUESTION]
        return dict(
            probabilities=ans["probabilities"],
            predicted_id=ans["choice"],
            latency_ms=latency_ms,
            input_tokens=raw["usage"]["input_tokens"],
            output_tokens=raw["usage"]["output_tokens"],
            model_version=raw["model"],
            raw=raw,
        )
