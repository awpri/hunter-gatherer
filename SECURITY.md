# Security policy

## Reporting a vulnerability

Do not include credentials, private library contents or exploit details in a public issue.
A private reporting channel must be configured and documented before public release.
No response deadline is currently promised.

## System and scope

hunter/gatherer is a local, single-user evidence library with a browser interface
and MCP tools. Its documented demo binds to loopback and uses a separate fictional
library. The repository also retains optional agent, model-provider, voice and
automation code. Those components remain in review scope when reachable through
their supported entry points.

The local application is not a multi-tenant hosted service. Deploying it on an
untrusted network requires a separately designed authentication and access boundary.

## Trust boundaries

Library files, imported documents, extracted text, source references and model
outputs are untrusted input. They must not grant filesystem access, execute
instructions or bypass the library's access decisions. Connected MCP clients can
perform the operations granted by the configured server; a smaller tool catalog
is not an authorization mechanism.

## Required properties

- Read, export, copy and source-opening operations must enforce current access and
  privacy rules, including linked records, cached results and collections.
- Document handling must respect permitted paths, reject traversal and unsafe
  symlink or reparse-point resolution, and bound parsing and rendering work.
- The documented local web surface must enforce its intended host/origin boundary.
- Source identifiers, page locations and version references must retain their
  meaning; generated annotations must not be presented as original source text.
- Backup, restore and demo operations must not silently overwrite unrelated
  libraries or import personal configuration into fictional fixtures.
- Optional provider calls must follow explicit configuration and data-sharing
  choices. Secrets and private library content must not be committed to this repo.

## Reporting context

Assess findings by the reachable entry point, attacker control and demonstrated
impact. Local deployment does not by itself make malicious-document handling,
cross-origin requests or private-data exposure harmless.

No vulnerability category, component or known risk is excluded by this policy.
Tests and security controls describe intended behavior; they do not establish that
all paths are safe. Optional integrations and supported platform behavior require
their own verification.
