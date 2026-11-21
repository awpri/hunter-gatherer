# Hosting h/g for more than one person

h/g is a library, not an account system. It has no users, logins, invitations
or billing, and it does not decide who may see a library. An application that
serves several people does those things itself and uses h/g underneath. This
page is the contract between the two.

For how h/g behaves when several clients write at once, see
[Several assistants, one library](concurrency.md).

## Who does what

| The hosting application | h/g |
| --- | --- |
| Signs people in; keeps sessions and credentials. | Nothing. h/g never sees a password or token. |
| Decides which libraries a person belongs to. | Nothing. |
| Picks the library for each request. | Serves exactly the one library its process or harness was started on. |
| Decides what that person may do there. | Enforces the policy it is handed, on every call. |
| Network exposure, TLS, rate limits, quotas. | Binds to loopback and checks Host and Origin. |
| Records who did what. | Stamps `recorded_by` on memories when told who the caller is. |
| Backups, retention and erasure requests. | Backup and restore tools. Forget is recoverable removal, not erasure. |

## Start with one library per person or group

The simplest hosting model needs no feature of h/g at all: give each person or
group its own library directory and route each authenticated request to it.

- A library is chosen when its process or harness starts: `hunt serve DIR`,
  `hunt-mcp --library DIR`, or `build_library_harness(library_settings(DIR))`
  inside your own Python process.
- **No tool argument names a database, a directory or a user.** A model cannot
  reach another library through anything it puts in a call. Every result carries
  an opaque `store.id` so you can check which library answered.
- Each library has its own database, its own folder of original files, and its
  own caches, search cursors, activity feed and background upkeep. Nothing is
  shared between two harnesses.
- Put each library on a disk attached to the server. h/g chooses how a library's
  file is shared from where it lives: processes work side by side on a local
  disk and take turns on a network volume. See
  [how the file is shared](concurrency.md#how-the-file-is-shared).

```python
from hunter_gatherer.library import build_library_harness, library_settings
from hunter_gatherer.mcp.server import build_server

harnesses = {}

def server_for(library_directory):          # called after your own membership check
    if library_directory not in harnesses:
        harnesses[library_directory] = build_library_harness(library_settings(library_directory))
    return build_server(harness=harnesses[library_directory], tool_profile="library")[0]
```

Everyone admitted to a library can then do everything the six tools offer in
it. If that is what membership means for you, stop here.

## Different permissions inside one shared library

When members of one library should not all be able to do the same things, hand
h/g an `AccessPolicy`. It is the smallest interface that covers every
operation, and it only ever comes from your code.

| Field | Default | Meaning |
| --- | --- | --- |
| `read_sensitive` | `True` | Honour `include_sensitive=True`. When `False`, such calls are refused, and the caller cannot cite sensitive sources or have them echoed back. |
| `write` | `True` | Change memories, collections, annotations and coordination records. |
| `import_files` | `True` | Read files from the server's filesystem into the library. |
| `import_roots` | none | Supply a collection of directories, e.g. `(uploads_dir,)` for one folder. Imports are confined to those folders, including when following links or `..`. |
| `administer` | `True` | Backups, maintenance, the agent runtime and any tool not classified below. Also needed to be shown the database path. |
| `principal` | empty | Your name for the caller. Written as `recorded_by` on memories it saves. Never used to decide anything. |

Reading ordinary records is implied. With no policy at all, nothing is
restricted; that is the single-user behaviour and it is unchanged.

There are three ways to apply one. All of them can only narrow: a request scope
inside a restricted harness gives the intersection, never more.

**An endpoint per role.** Build a server on a restricted view of the harness and
route people to the endpoint that matches their role.

```python
from hunter_gatherer.access import AccessPolicy, restricted_view

readers = restricted_view(harness, AccessPolicy(write=False, import_files=False, administer=False))
reader_server = build_server(harness=readers, tool_profile="library")[0]
```

**A policy per request.** Decide after authentication and wrap the request. This
reaches the MCP tool handlers of the HTTP endpoint as h/g serves it.

```python
from hunter_gatherer.access import AccessPolicy, access_scope

class Authenticated:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        user = await your_sign_in_check(scope)          # yours; reject before this point if it fails
        policy = AccessPolicy(write=user.may_edit, read_sensitive=user.is_owner,
                              import_files=False, administer=False, principal=user.id)
        with access_scope(policy):
            await self.app(scope, receive, send)
```

**A policy around a direct call**, when you call h/g in-process:

```python
with access_scope(policy):
    result = call_tool(harness, "hg_find", {"query": question})
```

A refused call returns `error_code: "access_denied"`. It is decided before
anything is read, so it reveals nothing about the library.

## What a policy covers

Every tool call enters through one dispatcher, which classifies the call by
tool **and action**. A family that mixes reading and writing is judged action by
action: `hg_memory profile` is a read, `hg_memory forget` is a write.

| Operation | Needs |
| --- | --- |
| Find, Open, profile, memory history, collection list/get/export/refs, import status and list, status | Nothing beyond admission to the library. |
| The same with `include_sensitive=true` | `read_sensitive` |
| Remember, replace, forget, save a collection, add or revoke annotations | `write` |
| Import preview, create and run | `import_files`, and a path inside `import_roots` |
| Backup, consolidation, evaluation, agent delegation, pipelines | `administer` |
| A tool or action added later and not yet classified | `administer`, or at least `write` for a new action of a known family |

How the parts of a library behave for a restricted caller:

| Part | Behaviour |
| --- | --- |
| Search results | Sensitive records are absent unless asked for, as always; asking is refused without `read_sensitive`. |
| Opening a citation | Checked against the record's current privacy on every call. An ID from an earlier, more privileged session opens nothing more. |
| Linked sources | Revalidated on each read. A caller who cannot read a source cannot attach it to a new memory, which would confirm what it says. |
| Original files and page images | Reached only through citations h/g issued. Open never accepts a path. |
| Earlier versions | History follows the same rule; a hidden or forgotten link ends the chain. |
| Exports | Sources are checked again when the export is made, not when the collection was saved. |
| Search cursors | Unguessable, held in the serving process, and valid only with the same sensitive setting. They reach nothing a fresh search would not. |
| Caches | Per library and per process. The embedding cache holds hashes and vectors, not text. |
| Background upkeep | Rebuilds derived search indexes only. It never changes or returns a record. |
| Activity feed, import uploads, the browser interface (`/api/library`) | The owner's own interface, which takes its privacy choices from request parameters. It answers 403 for a restricted harness. |

## What is not a permission

These are supplied by the caller, usually a model, and h/g treats them as
labels or requests:

- `include_sensitive` asks for sensitive records. It is honoured only where the
  policy allows it. Without a policy it is the owner's own per-call opt-in.
- Actor and workspace names in the coordination tools organize work between
  trusted clients. Anyone can write any name.
- `source` on a memory is whatever the caller typed. Use `principal` and
  `recorded_by` when you need to know who wrote something.
- The tool profile (`library` or `full`) changes which tools are advertised. It
  restricts nothing.
- Session IDs separate conversations. They identify no one.

## Limits

- A policy applies to a whole call. There are no per-record access lists:
  everyone admitted to a library can read all of its non-sensitive records, and
  "sensitive" is one class, not one per person.
- Do not offer the `full` profile to restricted callers unless you mean to. It
  includes an agent runtime with its own tools; those need `administer`, but the
  six-tool profile is the surface this contract was written for.
- Import job status, and document listings in the full catalogue, show the
  paths files were imported from. Filter them if those paths are confidential.
- `recorded_by` is written on remember and replace. Collections and annotations
  carry the labels their own tools take.
- h/g keeps no access log. If you need one, write it where you authenticate.
- Deploying on an untrusted network still needs your own transport security in
  front. See the [security policy](../SECURITY.md).
