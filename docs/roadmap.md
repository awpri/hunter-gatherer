# Direction and release scope

**0.1.0a1 is a public alpha.** The library, terminal and MCP workflows are the
supported focus. Proposals below are not release commitments.

h/g centers on a question, its sources and a useful handoff to an AI assistant.
The graph and search bar remain the main workspace. Optional capabilities should
help that flow without requiring an agent control panel.

## Keep, integrate, or defer

| Capability | Current scope | Direction |
| --- | --- | --- |
| Evidence search, graph, originals, history and collections | Available in the library UI/API. | **Core.** Improve retrieval, source navigation and accessible interaction. |
| MCP | Six families: Find, Open, Memory, Collection, Import and Status. | **Core.** Let existing assistants reason over exact sources. |
| Terminal | Init/import/serve, Ask/Open, JSON, setup and backup workflows. | **Core.** Keep source handling consistent and library selection explicit. |
| Writing examples | Stored separately from factual evidence in collections. | **Keep within collections.** They reference tone/style, not audio or claim support. |
| Neural search | Optional local embeddings; keyword libraries remain lean. | **Measure.** Compare missed matches, passage selection, latency and installation cost. |
| Speech input | Python audio components exist; no finished library microphone flow. | **Optional later.** Dictate into the existing editable search input. |
| Read aloud | Local/cloud TTS components exist. | **Optional later.** Explicit play/stop on selected text, with accessible controls. |
| Generated cited summaries | Advanced agent runtime can generate; Ask itself returns evidence. | **Later.** Explicit synthesis of selected sources with linked quotations. |
| Model choice and spending | Advanced runtime has routing, provider fallback and budget components. | **Optional AI settings.** Start with one chosen model and visible usage/cancellation. |
| Adaptive routing, tuning and answer reuse | Experimental runtime components exist. | **Defer.** Require measured benefit and source-freshness validation. |
| Wake words, interruption and speculative voice | Components exist outside the library workflow. | **Defer.** Device and lifecycle complexity has limited value for initial evidence search. |
| Pipelines and multi-agent orchestration | Advanced APIs/interpreter/scheduler remain available. | **Developer extension.** Outside the default library interface. |

Optional means no mandatory downloads, navigation or background work for people
who do not use a capability. Existing advanced features remain accessible;
availability does not imply that the alpha validates their whole workflow.

## Near-term priorities

1. Broaden retrieval evaluation with unfamiliar documents, paraphrases, negation,
   contradictory versions and missing answers. Check both source retrieval and
   the selected excerpt; the Lantern example alone proves little about general quality.
2. Improve import feedback and large-library responsiveness without losing exact
   references, history, privacy or useful diagnostics.
3. Test installed upgrades and recovery against real usage. Keep release artifacts,
   dependency records and cross-platform checks reproducible.
4. Consider a maintained Homebrew tap and package-index distribution after the
   GitHub alpha installation and upgrade path settle.
5. Add small optional features only when they improve the core flow: editable
   dictation before hands-free voice, and selected-source synthesis before
   advanced routing.

pip or uv already handles dependency installation and caching. `hunt setup`
reports optional capabilities; a second custom dependency manager is unnecessary.
Model downloads and external services should remain explicit setup choices.

## What MCP does and does not handle

An assistant using the library profile supplies its own model. h/g's router does
not select Claude's, Codex's or another client's model when it calls Find/Open.
The full profile includes `hg_delegate`, which deliberately runs h/g's agent
loop with separate configuration, tools and costs. `hunt advanced-serve` retains
the experimental agent server. Profiles control discovery, not authorization.

Advanced runtime integration still needs validation, including whether the
selected within-tier model reaches the concrete client, how actual cost feeds
adaptive selection, and whether governor adjustments affect execution. These
are reasons to avoid promising automatic model optimization today.

## Compatibility and migration

The public alpha uses `hunter-gatherer` for the distribution, `hunter_gatherer`
for Python imports, `hunt`/`hunt-mcp`/`hunt-ui` for commands, `hg_*` for MCP tools,
`hg://` for new resources and `HG_` for settings. General configuration lives in
`~/.hunter-gatherer/config.env`, or a file explicitly selected by `HG_CONFIG_FILE`.
Directory-based library commands use their own manifest instead.

Existing citation labels and source IDs stay stable. Readers recognize older
collection workspace markers, compact export formats and resource URI aliases.
Exact source text, hashes and histories are not renamed. Old MCP client prompts
and tool allowlists must be updated; old tool names are not the advertised API.

The original ProHarness installation remains separate. Installing h/g does not
read its configuration, open its library or change clients. Personal database
migration and active-client cutover are deliberately separate from this release:

1. Verify a backup of the database and preserved originals.
2. Restore to a new directory with the original embedding configuration. The
   guided `hunt init DIR --from-backup BUNDLE` path accepts supported keyword/BGE
   fingerprints; other models require explicit advanced configuration.
3. Compare source IDs, exact citations, versions, correction history, collections,
   privacy behavior and representative searches. Restore does not establish that
   a different model would retrieve equivalent results.
4. Verify a new backup/restore of the copied library.
5. Switch UI/MCP clients only after the owner approves that cutover. Retain the
   original and verified backup for rollback.

## Implementation references

- [Dependencies and entry points](../pyproject.toml)
- [Library lifecycle](../hunter_gatherer/library.py) and [web surface](../hunter_gatherer/server/library_app.py)
- [MCP profiles](../hunter_gatherer/mcp/profiles.py) and [handlers](../hunter_gatherer/mcp/tools.py)
- [CLI](../hunter_gatherer/cli.py), [agent loop](../hunter_gatherer/loop.py) and [voice components](../hunter_gatherer/voice/)
- [Router](../hunter_gatherer/models/router.py), [governor](../hunter_gatherer/governor.py) and [pipelines](../hunter_gatherer/pipelines/)
