# Searchable PDF image regions

The existing six-tool library interface supports authored image descriptions without requiring a text caption. No vision model runs during Find or Open. Descriptions are discovery annotations, explicitly unverified and never returned as source quotations.

After inspecting an original PDF page, use Import `action="enrich"` with the usual document ID, expected version/content hash, producer, records and top-level idempotency key. A visual record is:

```json
{"key":"figure-1","kind":"visual_region","text":"Description of the visible diagram","anchors":[],"region":{"page":1,"bbox":[0.1,0.2,0.8,0.7]}}
```

Bounds are finite normalized `[left, top, right, bottom]` coordinates relative to the displayed visible CropBox, after intrinsic PDF rotation. The rectangle must have positive area and remain inside `[0,1]`. Existing batches bind document version and the SHA-256 of original bytes. Limits remain 50 records, 2,000 description characters each and 128 KiB canonical input. PDF/page availability is checked when opened; saving an annotation does not verify its interpretation or page existence.

Find exposes a separate `visual_regions` array with `description`, `verification`, exact `source_reference`, stable `[region:<record UUID>]` citation and Open followup. This is bounded lexical discovery over saved descriptions, not visual similarity search. Find considers at most 256 FTS candidates ranked lexically, revalidates their visibility and identity, and pages admitted regions through its normal continuation cursor. An exact reference may be returned with an empty description and `description_omitted:true` when the full description cannot fit. Follow its Open call to inspect the original; repeat `include_sensitive` on continuation. The candidate-limit flag is based on valid resolved candidates, so filtering can understate whether the raw scan cap was reached; this is explicitly non-exhaustive discovery.

Open the citation with `view="region"` for a crop or `view="page"` for context. HTTP uses `GET /api/library/regions/{record_id}/image?view=region` (or `page`), optionally `include_sensitive=true`. Responses use the same `{image:{mime_type,data}}` HTTP shape and MCP image content as page previews. `source_reference` identifies the original page; `region_reference` additionally includes bbox. The record citation identifies the active saved annotation.

Rendering reuses the isolated, time-limited page worker, 4M-pixel allocation cap and 2 MiB PNG cap. It first renders a bounded displayed page, then crops pixels using floor for left/top and ceil for right/bottom. Tiny regions are not upscaled beyond page-render resolution. Managed original paths and bytes are checked by the original-page pipeline; fresh access and active annotation checks run again before returning pixels. Hidden, deleted, revoked, changed-hash and superseded-document region references fail closed. Exact historical page citations retain their preexisting independent behavior.

Region references are deliberately unsupported in collections and exports in this initial release: the existing collection validator rejects `[region:...]` as an unsupported citation. Use the exact original page as the collection source when page context is wanted. No image description is silently converted into a quotation or durable fact.
