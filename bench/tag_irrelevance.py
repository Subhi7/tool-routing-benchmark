"""Irrelevance sub-labels (a decision frozen before the full run).

BFCL's `NO_TOOL` gold label conflates two different situations:

  no_relevant_tool        None of the offered tools have anything to do with the request.
  relevant_but_uncallable A relevant tool is offered, but it can't be called as-is:
                           a required argument is missing from the request, or the
                           request would need more than one call.

This script assigns that sub-label to every `irrelevance` example, blind to any
model's predictions (Jev/Laya/GLiClass never enter this computation) -- it only
looks at the request text and the offered tool schemas, exactly as BFCL wrote them.

## The rule

1. **Relevance.** Build a text bag per tool: its name (snake/camel/dot-split into
   words) plus its description, plus the descriptions of every parameter, walked
   recursively through nested `dict`/`array` schemas. This matters because several
   BFCL tools are generic wrappers (`requests.get`) whose actual domain
   ("Date Nager holiday API", "Open-Meteo weather API", ...) only shows up in a
   parameter description, not the top-level tool description.

   Fit one TF-IDF vectorizer (unigrams + bigrams, English stopwords removed, a
   lightweight suffix-stripping stemmer -- see `stem()` -- so "tickets"/"ticket" and
   "buses"/"bus" collide) over every request text and every distinct tool text bag
   in the irrelevance split, so idf weights reflect the real corpus rather than one
   example at a time. Without stemming, plural/verb-form mismatches and idf discounts
   on common domain words (e.g. "bus" appears in 25 Bus-domain tools) pull genuinely
   relevant pairs below any reasonable threshold -- "I need to buy some bus tickets"
   against `Buses_3_BuyBusTicket` scored 0.099 unstemmed vs 0.30+ stemmed.

   For each example, cosine-similarity the request vector against each offered
   tool's vector; take the max. If `max_similarity < TAU`, no offered tool is
   relevant -> `no_relevant_tool`. Otherwise the highest-scoring tool is the
   "candidate" and the example is `relevant_but_uncallable` (BFCL's own gold label
   already says the candidate wasn't actually called).

   TAU was picked by inspecting the similarity histogram plus a stratified sample
   across similarity bins (see `calibrate()` below), not by hand-labeling ground
   truth, since none exists yet. A sample of ~30 examples spread across
   [0, 0.22) showed: below ~0.03 the bin is dominated by genuinely unrelated tools
   (a math word problem offered `multiply`, an IP-lookup request offered a weather
   tool); from ~0.03 up, most examples are clear topical matches with missing
   arguments ("Weather in zip code 30022" / `get_weather_forecast` at 0.056, "add a
   new sql server at http://plgah.ca" / `add_postgres_server` at 0.067, "I want to
   find three tickets for a round trip flight..." / `Flights_4_SearchRoundtripFlights`
   at 0.114). TAU=0.03 (559 no_relevant_tool / 321 relevant_but_uncallable on the
   full 880) sits just above the true-negative-dominated tail. Recorded as a single
   frozen constant rather than a magic literal buried in a conditional.

   Known limitation: the tokenizer and stopword list are English-only, so
   non-English requests are systematically underscored regardless of true
   relevance -- "Cual va a ser el clima en la cdmx?" (Spanish for "what will the
   weather be in Mexico City") scored 0.000 against a weather tool, and "play cha
   cha cha by kaarija" scored only 0.030 against `play_artist` because of the
   foreign artist name. These land in `no_relevant_tool` by the rule as written;
   a manual pass over non-English requests would likely reclassify some of them.

2. **Why uncallable (auxiliary, not part of the 2-way label).** For the candidate
   tool's required parameters, flag ones the request text doesn't plausibly supply:
   date/time cues (month names, weekdays, "today"/"tomorrow", year and date-like
   digit patterns), location cues ("City, State"/"City, Country" patterns, or 2+
   consecutive capitalized words -- deliberately requires more than one capitalized
   word, since a single one is too often a month name or plain sentence-initial
   capitalization: an early version of this rule matched "7th of March" as
   satisfying a `from_city` parameter), identifier cues (quoted strings or emails
   only -- an earlier version also accepted any capitalized word, which matched
   unrelated proper nouns in system-prompt-style requests), numeric cues (digits)
   for count/amount/price-like params, and a generic fallback of the parameter
   name's own words appearing in the request.
   `missing_required_params` lists the ones that fail every applicable cue.
   If that list comes up empty for a `relevant_but_uncallable` example -- the
   request seems to supply everything the candidate tool needs -- the row is
   flagged `looks_fully_specified: true`. That's the "needs multiple calls" case,
   or possibly a mislabeled BFCL example: worth a
   manual look, but still `relevant_but_uncallable` per the definition above.

Usage:

    python3 -m bench.tag_irrelevance                    # tag all 880, write output
    python3 -m bench.tag_irrelevance --calibrate         # print the threshold sweep
    python3 -m bench.tag_irrelevance --split pilot       # tag just the 50-example pilot slice

Output: data/irrelevance_subtags.jsonl, one row per irrelevance example:
    {id, sub_label, max_similarity, candidate_tool, missing_required_params,
     looks_fully_specified}
"""
import argparse
import json
import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

TAU = 0.03  # see calibrate(); similarities below this -> no offered tool is topically relevant

DATE_RE = re.compile(
    r"\b(\d{4}|\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?|today|tomorrow|yesterday|tonight|"
    r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|aug(ust)?|"
    r"sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?|"
    r"mon(day)?|tue(sday)?|wed(nesday)?|thu(rsday)?|fri(day)?|sat(urday)?|sun(day)?|"
    r"next week|this week|next month|weekend)\b", re.I)
CITY_STATE_RE = re.compile(r"\b[A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*,\s*[A-Z][a-zA-Z]+\b")
# 2+ consecutive capitalized words ("Los Angeles", "New York") -- deliberately excludes a
# single capitalized word, which is too often a month name or plain sentence capitalization.
MULTIWORD_PROPER_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
QUOTED_RE = re.compile(r"[\"'][^\"']{2,40}[\"']")
DIGIT_RE = re.compile(r"\d")

DATE_WORDS = {"date", "time", "year", "when", "day", "month", "schedule", "deadline"}
LOCATION_WORDS = {"city", "location", "address", "country", "latitude", "longitude",
                   "lat", "lon", "lng", "zip", "place", "region", "destination", "origin"}
IDENTIFIER_WORDS = {"id", "name", "username", "email", "identifier", "user"}
NUMERIC_WORDS = {"number", "amount", "count", "quantity", "price", "age", "num",
                  "total", "quantity", "size", "limit"}

STOPWORDS_EXTRA = {"the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "at",
                    "is", "are", "was", "were", "be", "with", "this", "that"}


def split_identifier(name):
    """'from_city' / 'fromCity' / 'requests.get' -> 'from city requests get'."""
    name = name.replace(".", " ").replace("_", " ")
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return name.lower()


def stem(word):
    """Minimal suffix stripper (not Porter) so plurals/verb forms collide:
    tickets->ticket, buses->bus, traveling->travel. Good enough for cosine
    similarity; not meant to be linguistically precise."""
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("es"):
        stem2 = word[:-2]
        if stem2.endswith(("s", "x", "z", "o", "ch", "sh")):
            return stem2
        return word[:-1]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]
    if word.endswith("ed") and len(word) > 4:
        return word[:-2]
    return word


TOKEN_RE = re.compile(r"[a-zA-Z]+")


def stemmed_tokenizer(text):
    return [stem(w) for w in TOKEN_RE.findall(text.lower()) if len(w) > 1]


def walk_schema_text(schema, depth=0):
    """Recursively collect every description string in a (possibly nested) JSON schema."""
    if depth > 6 or not isinstance(schema, dict):
        return []
    out = []
    if isinstance(schema.get("description"), str):
        out.append(schema["description"])
    props = schema.get("properties")
    if isinstance(props, dict):
        for pname, pschema in props.items():
            out.append(split_identifier(pname))
            out.extend(walk_schema_text(pschema, depth + 1))
    items = schema.get("items")
    if isinstance(items, dict):
        out.extend(walk_schema_text(items, depth + 1))
    return out


def tool_text(tool):
    parts = [split_identifier(tool["name"]), tool.get("description", "")]
    parts.extend(walk_schema_text(tool.get("parameters", {})))
    return " ".join(parts)


def request_text(messages):
    return " ".join(m["content"] for m in messages)


def required_params(tool):
    params = tool.get("parameters", {})
    return [(name, params.get("properties", {}).get(name, {})) for name in params.get("required", [])]


def param_satisfied(pname, pschema, text):
    words = set(split_identifier(pname).split())
    desc = (pschema.get("description") or "").lower()
    ptype = pschema.get("type", "")

    if words & DATE_WORDS or "date" in desc or "time" in desc:
        if DATE_RE.search(text):
            return True
    if words & LOCATION_WORDS or any(w in desc for w in ("city", "location", "address", "country")):
        if CITY_STATE_RE.search(text) or MULTIWORD_PROPER_RE.search(text):
            return True
    if words & IDENTIFIER_WORDS or "email" in desc:
        if EMAIL_RE.search(text) or QUOTED_RE.search(text):
            return True
    if words & NUMERIC_WORDS or ptype in ("integer", "number", "float"):
        if DIGIT_RE.search(text):
            return True
    # Generic fallback: does the parameter's own vocabulary show up in the request?
    text_words = set(re.findall(r"[a-z]+", text.lower())) - STOPWORDS_EXTRA
    if words - STOPWORDS_EXTRA <= text_words:
        return True
    return False


def missing_required(tool, text):
    return [name for name, schema in required_params(tool) if not param_satisfied(name, schema, text)]


def load_irrelevance(split="full"):
    path = {"full": "data/benchmark_full.jsonl", "pilot": "data/pilot_100.jsonl"}[split]
    rows = [json.loads(l) for l in open(path)]
    return [r for r in rows if r["category"] == "irrelevance"]


def compute_similarities(examples):
    """Fit one TF-IDF space over the whole corpus; return max cosine sim + best tool per example."""
    req_texts = [request_text(e["messages"]) for e in examples]
    tool_texts, tool_owner = [], []
    for i, e in enumerate(examples):
        for t in e["tools"]:
            tool_texts.append(tool_text(t))
            tool_owner.append((i, t["name"]))

    vec = TfidfVectorizer(ngram_range=(1, 2), stop_words="english", min_df=1,
                          tokenizer=stemmed_tokenizer, token_pattern=None)
    all_texts = req_texts + tool_texts
    matrix = vec.fit_transform(all_texts)
    req_vecs = matrix[: len(req_texts)]
    tool_vecs = matrix[len(req_texts):]

    results = []
    tools_by_example = {i: [] for i in range(len(examples))}
    for idx, (i, name) in enumerate(tool_owner):
        tools_by_example[i].append((name, tool_vecs[idx]))

    for i, e in enumerate(examples):
        best_name, best_sim = None, -1.0
        for name, tvec in tools_by_example[i]:
            sim = float(cosine_similarity(req_vecs[i], tvec)[0, 0])
            if sim > best_sim:
                best_name, best_sim = name, sim
        results.append((best_name, best_sim))
    return results


def tag(examples):
    sims = compute_similarities(examples)
    rows = []
    for e, (best_name, best_sim) in zip(examples, sims):
        if best_sim < TAU:
            rows.append(dict(id=e["id"], sub_label="no_relevant_tool",
                              max_similarity=round(best_sim, 4), candidate_tool=None,
                              missing_required_params=[], looks_fully_specified=False))
            continue
        tool = next(t for t in e["tools"] if t["name"] == best_name)
        text = request_text(e["messages"])
        missing = missing_required(tool, text)
        rows.append(dict(id=e["id"], sub_label="relevant_but_uncallable",
                          max_similarity=round(best_sim, 4), candidate_tool=best_name,
                          missing_required_params=missing,
                          looks_fully_specified=len(missing) == 0))
    return rows


def calibrate(examples):
    sims = compute_similarities(examples)
    values = sorted(sim for _, sim in sims)
    n = len(values)
    print(f"n={n}  min={values[0]:.3f}  p10={values[n // 10]:.3f}  "
          f"p25={values[n // 4]:.3f}  median={values[n // 2]:.3f}  "
          f"p75={values[3 * n // 4]:.3f}  max={values[-1]:.3f}")
    for cut in (0.02, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30):
        below = sum(1 for v in values if v < cut)
        print(f"  TAU={cut:.2f}: {below} no_relevant_tool / {n - below} relevant_but_uncallable")
    print("\nBorderline examples near candidate cutoffs:")
    for cut in (0.05, 0.10, 0.15, 0.20):
        near = sorted(zip(examples, sims), key=lambda p: abs(p[1][1] - cut))[:3]
        print(f"\n--- near TAU={cut} ---")
        for e, (name, sim) in near:
            print(f"  sim={sim:.3f} tool={name!r} req={request_text(e['messages'])[:100]!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="full", choices=["full", "pilot"])
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--out", default="data/irrelevance_subtags.jsonl")
    args = ap.parse_args()

    examples = load_irrelevance(args.split)
    if args.calibrate:
        calibrate(examples)
        return

    rows = tag(examples)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_no_tool = sum(r["sub_label"] == "no_relevant_tool" for r in rows)
    n_uncallable = len(rows) - n_no_tool
    n_flagged = sum(r["looks_fully_specified"] for r in rows)
    print(f"tagged {len(rows)} irrelevance examples -> {args.out}")
    print(f"  no_relevant_tool:        {n_no_tool}")
    print(f"  relevant_but_uncallable: {n_uncallable}  ({n_flagged} look fully specified -- review)")


if __name__ == "__main__":
    main()
