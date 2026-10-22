<h1><img src="docs/assets/wordmark.svg" alt="hunter/gatherer" width="380"></h1>

**Your data, AI-native.** Ask your library. Inspect the evidence. Give your assistant the sources.

[![Packaged demo checks](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml/badge.svg?branch=main)](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![MCP: six tool families](https://img.shields.io/badge/MCP-6_tool_families-9b8ec4.svg)](docs/mcp.md)

[**Get started**](docs/getting-started.md) · [**Terminal guide**](docs/cli.md) · [**Documentation**](docs/index.md) · [**Connect an assistant**](docs/mcp.md) · [**Contribute**](CONTRIBUTING.md)

hunter/gatherer (h/g) is a local evidence library for documents, files and memories.
Ask a question in the graph's search bar and read exact excerpts from your sources.
Open the original, copy quotations with citations, or keep an investigation in a
collection. Your AI assistant can use the same library through MCP.

## From a question to its sources

| What you need | What h/g gives you |
| --- | --- |
| Find a detail across your files | Question search with quotations, source links and expandable context. |
| Check where an answer came from | Preserved document versions, passage citations and original PDF pages and regions. |
| Follow an investigation | A graph and list view, saved connections, collections, notes and unresolved questions. |
| Give an assistant useful context | Six MCP tool families for finding, opening, remembering, collecting, importing and checking status. |
| Work from the terminal | `hunt ask` for cited quotations, `hunt open` for context, and JSON output for scripts and agents. |
| Keep control of your library | Local storage, import review, privacy controls, backup and restore, themes and motion preferences. |

The default experience retrieves evidence; it does not generate an answer. Use
your existing assistant to reason over inspected sources. The broader inherited
agent runtime, model router and audio components are separate from this workflow.
See [feature scope and common questions](docs/faq.md).

## Try it locally

You need **Python 3.11 or newer** and Git. The bundled interface runs without Node.
Use a dedicated environment. The `hunt` command is the h/g entry point; the
Python package and older `proharness` commands remain compatible.

```bash
git clone https://github.com/awpri/hunter-gatherer.git
cd hunter-gatherer
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/hunt demo create ./demo-library
.venv/bin/hunt ask "What is the Lantern pilot's weekly review schedule?" --demo-library ./demo-library
.venv/bin/hunt demo serve ./demo-library --port 8767
```

Open [localhost:8767](http://127.0.0.1:8767/) and ask:
**“What is the Lantern pilot's weekly review schedule?”**

The demo contains fictional sources and uses its own database. Its launcher
ignores normal configuration and does not download model weights or call a model
provider. The base install skips neural embedding models, Torch, Qdrant and cloud
SDKs. Add [optional features](docs/getting-started.md#optional-features) when you
need them. Package installation requires internet access; the demo folder must
be new or empty.

[Windows, uv and setup details →](docs/getting-started.md)

## From your terminal

With the environment activated:

```bash
hunt ask "Is the Lantern pilot real?" --demo-library ./demo-library
hunt ask "When is the weekly review?" --demo-library ./demo-library --json
hunt setup
```

Readable results keep quotations, source titles and citations together. `--json`
returns structured evidence for an agent or script; `hunt open` follows a citation
to its source. No answer-generation service is required. See the
[terminal guide](docs/cli.md) for library selection, output limits and examples.

## Install with your AI assistant

Copy this prompt into an assistant with terminal access:

```text
Install hunter/gatherer from https://github.com/awpri/hunter-gatherer using its
current README and docs/getting-started.md. Check my OS, Git and Python first.
Use a new checkout and a dedicated .venv; prefer uv if already installed,
otherwise use Python 3.11+ and pip inside that environment. Install the base
package only. Do not use sudo/global pip, modify shell startup files, replace an
existing installation, or read/migrate my ProHarness configuration or library.
If repository access fails, explain the access requirement; do not substitute
an unrelated package with the same name.

Run hunt setup, create a new fictional demo directory, and verify hunt ask
"What is the Lantern pilot's weekly review schedule?" in readable and JSON
modes using --demo-library. Show the actual result and installed commit.
Then start demo serve on an available loopback port and give me its URL,
the exact restart command and how to stop it. Ask before adding optional
features, downloading model weights or connecting personal data.
```

## Documentation

- [Using the library](docs/usage.md) — Ask, graph exploration, citations and collections.
- [Terminal usage](docs/cli.md) — questions, source inspection, JSON and script exit codes.
- [MCP integration](docs/mcp.md) — connect an assistant and follow sources through the six-tool interface.
- [Questions and feature scope](docs/faq.md) — search limits, privacy, writing examples, audio and model routing.
- [Roadmap](docs/roadmap.md) — core workflow and proposed optional assistant features.
- [Fictional demo](docs/demo.md) — practice questions, document history and recovery.
- [PDF image regions](docs/visual-regions.md) — exact page and region inspection.
- [Release verification](docs/public-release-guide.md) — packaging, checks and the source export boundary.

This is an early project. The [live CI matrix](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml)
exercises packaged installs on Linux, macOS and Windows with Python 3.11–3.14;
check the result for your platform. Source installation is the documented path;
there is no h/g package release on PyPI or Homebrew yet. While the repository is
private, cloning requires an authorized GitHub account.

## Contributing

Bug reports, suggestions and forks are welcome. Pull requests are by invitation
only; [open an issue](https://github.com/awpri/hunter-gatherer/issues/new/choose)
to discuss a change before submitting one. Reviews and support are provided as
time permits. See [CONTRIBUTING.md](CONTRIBUTING.md) for the policy and development guide.
See [SECURITY.md](SECURITY.md) for the security boundaries and reporting setup.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution and bundled
third-party notices. h/g is developed separately from ProHarness; its live
libraries and configuration are not part of this repository.
