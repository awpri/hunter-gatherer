# Questions and feature scope

## Is Ask a chatbot?

No. Ask retrieves quotations and saved records from your library. It does not
generate a synthesized answer. Read the sources yourself, or connect an assistant
through [MCP](mcp.md) to reason over them.

## What does a highlighted sentence mean?

It is an exact excerpt selected by a relevance heuristic to help you scan. The
selection does not verify a statement or prove that it answers your question.
Expand the passage and inspect qualifications, dates and surrounding context.

Some rules recognize question types. For example, an authenticity question such
as “Is this real?” can prioritize words such as “fictional” and “invented.” That
is a deliberate matching rule, not general reasoning. Keyword and semantic
retrieval can both return the right document with an unhelpful passage; check the
source rather than treating its rank or highlighting as a verdict.

Managed libraries match common regular plurals and accented spellings, so
“items” can find “item” and “cafe” can find “café.” Count questions can prioritize
an explicit quantity beside the requested thing, such as “one small household
item.” These are local matching rules, not a language model or a complete
understanding of grammar. Unrelated prices, dates and times are not count answers.

## What does Copy include?

Loaded source passages and saved memories, with citations. Copying a selection
includes only the selected sources. The export retains the full returned passage,
not just its shortened on-screen excerpt. No collection is required.

File titles, relationship hints and generated annotations are not quotations.
Access is checked again before copying, and withheld/unavailable text or output
omissions are reported.

## Does an empty result mean the information is missing?

Not necessarily. Search and output are bounded, extraction can omit content, and
privacy settings can withhold records. Load more when available, change the
question, or inspect the original. A search result cannot establish that a claim
is absent from the entire library.

## Why show related files or connected memories?

A title, saved relationship or authored annotation can point to a useful source
when no quotation was returned. Treat these as directions for further inspection.
A recorded connection is not independent corroboration.

## What is a writing example? Is that the voice feature?

A writing example is a passage an assistant can use as a reference for wording,
tone or style. The collection schema calls these `voice_examples`. They remain
separate from factual evidence, microphone input and spoken output.

## What happened to voice and model routing?

The inherited agent runtime, audio components and model-routing code remain
available for advanced use. They are not connected to the default quote-first
web workflow. Speech input (`voice`) and output (`tts` or `tts-openai`) require
optional packages and additional setup.

An MCP assistant uses its own model configuration. h/g's router applies when h/g
runs the agent itself. Existing streaming, scheduling or audio modules do not
imply a finished browser feature. `hunt advanced-serve` retains the experimental
agent server separately from ordinary library startup. See the [roadmap](roadmap.md).

## Do I need to download every dependency or model?

No. The base install runs personal keyword libraries, the fictional demo, web UI,
terminal search and MCP without neural embeddings, speech packages, OCR or cloud
SDKs. `hunt setup` shows installed capabilities and install hints; it downloads
nothing. pip or uv manages optional packages in your environment.

For a new neural library, install the `semantic` extra, then use `hunt init DIR
--semantic`. Initialization uses cached weights unless you explicitly add
`--download-model`. An existing semantic library needs its original model;
missing packages or weights must not silently replace it with hashing.

## How do SQLite, embeddings and Qdrant fit together?

| Part | Job in h/g |
| --- | --- |
| SQLite | Stores records, imported passages, source metadata and keyword indexes. |
| Embedding model | Converts text into vectors so search can compare meaning as well as words. |
| Qdrant | Optional external index for memory vectors. It does not replace the source records. |

The default personal library uses keyword retrieval and lightweight deterministic
hashing. Hashing is not a neural semantic model. Optional semantic search can run
without Qdrant, using the local vector backend. Document passages stay on the
SQLite search path even when the memory-vector backend uses Qdrant.

The Lantern demo's successful “Is the pilot real?” example uses keyword retrieval
and excerpt selection. It does not demonstrate neural understanding or Qdrant.

## Does every terminal question need to load the model again?

A standalone local Ask starts its own process. For repeated questions, use
`hunt ask "question" --server http://127.0.0.1:8767` against a running library.
The server reuses its configured model; the CLI does not load another copy.
Keyword and demo libraries have no neural model to load.

## Can an agent use the command line instead of MCP?

Yes. `hunt ask "question" --library DIR --json` returns structured evidence;
`hunt open CITATION --library DIR --json` reads an exact source. `--json` changes
presentation only. Use `--server URL` to reuse an existing process. See the
[terminal guide](cli.md) for limits and exit codes.

## Is everything local?

Personal libraries created by `hunt init` use local SQLite and an in-process
vector backend. Semantic libraries use a local embedding model; downloading it
requires the explicit initialization flag. Library startup ignores ambient cloud
credentials. Demo startup is also isolated. Installing packages uses the network.

An assistant connected through MCP may send returned sources to its provider.
Advanced runtime settings can configure cloud services or external databases;
those are separate from the default directory-based workflow.

## What do privacy controls do?

Sensitive records require an explicit visibility choice when read. Exports
recheck access. These controls cannot retract information already copied elsewhere.
Forgetting a memory is recoverable removal, not erasure of every historical
record or backup. Keep and manage backups separately.

## Which names and settings does h/g use?

The public namespace is now consistent:

| Interface | Current name |
| --- | --- |
| Distribution / Python import | `hunter-gatherer` / `hunter_gatherer` |
| Commands | `hunt`, `hunt-mcp`, `hunt-ui` |
| MCP tools / resources | `hg_*` / `hg://` |
| General settings | `HG_` variables and `~/.hunter-gatherer/config.env` |

`HG_CONFIG_FILE` can select another configuration file. A working-directory
`.env`, another application's settings and cookies, and an existing personal
library are not picked up automatically. MCP client instructions must use the
current tool names. Use an explicit, verified copy when moving an existing
library; see [migration boundaries](roadmap.md#compatibility-and-migration).

## Is this a stable release?

**0.1.0a2 is a public alpha**, distributed through GitHub release assets and a
versioned source tag. There is no PyPI or Homebrew release yet. Check the exact
release's notes and [packaged-install CI](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml).
Tests cover a defined source boundary and synthetic workflows; they do not
establish universal retrieval accuracy or validate every inherited runtime feature.
