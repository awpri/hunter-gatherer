import type {LibraryDocument} from './libraryApi';

export interface SourceTextSegment {text: string; start: number; end: number; chunk_id?: string; citation?: string}
export interface MappedSourceText {
  status: 'mapped' | 'missing' | 'invalid';
  passages: SourceTextSegment[][];
  complete: boolean;
  coverage: 'full' | 'partial' | 'none';
}
const offset = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const referenceId = /^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;

/** Offsets are Python codepoints, not JavaScript UTF-16 indexes. Reject a malformed
 * packet as a whole: a plausible citation must never decorate unrelated text. */
export function mapSourceText(doc: LibraryDocument): MappedSourceText {
  const passages = doc.passages || [];
  const fallback = (status: 'missing' | 'invalid'): MappedSourceText => ({
    status, complete: false, coverage: 'none',
    passages: passages.map(passage => [{text: passage.text, start: passage.start, end: passage.end}]),
  });
  const map = doc.citation_map;
  if (map === undefined) return fallback('missing');
  if (!map || !referenceId.test(doc.document_id) || map.coordinate_system !== 'section_codepoints_half_open' ||
      !['full', 'partial', 'none'].includes(map.coverage) || typeof map.complete !== 'boolean' ||
      !Array.isArray(map.spans) || map.spans.length > 128 || (map.omitted_reasons !== undefined &&
        (!Array.isArray(map.omitted_reasons) || map.omitted_reasons.some(reason => typeof reason !== 'string')))) return fallback('invalid');
  const points = passages.map(passage => Array.from(passage.text));
  if (passages.some((passage, index) => !offset(passage.start) || !offset(passage.end) ||
    passage.end < passage.start || passage.end - passage.start !== points[index].length)) return fallback('invalid');
  const grouped = passages.map(() => [] as typeof map.spans);
  for (const span of map.spans) {
    if (!span || !offset(span.passage_index) || span.passage_index >= passages.length ||
        !offset(span.start) || !offset(span.end) || span.end <= span.start ||
        typeof span.chunk_id !== 'string' || !referenceId.test(span.chunk_id) ||
        span.citation !== `[doc:${doc.document_id}#${span.chunk_id}]`) return fallback('invalid');
    const passage = passages[span.passage_index];
    if (span.start < passage.start || span.end > passage.end) return fallback('invalid');
    grouped[span.passage_index].push(span);
  }
  const result: SourceTextSegment[][] = [];
  let cited = 0, total = 0;
  for (let index = 0; index < passages.length; index++) {
    const passage = passages[index], spans = grouped[index].sort((a, b) => a.start - b.start);
    const segments: SourceTextSegment[] = [];
    let cursor = passage.start;
    const text = (start: number, end: number) => points[index].slice(start - passage.start, end - passage.start).join('');
    for (const span of spans) {
      if (span.start < cursor) return fallback('invalid');
      if (span.start > cursor) segments.push({text: text(cursor, span.start), start: cursor, end: span.start});
      segments.push({text: text(span.start, span.end), start: span.start, end: span.end, chunk_id: span.chunk_id, citation: span.citation});
      cited += span.end - span.start;
      cursor = span.end;
    }
    if (cursor < passage.end || !segments.length) segments.push({text: text(cursor, passage.end), start: cursor, end: passage.end});
    total += passage.end - passage.start;
    result.push(segments);
  }
  const coverage = cited === 0 ? 'none' : cited === total ? 'full' : 'partial';
  if (coverage !== map.coverage) return fallback('invalid');
  return {status: 'mapped', passages: result, complete: map.complete, coverage};
}
