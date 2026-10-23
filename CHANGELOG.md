# Release notes

## 0.1.0a2 — consistent h/g namespaces

- Use the current `hg_*` tools, `hg://` resource URIs and h/g export markers
  throughout the package, documentation and examples.
- Remove alternate resource, tool and serialized-format aliases. Libraries or
  saved exports from a differently named installation require an explicit
  migration before opening them with this version.
- Preserve current collection revisions, record IDs, exact quotations, original
  file hashes and source visibility checks.

Install this alpha into its own environment. Existing personal libraries and
configuration are never migrated automatically. The earlier synthetic retrieval
evaluation remains labeled with the version it measured.

## 0.1.0a1 — first public alpha

hunter/gatherer is a local evidence library with a quote-first browser interface,
terminal commands and six MCP tool families. This alpha is distributed through
GitHub under Apache 2.0.

### Included

- Create a separate library with `hunt init`, import selected files with
  `hunt import`, and open the browser interface with `hunt serve`.
- Ask from the graph search bar or `hunt ask`; open original sources, inspect
  quotation context, copy citations and organize collections.
- Use `hunt ask --server` to reuse a running library and its loaded model;
  add `--json` for structured output.
- Connect an assistant through `hg_find`, `hg_open`, `hg_memory`,
  `hg_collection`, `hg_import_documents` and `hg_status`.
- Start with local keyword search and lightweight hashing. Neural embeddings,
  Qdrant, PDF rendering, OCR and audio components are optional dependencies.
- Back up a library and restore it into a new directory, retaining citations
  and archived original files.
- Configure themes, density, reduced motion and private-record access.
- Match common regular plurals and accented spellings, and focus count questions
  on quantities tied to the requested thing. Managed libraries present results
  in the same question-focused order used to select their quotations.

### Naming and compatibility

The distribution is `hunter-gatherer`, the Python package is `hunter_gatherer`,
configuration uses `HG_*`, and new resource URIs use `hg://`. Configured stores
default to `~/.hunter-gatherer`. Managed `--library` workflows use their own
manifest and ignore ambient configuration and agent skills.

Collections, evidence exports and resource URIs use h/g identifiers. Exact source
citations retain their record IDs. Clients connecting to h/g must use its current
tool names and configuration. No existing personal installation or database is
migrated automatically.

### Known limitations

- Ask retrieves quotations rather than generating an answer. Search ranking and
  excerpt emphasis are heuristic; a matching source may not answer the question.
- Historical-version questions require explicit source/version navigation.
  Read the [small synthetic retrieval evaluation](docs/retrieval-evaluation-v0.1.0a1.md)
  for measured results and limitations.
- The supported library service is local and single-user. Optional agent,
  routing, voice and OCR components are advanced features, not required for Ask.
- GitHub is the only release channel for this alpha. PyPI, Homebrew, a dependency
  download manager and automatic personal-library migration are deferred.

See [installation](docs/getting-started.md), [terminal usage](docs/cli.md),
[MCP setup](docs/mcp.md) and [release verification](docs/public-release-guide.md).
Bug reports and forks are welcome; pull requests are by invitation.
