"""GLiClass (knowledgator/gliclass-large-v1.0) via its own chunking pipeline, no truncation.

The original adapter (`gliclass.py`) concatenated every candidate tool into one sequence and
right-truncated at `max_length=1024`. On long BFCL examples (many tools, or verbose parameter
schemas) that silently dropped some candidate tool definitions entirely -- including NO_TOOL,
which is always last. That is not a
fair comparison against Jev, which has no such cap, and it is not how GLiClass's own docs say
to handle label sets that don't fit: the library ships
`ZeroShotClassificationWithChunkingPipeline` with a `labels_chunk_size` parameter specifically
so large label sets are scored in chunks instead of disappearing from a too-long sequence.

## Three attempts, in order, and why the first two were wrong

1. **Multi-label scoring (independent sigmoid per label) is unusable on this checkpoint.**
   The natural reading of "give it a confidence threshold for abstention" is multi-label
   mode: sigmoid-score every tool, abstain if all are below 0.5. In practice this checkpoint
   saturates labels near 1.0 regardless of true relevance, independent of chunk size, so
   nearly every prediction would clear a 0.5 abstention threshold. Discarded.
2. **Single-label mode with `labels_chunk_size=1`** (one tool per forward pass, logits from
   every chunk concatenated and joint-softmaxed at the end -- replicating the chunking
   pipeline's own internal single-label logic, needed anyway since its public `__call__`
   only returns the argmax in single-label mode, not the full distribution `bench/report.py`
   needs for ECE/Brier). This guaranteed zero truncation and looked principled, but scoring
   each tool alone made correct and incorrect tools score nearly identically, while putting
   two or more candidates in the same chunk restored a clear preference for the correct one.
   Scoring each tool in total isolation throws away
   the within-sequence cross-attention between candidate labels (DeBERTa's encoder lets every
   token, including each label's class token, attend to every other token in the same
   sequence) that this model depends on to discriminate -- an absolute "does this look
   plausible" score per label, with nothing to compare against, is a different and much
   weaker signal than a relative one. `labels_chunk_size=1` wasn't a conservative starting
   point here, it was removing the thing that makes the task work at all.
3. **What this adapter actually does: greedily pack as many complete tool definitions into
   each chunk as fit under `MAX_LENGTH`,** preserving label order. For the large majority of
   BFCL examples (few enough tools, none pathologically verbose) every candidate -- including
   NO_TOOL -- ends up in one chunk together, getting the same full cross-attention the
   original adapter gave them, but now backed by a `MAX_LENGTH` verified never to truncate
   (see below) instead of an unverified 1024 that did. Only examples whose combined tools
   exceed that budget split across more than one chunk, and even then each chunk is as large
   as it can safely be. Logits from every chunk are concatenated in original order and
   softmaxed once at the end, exactly like the original single-pass adapter's probabilities,
   just never truncated.

NO_TOOL is included as a normal candidate label (not treated specially), for the same reason
as attempt 2: so that removing truncation is the only variable changed relative to the
original `gliclass.py` adapter, which also offered NO_TOOL as a label. Whether that is enough
to make GLiClass competitive at abstention, versus truncation being incidental to a deeper
architectural mismatch, is a question for the actual run's numbers, not asserted here.

## MAX_LENGTH

Bounded by the single longest (text + one tool) chunk across the whole benchmark, which sets
a hard floor under which packing can never do better than 1 tool per chunk: measured directly
via a one-time scan over all 1,933 examples' every (text, one-tool) pairing, that worst case
is 3,117 tokens (`live_irrelevance_435-108-1` / `get_response`). `MAX_LENGTH=4096` gives
headroom above that and is used for every chunk; truncation is verified per-row
(`any_truncated` / `truncated_tools`), not assumed, by independently re-tokenizing each tool
standalone without truncation and comparing to this constant. The underlying DeBERTa-v2
encoder's own config says `max_position_embeddings=512`, but DeBERTa's relative (bucketed)
position attention already operates past that in GLiClass's own library default of 1024
tokens; 4096 continues the same regime rather than introducing a new one.
"""
import time

import torch
from gliclass import GLiClassModel, ZeroShotClassificationWithChunkingPipeline
from transformers import AutoTokenizer

REPO = "knowledgator/gliclass-large-v1.0"
REVISION = "ae8a18644e84cfda53325bd91c7941f2f6fd0ccb"
MAX_LENGTH = 4096  # see module docstring: measured worst single-tool case across all 1933 examples is 3117


def default_device():
    if torch.cuda.is_available():
        return "cuda:0"
    return "mps" if torch.backends.mps.is_available() else "cpu"


class GLiClassChunked:
    name = "gliclass_chunked"

    def __init__(self, device=None):
        self.device = device or default_device()
        self.model = GLiClassModel.from_pretrained(REPO, revision=REVISION)
        self.tok = AutoTokenizer.from_pretrained(REPO, revision=REVISION)
        self.pipe = ZeroShotClassificationWithChunkingPipeline(
            self.model, self.tok, classification_type="single-label", device=self.device,
            progress_bar=False, max_length=MAX_LENGTH)
        self.model.eval()
        self.predict({"context": "warm up", "instruction": "Pick one.",
                      "choices": [{"id": "A", "tool": "a", "choice_text": "a"},
                                  {"id": "B", "tool": "NO_TOOL", "choice_text": "NO_TOOL\nNone."}]})

    def _sync(self):
        if self.device == "mps":
            torch.mps.synchronize()
        elif self.device.startswith("cuda"):
            torch.cuda.synchronize()

    def _pack_chunks(self, text, labels, prompt):
        """Greedily group label indices so each chunk's (text + its labels + prompt) stays
        under MAX_LENGTH, maximizing how many real candidates share cross-attention per
        forward pass. A single label that alone exceeds MAX_LENGTH still gets its own
        (truncated) chunk rather than crashing -- flagged via the standalone-length check
        in predict(), not silently retried here."""
        chunks, current = [], []
        for i, label in enumerate(labels):
            candidate = current + [i]
            built = self.pipe.prepare_input(text, [labels[j] for j in candidate], prompt=prompt)
            n = len(self.tok(built)["input_ids"])
            if current and n > MAX_LENGTH:
                chunks.append(current)
                current = [i]
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    @torch.no_grad()
    def predict(self, r):
        ids = [c["id"] for c in r["choices"]]
        labels = [c["choice_text"] for c in r["choices"]]  # NO_TOOL included, like the other models

        # Per-tool standalone length: the acceptance-gate check -- can every tool's own
        # information possibly fit, even in the worst case of being alone in its own chunk?
        standalone_lengths = [len(self.tok(self.pipe.prepare_input(r["context"], [l],
                                                                    prompt=r["instruction"]))["input_ids"])
                               for l in labels]
        truncated = [n > MAX_LENGTH for n in standalone_lengths]

        chunks = self._pack_chunks(r["context"], labels, r["instruction"])

        t0 = time.perf_counter()
        logits = [None] * len(labels)
        for chunk in chunks:
            chunk_labels = [labels[i] for i in chunk]
            inputs = self.pipe.prepare_inputs([r["context"]], chunk_labels, same_labels=True,
                                              prompt=r["instruction"])
            out = self.model(**inputs, max_num_classes=len(chunk_labels))
            chunk_logits = out.logits[0][: len(chunk_labels)].tolist()
            for i, lg in zip(chunk, chunk_logits):
                logits[i] = lg
        self._sync()
        latency_ms = (time.perf_counter() - t0) * 1000

        probs = torch.softmax(torch.tensor(logits), dim=-1).tolist()
        probabilities = dict(zip(ids, probs))
        predicted_id = max(probabilities, key=probabilities.get)

        return dict(
            probabilities=probabilities,
            predicted_id=predicted_id,
            latency_ms=latency_ms,
            input_tokens=sum(standalone_lengths),
            output_tokens=0,
            model_version=f"{REPO}@{REVISION[:8]}+chunked(single-label,packed,max_length={MAX_LENGTH})",
            any_truncated=any(truncated),
            truncated_tools=[ids[i] for i, t in enumerate(truncated) if t],
            tool_token_lengths=dict(zip(ids, standalone_lengths)),
            n_chunks=len(chunks),
            raw=dict(logits=logits),
        )
