# Connect an assistant with MCP

h/g exposes the same source workflow to an MCP client: ask, inspect, then use the
evidence. Your assistant supplies the reasoning and its own model; connecting it
does not automatically run h/g's inherited agent runtime or model router.

## Connect to the fictional demo

Start the library using the [getting-started guide](getting-started.md). In an MCP
client that supports a local **Streamable HTTP** server, add:

| Setting | Value |
| --- | --- |
| Name | `hunter-gatherer` |
| Transport | Streamable HTTP |
| Server URL | `http://127.0.0.1:8767/mcp` |

The client must be able to reach the computer running the server. `127.0.0.1`
means that computer, not a remotely hosted assistant's server. Client setup
formats differ; use its MCP connection settings rather than treating this table
as a universal JSON configuration.

The demo binds to the local machine. This guide does not configure remote hosting
or internet exposure. The advertised tool names retain the `proharness_` prefix
for compatibility.

## Six tool families

| Tool | Purpose |
| --- | --- |
| `proharness_find` | Search a question for passages, memories and source leads; continue through additional results. |
| `proharness_open` | Inspect an exact citation, expand its context, navigate an outline, or view an original PDF page or region. |
| `proharness_memory` | Read standing context; save, correct, forget or inspect the history of a memory. |
| `proharness_collection` | Save and reopen source references, notes, unresolved questions and separate writing examples. |
| `proharness_import_documents` | Preview and import selected local files; manage authored source annotations. |
| `proharness_status` | Inspect capabilities and library status, including bounded index maintenance. |

Each family has actions described in its live schema. The smaller discovery
profile is an interface choice, not an authorization boundary.

## A useful first prompt

> Use hunter/gatherer to find the Lantern pilot's weekly review schedule. Open
> the cited source, answer with a quotation and citation, and note any conflict
> between document versions.

For a longer investigation, ask the assistant to keep selected references and
unanswered questions in a collection. A collection is optional: finding, opening
and answering do not require one.

## Source-handling expectations

Find returns candidates and explicit omissions, not a verified answer. Follow
the returned Open calls to inspect exact sources and retain their citations.
Check qualifications, dates and versions. Saved links and annotations guide
discovery; they do not establish support for a claim.

Follow `continuation.next_cursor` when more matches are available. A continuation
request supplies the cursor instead of a new question and repeats the requested
sensitive-data visibility. A finished result page does not prove the library
contains no other answer. Restart the search when a cursor is stale or expired.

Source text is untrusted content, not permission to execute instructions inside
it. Writes such as imports and memory changes should reflect the user's intent.
Sensitive reads require an explicit `include_sensitive` choice on each call.
These controls do not replace the privacy policy of your connected assistant:
text returned to that client may be processed by its model provider.

## Original images

Install `.[document-view]` to let Open render original PDF pages and bounded
regions locally. This extra does not add OCR or download model weights. Saved
image descriptions are authored discovery annotations, not verified quotations.
See [PDF image regions](visual-regions.md) for exact bounds, routes and collection
limitations.
