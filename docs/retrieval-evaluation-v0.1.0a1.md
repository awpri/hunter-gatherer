# Retrieval evaluation for v0.1.0a1

This release includes a small, reproducible diagnostic evaluation of document
discovery and quote presentation. It does **not** establish general search
quality, semantic understanding, or the ability to generate correct answers.

## Method

The [fixed fixture](../tests/fixtures/release_retrieval/cases.json) contains ten
synthetic document versions across nine source families and twelve questions.
All organizations, people and events are invented. Cases cover literal wording,
paraphrases, negation, targets versus measured outcomes, current and historical
versions, and missing facts. Eleven questions identify an expected source and
an exact sentence; one asks for an absent fact.

The cases and expected sentences were written before the first measurement.
They were then kept unchanged while a general excerpt-presentation fix was
developed. This is an author-created diagnostic set, not an externally held-out
or representative benchmark. Three additional presentation regression cases
were written before implementing that fix.

Fixture SHA-256:

```text
b4626cef4559602fcf9625d9fcfe25df001633291d0c5c71bcb63cee80c27a9f
```

The [evaluation script](../scripts/evaluate_release_retrieval.py) creates fresh,
isolated SQLite libraries. It compares the explicit `hashing:384` configuration
with cached `BAAI/bge-small-en-v1.5` neural embeddings. Neither run uses Qdrant,
a writing model, reranking, personal settings, or an existing user library.
Document search uses the exhaustive archive policy. Each question receives one
Find page with a 4,096-token output budget.

Document rank is the order of distinct documents in the delivered passages,
not a separately measured rank over every search candidate. The script scores
the expected sentence in the emphasized span and in the visible excerpt
separately. It also checks that every delivered quotation is exact original
fixture text, matches its stored chunk, and reopens through its citation to the
same text. Display excerpts must remain contiguous source text.

## Results

Both configurations produced the same retrieval and presentation counts:

| Measurement | Hashing, before | Hashing, alpha | BGE, before | BGE, alpha |
|---|---:|---:|---:|---:|
| Expected source delivered first | 10/11 | 10/11 | 10/11 | 10/11 |
| Expected sentence in visible excerpt | 7/11 | 10/11 | 7/11 | 10/11 |
| Expected sentence selectively emphasized | 2/11 | 2/11 | 2/11 | 2/11 |
| Retrieved expected sources with any selective emphasis | 10 | 2 | 10 | 2 |
| Exact-source, citation or display-fidelity errors | 0 | 0 | 0 | 0 |

The initial selector sometimes highlighted a target while omitting a later
sentence saying that the outcome had not been measured. It also sometimes
highlighted a plain-text title instead of useful prose.

The alpha selector retains the matching paragraph, recognizes plain initial
title lines, and suppresses selective emphasis for weak matches or surrounding
qualifications. Long paragraphs use a neighboring-sentence preview without
selective emphasis; full text remains available through **Read in context** or
the source citation. The 800-character paragraph threshold is a soft bound:
complete sentences are preserved.

This improves the visibility of source context. It does not make the selector
understand the answer. Emphasis remains a conservative display heuristic, and
source context still needs to be read. The two configurations tying on this
small set does not establish that neural embeddings provide no benefit on
other libraries or questions.

## Remaining limitations

The historical-version question was the sole source-retrieval miss. Default
discovery returned the current Cedar notice, not the requested superseded
February notice. A request for history must not be treated as answered merely
because a current document was returned. Historical sources require explicit
source/version navigation in this alpha.

All four questions marked as lacking a supported requested fact retained
`answerability: not_determined`. That is a metadata contract, not evidence that
the system reasoned correctly or learned to abstain. The missing-registration
question can still return topically related passages that do not contain the
requested fact. h/g does not generate a yes/no verdict from these results.

These measurements concern the first delivered page and the shared browser/
terminal excerpt policy. They do not evaluate generated answers, broad recall,
large libraries, Qdrant, non-English sources, OCR, or a range of machines.

## Timing observations

In the alpha run, median search time across the twelve distinct questions was
**1.82 ms with hashing** and **11.62 ms with BGE**. The model was already loaded
by ingestion. These figures measure the in-process Find call only; they exclude
process startup, installation, downloading weights, HTTP/MCP transport and
terminal rendering. They are single-run observations on a tiny corpus, not a
latency guarantee or a measured Qdrant overhead.

The run used an ARM64 Mac, Python 3.14.8, sentence-transformers 6.1.0, PyTorch
2.14.1, Transformers 5.20.2 and NumPy 2.5.4. The cached BGE model revision was
`5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`. Runtime versions, hardware, model
cache and corpus size can change the timings.

## Reproduce

From a source checkout with h/g installed in a dedicated virtual environment:

```bash
python scripts/evaluate_release_retrieval.py --mode hashing --output ../hg-hashing-report.json
```

For the semantic comparison, install the `semantic` extra in that environment
and have the BGE weights cached locally first:

```bash
python -m pip install -e '.[semantic]'
python scripts/evaluate_release_retrieval.py --mode both --output ../hg-retrieval-report.json
```

The evaluator forces offline model loading. A missing cached model is an error;
it will neither download weights nor silently replace BGE with hashing. The
command records the installed runtime versions, fixture hash, per-question
results and exact excerpts. Use a new output filename for each run. Scratch
libraries are retained beside the report for inspection and are separate from
the normal h/g library.

There is no retrieval-score threshold that silently turns these observations
into a release-quality claim. An exact-source or citation-fidelity failure
does make the evaluator exit unsuccessfully.
