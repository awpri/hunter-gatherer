# hunter/gatherer

**Ask your library. Inspect the evidence. Give your assistant the sources.**

hunter/gatherer (h/g) is a local personal evidence library for documents, files,
and memories. Ask a normal question and inspect exact quotations with citations.
Open the original source, copy cited context, or save an investigation as a
collection. A compact MCP interface gives your existing AI assistant the same
path from a question to its sources.

The interface keeps the full library available: graph and list exploration,
imports, document history, original PDF pages and regions, collections, review,
health information, themes and motion preferences. Quotes are source text;
retrieval matches and recorded connections are not verified answers.

## Try the isolated demo

Use Python 3.11 or newer and a **dedicated virtual environment** from this directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install '.[dev,document-view]'
.venv/bin/proharness demo create ./demo-library
.venv/bin/proharness demo serve ./demo-library --port 8767
```

Open http://127.0.0.1:8767/. On Windows, virtual-environment executables live in
`.venv/Scripts` instead of `.venv/bin`. See the [Windows setup instructions](getting-started.md#windows-powershell)
and [current platform checks](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml).
The demo target must be new or empty. It uses only fictional Lantern project
sources, a separate SQLite database and deterministic hashing embeddings. It
ignores normal configuration and downloads no model weights. Installing Python
packages may need network access. Hashing demonstrates the workflow; it is not
a semantic retrieval quality benchmark.

Try: **What is the Lantern pilot's weekly review schedule?**

Graph is the home workspace. Choose **Ask** in its search bar and enter one clear
question with its subject. Quotes open alongside the graph; closing them returns
to the same graph position. **Records** searches library entries, and **Commands**
finds actions. Additional questions are optional; they look for different details,
rather than imposing filters every source must match.

Open a source title to read the original. Expand a quote to see the surrounding
passage. Copy quotations with their citations for your assistant, or select
sources to save a collection with notes, review decisions and unresolved questions.

### About search results

- **Are these generated answers?** No. Quotes are exact excerpts from your files;
  saved memories are records in your library. A highlighted sentence matches your
  question's wording, but the highlight does not verify its claim. Read the full
  passage for context, qualifications and dates.
- **What gets copied?** The copy action uses the loaded source passages and saved
  memories, with their citations. When you choose to copy a selection, it uses only
  selected sources from those results. File titles, connection hints and generated
  annotations are not quotations and are not included as source text. Sources are
  checked again for current access; any withheld or omitted text is reported.
- **Do I need a collection?** No. Collections save references and your own review
  notes for later. A writing example is a source you want an assistant to use as
  a reference for tone or style; it is separate from evidence for a factual claim.
- **Why are there related files or records?** A title, saved connection or derived
  annotation can help you find a source even when no matching quotation was
  returned. Open it to inspect the actual content. A connection is not independent
  corroboration.
- **Does an empty result mean the information is absent?** No. Each search has a
  delivery limit. Load more results when offered, try another question, or inspect
  the library directly. Missing text can also reflect extraction or access limits.
  Result details retain the search diagnostics for investigating these cases.

This is developed separately from ProHarness. Internal Python module, command and
MCP names remain `proharness` for compatibility. Do not install both projects in
the same virtual environment. The explicit demo launcher above keeps data and
configuration isolated; ordinary `proharness serve` reads normal ProHarness
configuration and is not the separate demo launcher.

## Connect your assistant

Connect an MCP client to http://127.0.0.1:8767/mcp. The six tool families are
**Find, Open, Memory, Collection, Import and Status**. Find returns bounded source
candidates; Open inspects the exact identified source. Follow continuation when
more admitted matches are available. A miss is not proof of absence, and a link
is not independent corroboration. Your assistant should cite inspected sources
and keep unsupported details explicit.

The `document-view` extra enables original PDF page images and bounded region
inspection through Open. It renders locally without OCR or model downloads.
Without it, text retrieval works and page-image requests return a capability
error. See the [image region workflow](visual-regions.md) for authored descriptions,
exact bounds and supported Open routes. Optional OCR recovery, cloud clients and
voice require additional setup and are outside this demo's validation scope.

## Verify and recover

```bash
.venv/bin/python -m pytest tests
.venv/bin/python scripts/smoke_packaged_demo.py
.venv/bin/proharness backup create ./demo-backup --database ./demo-library/demo.db
.venv/bin/proharness backup verify ./demo-backup
.venv/bin/proharness backup restore ./demo-backup --database ./restored-demo.db
```

Backup and restore require explicit paths; restore refuses an occupied target.
The selected synthetic tests cover the six-tool workflow, continuation, API and
privacy behavior, exact sources, original PDF pages, isolated configuration,
persistence, backup/recovery and export boundaries. They are a bounded subset of
development tests. Private evaluation corpora and evaluation modules are excluded;
benchmark commands requiring those modules are outside this candidate's scope.

The inherited ProHarness packaged-demo matrix passed on macOS and Linux with
Python 3.11–3.14; its Windows cells failed. That is a prior baseline, not evidence
that this modified hunter/gatherer release passed the matrix. Check this repo's
CI results before relying on a platform. Base installation includes cloud SDKs
and sentence-transformers/Qdrant dependencies even for the hashing demo; no provider
key is required, but initial downloads can be substantial. There is no Python
lockfile; record resolved versions for reproducible release checks.

## Rebuild the interface

```bash
cd frontend
npm ci
npm run build
npm test
```

The exporter selects HTML-referenced assets and explicit font/license files.
Verify installed assets after rebuilding. Keep third-party notices with them.

## Prepare a clean source candidate

```bash
python scripts/export_public_candidate.py --destination ../fresh-public-candidate
python scripts/check_publication_hygiene.py ../fresh-public-candidate
```

The destination must be new or empty and outside the development repository.
The manifest records selected file hashes and sizes. The exported README is this
guide with local links adjusted. The exporter excludes Git history, configuration,
credentials, agent state, databases, private corpus files and unreviewed tests/docs.
Use a fresh Git repository from that export; do not publish development history.
The [provenance record](release-provenance-20261014.md) describes the review boundary.
Pattern scanning is a bounded check, not comprehensive rights or privacy clearance.

## License

Project source is licensed under **Apache License 2.0**, selected for hunter/gatherer
on 2026-10-20. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE). Bundled fonts and
icons retain their upstream terms. The original ProHarness project and live
personal library are separate and unchanged by this release.
