# Try a fictional evidence library locally

Create a dedicated demo with an explicit target directory:

```sh
hunt demo create /absolute/path/lantern-demo
hunt ask "Is the Lantern pilot real?" --demo-library /absolute/path/lantern-demo
hunt demo serve /absolute/path/lantern-demo --port 8767
```

The target must be absent or empty. Creation refuses an occupied directory,
including an existing demo. Pick another empty directory to repeat the exercise.
The demo uses local SQLite and deterministic HashingEmbedder(384). Creation and
its dedicated restart command ignore ambient environment settings and `.env`.
They do not download embedding models or call model providers. Hashing embeddings
are a workflow demonstration, not a semantic retrieval quality benchmark.

Open the local library at `http://127.0.0.1:8767`. Connect an MCP client using the
streamable HTTP endpoint `http://127.0.0.1:8767/mcp`. Keep using `demo serve` for
restarts; ordinary `serve` uses your normal configuration. A connected assistant
may use its own model service to answer questions; the demo server itself does not.

Every source, person and organization is fictional. `manifest.json` lists the
source document IDs, hashes, preserved-original paths, correction memory IDs and
three practice questions. `sources/` holds readable copies of all input revisions.
Originals are also preserved through the normal document archive, so both versions
remain referenceable after importing the later version.

Try these questions and inspect the cited originals:

- What is the Lantern pilot's weekly review schedule? The plan supports Tuesday at 10:00 UTC.
- Who owns the pilot now, and who owned it before? Rowan Finch replaced Mira Vale on 2030-04-08. Check the current plan and its earlier version.
- What measured percentage did completion time improve by? The sources record a 20 percent target, but no outcome measurement. A faithful answer says it is unknown.

The normal memory correction mechanism marks the original owner memory superseded,
while keeping its audit history. The normal document importer links plan version 2
to version 1. No synthetic evidence is imported into your existing store. The demo
directory contains its database and archive; deleting this dedicated directory
removes the demo when you are finished.

## Installation and verification

Use Python 3.11 or later, a dedicated virtual environment, and `python -m pip
install .` from a source checkout. For a built wheel, install that wheel instead.
The bundled web interface and three demo Markdown sources ship inside the package;
Node is not needed to run them. This release requires MCP SDK 2.x, matching the
runtime APIs it imports. The base install does not require neural embeddings,
Torch, Qdrant or cloud SDKs. See [optional features](getting-started.md#optional-features)
for capabilities that require additional packages and setup.

Run the packaged smoke script against the exact installed artifact to verify
Find → Open → saved collection → restart, backup/restore and UI assets. See
[the candidate scope and verification guide](public-release-guide.md) for commands
and platform limits.
Demo startup and offline workflow tests reject outbound socket connections and
prove ambient store settings are ignored.

Original PDF images require the optional `document-view` extra, and OCR/layout
recovery requires separate optional setup. Neither is needed for this text demo.
