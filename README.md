<h1><img src="docs/assets/wordmark.svg" alt="hunter/gatherer" width="380"></h1>

**Your data, AI-native.** Ask your library. Inspect the evidence. Give your assistant the sources.

[![Packaged demo checks](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml/badge.svg?branch=main)](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![MCP: six tool families](https://img.shields.io/badge/MCP-6_tool_families-9b8ec4.svg)](docs/mcp.md)

[**Get started**](docs/getting-started.md) · [**Documentation**](docs/index.md) · [**Connect an assistant**](docs/mcp.md) · [**Contribute**](CONTRIBUTING.md)

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
| Keep control of your library | Local storage, import review, privacy controls, backup and restore, themes and motion preferences. |

The default experience retrieves evidence; it does not generate an answer. Use
your existing assistant to reason over inspected sources. The broader inherited
agent runtime, model router and audio components are separate from this workflow.
See [feature scope and common questions](docs/faq.md).

## Try it locally

You need **Python 3.11 or newer** and Git. The bundled interface runs without Node.
Use a dedicated environment; h/g currently retains the `proharness` package and
command names for compatibility.

```bash
git clone https://github.com/awpri/hunter-gatherer.git
cd hunter-gatherer
python3 -m venv .venv
.venv/bin/python -m pip install '.[document-view]'
.venv/bin/proharness demo create ./demo-library
.venv/bin/proharness demo serve ./demo-library --port 8767
```

Open [localhost:8767](http://127.0.0.1:8767/) and ask:
**“What is the Lantern pilot's weekly review schedule?”**

The demo contains fictional sources and uses its own database. Its launcher
ignores normal configuration and does not download model weights or call a model
provider. Dependency installation requires internet access and can be substantial.
The demo folder must be new or empty.

[Windows instructions and setup details →](docs/getting-started.md)

## Documentation

- [Using the library](docs/usage.md) — Ask, graph exploration, citations and collections.
- [MCP integration](docs/mcp.md) — connect an assistant and follow sources through the six-tool interface.
- [Questions and feature scope](docs/faq.md) — search limits, privacy, writing examples, audio and model routing.
- [Roadmap](docs/roadmap.md) — core workflow and proposed optional assistant features.
- [Fictional demo](docs/demo.md) — practice questions, document history and recovery.
- [PDF image regions](docs/visual-regions.md) — exact page and region inspection.
- [Release verification](docs/public-release-guide.md) — packaging, checks and the source export boundary.

This is an early project. The [live CI matrix](https://github.com/awpri/hunter-gatherer/actions/workflows/packaged-demo.yml)
exercises packaged installs on Linux, macOS and Windows with Python 3.11–3.14;
check the result for your platform. Source installation is the documented path;
there is no h/g package release on PyPI yet.

## Contributing

Bug reports, documentation improvements and focused pull requests are welcome.
Start with [CONTRIBUTING.md](CONTRIBUTING.md), or [open an issue](https://github.com/awpri/hunter-gatherer/issues/new/choose).

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution and bundled
third-party notices. h/g is developed separately from ProHarness; its live
libraries and configuration are not part of this repository.
