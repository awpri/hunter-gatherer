# Proposed direction

**Status: product proposal, not a release commitment.** This describes a smaller
default experience and optional extensions. It does not claim the proposed UI,
dependency split or integrations already exist.

hunter/gatherer's focus is a question, its sources, and a usable handoff to an AI
assistant. The graph and search bar remain the main workspace. Extra capabilities
should help that workflow without making people operate an agent control panel.

## Keep, integrate, or defer

| Capability | Current implementation | Proposed place |
| --- | --- | --- |
| Evidence search, graph, original sources, history and collections | Available in the current library UI and library API. | **Core.** Improve retrieval, source navigation and accessible interaction before adding another workspace. |
| MCP connection | The library profile exposes Find, Open, Memory, Collection, Import and Status. | **Core.** Let an existing assistant reason over inspected sources and write back deliberate memories or collections. |
| Writing examples | Collections store style examples separately from evidence references; exports retain that distinction. | **Keep within collections.** Use “Writing example” and a short explanation of tone/style. This is not recorded audio or proof of a claim. |
| Speech input | The CLI has press-to-talk recording, transcription and an agent response. Speech components exist; there is no microphone flow in the current library UI. | **Optional extension.** A microphone beside the existing search input should transcribe into editable text, then use the ordinary Ask action. |
| Read aloud | Local and cloud TTS implementations and sentence-chunk helpers exist in the voice code. | **Optional extension.** A play/stop action on a quote or generated summary. Preserve keyboard and screen-reader access; do not start playback automatically. |
| Generated summaries with citations | The inherited agent runtime can produce answers. Current web Ask retrieves quotations and saved records; it does not generate a summary. | **Later, optional.** “Summarize these sources” within the result panel, with linked citations and the original quotes still available. Start with selected sources and one configured model. |
| Model choice, fallback and spending controls | The agent runtime has local/cheap/cloud tier routing, provider fallback, usage accounting and budget components. Search itself does not call that answer-generation runtime. | **Optional AI settings.** Start with a chosen model, an explicit local/cloud policy, a spend limit and a visible stop action. Show model and usage details when a generated operation runs. |
| Adaptive model selection, automatic tuning and semantic answer reuse | Bandit, classifier, tuning, governor and answer-cache code exists. Some adaptive paths need integration work and validation. | **Defer from the supported default experience.** Keep as experimental runtime capabilities until measurable benefits and source-freshness checks justify them. |
| Wake words, interruption and speculative voice prefetch | Detector, cancellation and prefetch components exist; the CLI entry point uses the simpler press-to-talk path. | **Defer.** They add device, latency and lifecycle complexity without improving the main evidence workflow yet. |
| Pipelines, multi-agent orchestration and autonomous workflows | An interpreter, scheduler, agent runtime and advanced MCP/legacy-server integrations exist. The current library web surface does not include the legacy workbench or scheduler. | **Optional developer/automation extension.** Preserve the APIs for deliberate use. A general-purpose workflow builder or always-running assistant is outside the initial library interface. |

“Optional” means a capability can remain available without adding permanent
navigation, running background work or requiring every installation to download
its dependencies. It does not require discarding existing code or saved data.

## One interaction flow

1. **Ask:** type or optionally dictate into the existing search bar. A dictated
   question remains editable before submission.
2. **Inspect:** read focused quotes, open originals, compare records and select
   useful sources. Graph state and the source reader remain part of this workspace.
3. **Use:** copy quotes with citations, save a collection, or let a connected MCP
   assistant work with the same sources.
4. **Optionally synthesize:** a future explicit action could summarize the selected
   sources in the same panel. Generated prose must be identified as such, cite
   source locations and preserve the distinction between a quote and an inference.

AI configuration belongs in Settings when that optional capability is enabled.
The normal search form should not require a provider, model tier, routing
threshold, token budget or prompt template. A result may expose details on demand
without making them prerequisites to reading a source.

Browser dictation needs its own capture and request flow: the existing Python
microphone session captures the machine running Python. It is not a ready-made
browser microphone integration. Implementation should make recording state,
cancel, transcript editing and the chosen local/cloud processing route clear.

## What MCP does and does not handle

With the six-tool **library** profile, the connected client uses its own model to
reason about h/g sources. h/g's internal model router does not choose Claude's,
Codex's or another client's model just because that client calls Find or Open.

The **full** profile also includes `proharness_delegate`, which explicitly runs
the internal agent loop. That is a different operation with its own tools, model
configuration and costs. Saved pipelines have additional integration in the
legacy agent server. They are not part of the six-tool library catalog.

Profiles choose the discovery surface; they are not authorization boundaries.
Any future optional AI/automation interface needs explicit execution and
configuration boundaries rather than relying on hidden menu entries.

## Reduce installation weight

The current base dependencies include `sentence-transformers`, `qdrant-client`,
`openai` and `anthropic`, even for the isolated demo. Voice is already separated
into the `voice` extra, and local speech synthesis into `tts`. Original PDF
rendering, OCR recovery and the desktop shell also have separate extras.

A useful packaging follow-up is to evaluate separate semantic-search, vector
backend and AI-provider extras, while retaining an easy supported installation
for everyday users. This requires checking imports, first-run setup, capability
errors and existing stores before changing requirements. Hashing embeddings make
the fictional demo deterministic; they are not a substitute for a production
semantic-search model.

The expensive complexity is mandatory downloads, configuration and maintenance
for unused capabilities. A small source module behind an optional, tested API is
not automatically a problem.

## Proposed order

1. **Reliable library release:** passing packaged-demo checks on supported
   platforms, clear setup and recovery instructions, source navigation and
   accessible graph/search behavior.
2. **Complete the rename and migrate deliberately:** move public and internal
   interfaces to h/g names, then validate a copy of an existing library before
   explicitly switching the active installation. Follow the sequence below.
3. **Installation boundaries:** measure the dependency footprint, separate
   optional capabilities where safe, and document supported versus experimental
   paths. Keep existing environments and stored libraries compatible.
4. **Small optional voice integration:** editable search dictation first;
   read-aloud second if useful. Validate actual microphone/audio devices and
   cancellation rather than assuming the Python components complete the UI.
5. **Optional cited synthesis:** one configured model over selected sources,
   explicit execution, working usage accounting and cancellation. Add fallback
   and budget policy after this narrow path is demonstrated end to end.
6. **Measured extensions:** advanced routing, caching and automation only when
   evaluations show a concrete gain in reliability, quality, latency or cost.

Known runtime integration work includes making a selected within-tier
`Route.model` determine the concrete client, recording actual cost for bandit
updates, and applying or removing the governor's currently unused alpha offset.
These are reasons to avoid promising automatic model optimization today, not
limitations of the quote-first search workflow.

## Finish the rename, then migrate a library

The intended destination is a consistent hunter/gatherer identity, including
internal names. The current compatibility names are transitional. This is a
future implementation sequence; documenting it does not migrate or switch a
live library.

1. **Define compatibility first.** Inventory package imports, commands, MCP tools,
   resource URIs, environment settings and stored references. Add compatibility
   tests and document deprecations before changing these interfaces.
2. **Introduce the h/g namespace.** Rename the package, CLI, MCP server and tools,
   resource URI scheme, environment variables and configuration defaults using
   consistent machine-safe names. Update documentation and client setup together.
   Keep explicit transitional aliases where existing clients or citations need
   them; remove old product references as those dependencies are retired.
3. **Migrate an isolated copy.** Verify a backup of the existing database and
   preserved source archive, then migrate a separate copy with its own settings
   and paths. Keep the original installation and library intact.
4. **Verify the whole library.** Check that citations resolve to the same sources,
   document versions and memory correction histories survive, and collections,
   privacy controls and embedding configuration retain their behavior. Compare
   representative retrieval results and test backup and restore of the migrated
   library. Rebuild an index only when the selected embedding configuration needs
   it; do not silently substitute a different model.
5. **Make an explicit cutover.** Switch the active UI and MCP client configuration
   only after the migrated copy passes verification and the owner approves that
   switch. Retain the original and verified backup for rollback. Retire legacy
   aliases according to the documented compatibility plan, without breaking
   saved citation resolution.

## Implementation references

- [Dependency groups](../pyproject.toml)
- [Library web surface](../proharness/server/library_app.py)
- [MCP profiles](../proharness/mcp/profiles.py) and [tool handlers](../proharness/mcp/tools.py)
- [CLI entry points](../proharness/cli.py) and [voice components](../proharness/voice/)
- [Agent loop](../proharness/loop.py), [router](../proharness/models/router.py) and [spend governor](../proharness/governor.py)
- [Pipelines](../proharness/pipelines/) and [legacy server routes](../proharness/server/routers/pipelines.py)
