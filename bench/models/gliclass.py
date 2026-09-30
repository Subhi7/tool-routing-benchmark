"""GLiClass (knowledgator/gliclass-large-v1.0), zero-shot, single-label.

Uses the library's own input builder (text, then <<LABEL>>choice ..., <<SEP>>,
then the instruction as `prompt`) and forward pass, but softmaxes the logits
ourselves so we keep the full distribution (the pipeline returns only the top
label in single-label mode).

The sequence is right-truncated at max_length=1024. Because this checkpoint
puts text first, long inputs lose the LAST labels, and NO_TOOL is always last.
We log how many label markers survived (`labels_visible`).
"""
import time

import torch
from gliclass import GLiClassModel, ZeroShotClassificationPipeline
from transformers import AutoTokenizer

REPO = "knowledgator/gliclass-large-v1.0"
REVISION = "ae8a18644e84cfda53325bd91c7941f2f6fd0ccb"


def default_device():
    if torch.cuda.is_available():
        return "cuda:0"
    return "mps" if torch.backends.mps.is_available() else "cpu"


class GLiClass:
    name = "gliclass"

    def __init__(self, device=None):
        self.device = device or default_device()
        self.model = GLiClassModel.from_pretrained(REPO, revision=REVISION)
        self.tok = AutoTokenizer.from_pretrained(REPO, revision=REVISION)
        wrapper = ZeroShotClassificationPipeline(self.model, self.tok, classification_type="single-label",
                                                 device=self.device, progress_bar=False)
        self.pipe = wrapper.pipe
        self.label_id = self.tok.convert_tokens_to_ids(self.pipe.label_token)
        self.model.eval()
        self.predict({"context": "warm up", "instruction": "Pick one.",
                      "choices": [{"id": "A", "choice_text": "a"}, {"id": "B", "choice_text": "b"}]})

    def _sync(self):
        if self.device == "mps":
            torch.mps.synchronize()
        elif self.device.startswith("cuda"):
            torch.cuda.synchronize()

    @torch.no_grad()
    def predict(self, r):
        ids = [c["id"] for c in r["choices"]]
        labels = [c["choice_text"] for c in r["choices"]]
        full_len = len(self.tok(self.pipe.prepare_input(r["context"], labels, prompt=r["instruction"]))["input_ids"])
        t0 = time.perf_counter()
        inputs = self.pipe.prepare_inputs([r["context"]], labels, same_labels=True, prompt=r["instruction"])
        out = self.model(**inputs, max_num_classes=len(labels))
        logits = out.logits[0][: len(labels)].float()
        probs = torch.softmax(logits, dim=-1).tolist()
        self._sync()
        latency_ms = (time.perf_counter() - t0) * 1000
        n_tokens = int(inputs["input_ids"].shape[1])
        visible = int((inputs["input_ids"][0] == self.label_id).sum())
        probabilities = dict(zip(ids, probs))
        return dict(
            probabilities=probabilities,
            predicted_id=max(probabilities, key=probabilities.get),
            latency_ms=latency_ms,
            input_tokens=n_tokens,
            output_tokens=0,
            model_version=f"{REPO}@{REVISION[:8]}",
            input_tokens_full=full_len,
            truncated=full_len > n_tokens,
            labels_visible=visible,
            labels_total=len(labels),
            raw=dict(logits=logits.tolist()),
        )
