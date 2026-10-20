import {describe, expect, it} from 'vitest';
import type {LibraryDocument} from './libraryApi';
import {mapSourceText} from './sourceCitations';

function fixture(): LibraryDocument {
  return {document_id: 'dddddddddddddddddddddddddddddddd', title: 'Fixture', version: 1, created_at: 0,
    passages: [{section_id: 's1', start: 10, end: 22, text: '😀e\u0301\0 gap end'}],
    citation_map: {coordinate_system: 'section_codepoints_half_open', coverage: 'partial', complete: true,
      spans: [{passage_index: 0, start: 10, end: 14, chunk_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', citation: '[doc:dddddddddddddddddddddddddddddddd#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]'},
        {passage_index: 0, start: 19, end: 22, chunk_id: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', citation: '[doc:dddddddddddddddddddddddddddddddd#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]'}]}};
}
describe('section citation mapping', () => {
  it('preserves every codepoint including emoji, combining marks, NUL and uncited gaps', () => {
    const doc = fixture();
    const result = mapSourceText(doc);
    expect(result.status).toBe('mapped');
    expect(result.complete).toBe(true);
    expect(result.passages[0]).toEqual([
      {text: '😀e\u0301\0', start: 10, end: 14, chunk_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', citation: '[doc:dddddddddddddddddddddddddddddddd#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]'},
      {text: ' gap ', start: 14, end: 19},
      {text: 'end', start: 19, end: 22, chunk_id: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', citation: '[doc:dddddddddddddddddddddddddddddddd#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]'},
    ]);
    expect(result.passages[0].map(segment => segment.text).join('')).toBe(doc.passages![0].text);
  });
  it('maps a bounded middle slice by section absolute offsets', () => {
    const doc = fixture();
    doc.passages![0] = {section_id: 's1', start: 12, end: 21, text: '\u0301\0 gap en'};
    doc.citation_map!.spans[0].start = 12;
    doc.citation_map!.spans[1].end = 21;
    const result = mapSourceText(doc);
    expect(result.status).toBe('mapped');
    expect(result.passages[0].map(segment => segment.text)).toEqual(['\u0301\0', ' gap ', 'en']);
  });
  it('keeps passages separate even when sections reuse absolute offsets', () => {
    const doc = fixture();
    doc.passages!.push({section_id: 's2', start: 0, end: 3, text: 'two'});
    doc.citation_map!.spans.push({passage_index: 1, start: 0, end: 3, chunk_id: 'cccccccccccccccccccccccccccccccc', citation: '[doc:dddddddddddddddddddddddddddddddd#cccccccccccccccccccccccccccccccc]'});
    const result = mapSourceText(doc);
    expect(result.status).toBe('mapped');
    expect(result.passages.map(segments => segments.map(segment => segment.text).join(''))).toEqual(['😀e\u0301\0 gap end', 'two']);
  });
  it('preserves leading, trailing, and wholly uncited text', () => {
    const doc = fixture();
    doc.citation_map!.spans = [{passage_index: 0, start: 14, end: 19, chunk_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', citation: '[doc:dddddddddddddddddddddddddddddddd#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]'}];
    expect(mapSourceText(doc).passages[0].map(segment => [segment.text, segment.chunk_id])).toEqual([
      ['😀e\u0301\0', undefined], [' gap ', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], ['end', undefined],
    ]);
    doc.citation_map!.spans = [];
    doc.citation_map!.coverage = 'none';
    expect(mapSourceText(doc)).toMatchObject({status: 'mapped', coverage: 'none', complete: true,
      passages: [[{text: doc.passages![0].text}]]});
  });
  it('uses an uncited fallback for legacy responses without a map', () => {
    const doc = fixture(); delete doc.citation_map;
    expect(mapSourceText(doc)).toMatchObject({status: 'missing', passages: [[{text: doc.passages![0].text}]]});
  });
  it('retains partial metadata when the backend deliberately caps mapping', () => {
    const doc = fixture(); doc.citation_map!.complete = false; doc.citation_map!.omitted_reasons = ['citation_limit'];
    expect(mapSourceText(doc)).toMatchObject({status: 'mapped', complete: false, coverage: 'partial'});
  });
  it('accepts lowercase hyphenated UUIDs as well as 32-hex IDs', () => {
    const doc = fixture();
    doc.document_id = 'dddddddd-dddd-dddd-dddd-dddddddddddd';
    doc.citation_map!.spans[0].chunk_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
    for (const span of doc.citation_map!.spans) span.citation = `[doc:${doc.document_id}#${span.chunk_id}]`;
    expect(mapSourceText(doc).status).toBe('mapped');
  });
  it.each([
    ['overlapping spans', (doc: LibraryDocument) => {doc.citation_map!.spans[1].start = 13;}],
    ['out of bounds', (doc: LibraryDocument) => {doc.citation_map!.spans[0].start = 9;}],
    ['fractional offset', (doc: LibraryDocument) => {doc.citation_map!.spans[0].start = 10.5;}],
    ['unsafe integer', (doc: LibraryDocument) => {doc.citation_map!.spans[0].end = Number.MAX_SAFE_INTEGER + 1;}],
    ['empty interval', (doc: LibraryDocument) => {doc.citation_map!.spans[0].end = 10;}],
    ['unknown passage', (doc: LibraryDocument) => {doc.citation_map!.spans[0].passage_index = 9;}],
    ['wrong document citation', (doc: LibraryDocument) => {doc.citation_map!.spans[0].citation = '[doc:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]';}],
    ['wrong chunk citation', (doc: LibraryDocument) => {doc.citation_map!.spans[0].citation = '[doc:dddddddddddddddddddddddddddddddd#ffffffffffffffffffffffffffffffff]';}],
    ['empty chunk identity', (doc: LibraryDocument) => {doc.citation_map!.spans[0].chunk_id = '';}],
    ['arbitrary chunk ID with matching citation', (doc: LibraryDocument) => {doc.citation_map!.spans[0].chunk_id = 'c1'; doc.citation_map!.spans[0].citation = `[doc:${doc.document_id}#c1]`;}],
    ['arbitrary document ID with matching citations', (doc: LibraryDocument) => {doc.document_id = 'source-1'; for (const span of doc.citation_map!.spans) span.citation = `[doc:source-1#${span.chunk_id}]`;}],
    ['uppercase ID with matching citation', (doc: LibraryDocument) => {doc.citation_map!.spans[0].chunk_id = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'; doc.citation_map!.spans[0].citation = `[doc:${doc.document_id}#AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA]`;}],
    ['wrong text extent', (doc: LibraryDocument) => {doc.passages![0].end = 23;}],
    ['wrong coverage', (doc: LibraryDocument) => {doc.citation_map!.coverage = 'full';}],
    ['wrong coordinate system', (doc: LibraryDocument) => {(doc.citation_map as any).coordinate_system = 'utf16';}],
    ['null span', (doc: LibraryDocument) => {(doc.citation_map!.spans as any[]).push(null);}],
    ['non-array spans', (doc: LibraryDocument) => {(doc.citation_map as any).spans = {};}],
    ['over-budget spans', (doc: LibraryDocument) => {doc.citation_map!.spans = Array(129).fill(doc.citation_map!.spans[0]);}],
    ['non-boolean completeness', (doc: LibraryDocument) => {(doc.citation_map as any).complete = 'yes';}],
    ['null map', (doc: LibraryDocument) => {(doc as any).citation_map = null;}],
  ])('fails closed for %s while preserving source wording', (_label, mutate) => {
    const doc = fixture(); mutate(doc);
    const result = mapSourceText(doc);
    expect(result.status).toBe('invalid');
    expect(result.passages.flat().every(segment => !segment.citation && !segment.chunk_id)).toBe(true);
    expect(result.passages[0].map(segment => segment.text).join('')).toBe(doc.passages![0].text);
  });
});
