# Questions and feature scope

## Is Ask a chatbot?

No. Ask retrieves quotations and saved records from your library. It does not
generate a synthesized answer. Use the exact sources yourself, or connect an
assistant through [MCP](mcp.md) to reason over them.

## What does a highlighted sentence mean?

It is selected to help you scan for text related to your question. The emphasis
does not verify the statement. Expand the passage and inspect its source for
qualifications, dates and context.

## What does Copy include?

Loaded source passages and saved memories, with their citations. Copying a
selection includes only the selected sources from those results. The exported
text retains the full returned passage, not just the shortened on-screen excerpt.

File titles, relationship hints and generated annotations are not included as
quotations. Access is checked again before copying; the interface reports text
that is withheld, unavailable or omitted by the output limit. No collection is
required.

## Does an empty result mean the information is missing?

Not necessarily. Retrieval and delivery are bounded, and extraction may not
cover all the content in a file. Load more when available, change the question,
or inspect the original. A result cannot establish that a claim is absent from
the entire library.

## Why show related files or connected memories?

A title, saved relationship or authored annotation can point to a useful source
when no quotation was returned. Treat these as directions for further inspection.
A recorded connection is not independent corroboration.

## What is a writing example? Is that the voice feature?

A writing example is a passage an assistant can use as a reference for wording,
tone or style. Earlier interfaces called these “voice examples”; the underlying
collection schema still uses `voice_examples`. They are separate from factual
evidence and separate from microphone input or spoken output.

## What happened to voice and model routing?

The repository retains the inherited Python agent runtime, audio components and
model-routing code. They are not connected to the current quote-first web
workflow. Optional extras include speech input (`voice`) and speech output
(`tts`); they require additional setup and are outside the packaged fictional
demo's validation scope.

An external assistant connected through MCP uses its own model configuration.
h/g's router applies when h/g runs the agent itself. The presence of routing,
streaming or hands-free components in the source is not a claim that a finished
web feature is available.

See the [roadmap](roadmap.md) for the proposed integration order and which
components should remain optional.

## Do I need to download every dependency or model?

No. The base install runs the fictional library, web UI, terminal search and MCP
without neural embedding models, speech packages, OCR or cloud SDKs. `hunt setup`
shows optional capabilities and install hints without downloading anything. Use
pip or uv in the h/g environment to add only the extras you need; see
[setup](getting-started.md#optional-features).

An existing semantic library still needs its configured embedding model. Missing
packages must not silently change that model or replace it with demo hashing.

## Can an agent use the command line instead of MCP?

Yes. `hunt ask "question" --json` returns structured evidence, and `hunt open`
reads a citation. Both use the same source-reading and access rules as the
library. Pass `--demo-library DIR` for the isolated demo. The [terminal guide](cli.md)
covers output limits and exit codes; MCP remains useful for an ongoing session.

## Is everything local?

The dedicated fictional demo uses local SQLite and deterministic hashing
embeddings. Its create/serve commands ignore ambient configuration, make no model
provider calls and download no model weights. Installing dependencies uses the
network. A connected assistant may send the sources it reads to its own provider.

Other runtime configurations can use cloud model services or external stores.
The isolated demo does not configure or validate those integrations.

## What do the privacy controls do?

Sensitive records require an explicit visibility choice when read. Exports
recheck current source access. These controls help govern what the library
returns; they cannot retract information already copied elsewhere.

Forgetting a memory is recoverable removal, not erasure of every historical
record or backup. Manage retained backups separately. See
[release verification](public-release-guide.md) for backup and restore commands.

## Why are some names still ProHarness?

The product, interface and `hunt` command are hunter/gatherer, while the internal
Python package, older commands and MCP names retain `proharness` for compatibility. Use separate virtual
environments for h/g and ProHarness. The original live ProHarness library is not
part of this repository or the fictional demo.

## Is there a published package or a stable release?

Source installation is the documented path. There is no h/g PyPI or Homebrew release yet.
This is an early project; inspect the
[packaged-install CI results](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml)
for your platform. The demo tests cover a defined source and packaging boundary,
not every inherited runtime component.
