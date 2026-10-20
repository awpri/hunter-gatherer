import {describe, expect, it, vi} from 'vitest';
import type {ReactElement} from 'react';
import type {LibraryDocument} from '../lib/libraryApi';
import {SourceText} from './SourceText';

function elements(node: unknown): ReactElement<any>[] {
  if (Array.isArray(node)) return node.flatMap(elements);
  if (!node || typeof node !== 'object' || !('props' in node)) return [];
  const element = node as ReactElement<any>;
  return [element, ...elements(element.props.children)];
}
function fixture(): LibraryDocument {
  return {document_id: 'dddddddddddddddddddddddddddddddd', title: 'Fixture', version: 1, created_at: 0,
    passages: [{section_id: 's1', section: 'First', start: 100, end: 113, text: '😀first second'}],
    citation_map: {coordinate_system: 'section_codepoints_half_open', coverage: 'partial', complete: true, spans: [
      {passage_index: 0, start: 100, end: 106, chunk_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', citation: '[doc:dddddddddddddddddddddddddddddddd#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]'},
      {passage_index: 0, start: 107, end: 113, chunk_id: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', citation: '[doc:dddddddddddddddddddddddddddddddd#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]'},
    ]}};
}
describe('shared source text renderer', () => {
  it('pairs distinct exact spans with their own citations and chunk navigation', () => {
    const doc = fixture(), select = vi.fn();
    const nodes = elements(SourceText({doc, select}));
    expect(nodes.filter(node => node.props['data-source-text']).map(node => node.props.children).join('')).toBe(doc.passages![0].text);
    const segments = nodes.filter(node => String(node.props.className).startsWith('source-text-span '));
    expect(segments).toHaveLength(3);
    expect(elements(segments[0]).find(node => node.type === 'code')?.props.children).toBe('[doc:dddddddddddddddddddddddddddddddd#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa]');
    expect(elements(segments[2]).find(node => node.type === 'code')?.props.children).toBe('[doc:dddddddddddddddddddddddddddddddd#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]');
    expect(elements(segments[1]).some(node => node.props.children === 'Uncited text')).toBe(true);
    const buttons = nodes.filter(node => node.type === 'button');
    expect(buttons).toHaveLength(2);
    buttons[1].props.onClick();
    expect(select).toHaveBeenCalledWith({type: 'document', id: 'dddddddddddddddddddddddddddddddd', chunk: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'});
    expect(buttons[1].props['aria-label']).toBe('Open exact passage [doc:dddddddddddddddddddddddddddddddd#bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb]');
  });
  it('preserves legacy wording and explicitly reports unavailable citations', () => {
    const doc = fixture(); delete doc.citation_map;
    const nodes = elements(SourceText({doc, select: vi.fn()}));
    expect(nodes.filter(node => node.props['data-source-text']).map(node => node.props.children).join('')).toBe(doc.passages![0].text);
    expect(nodes.some(node => node.type === 'button')).toBe(false);
    expect(nodes.some(node => node.props.children === 'Passage citations are unavailable for this source text.')).toBe(true);
  });
  it('renders malformed mapping without attaching any citation or navigation', () => {
    const doc = fixture(); doc.citation_map!.spans[0].citation = '[doc:dddddddddddddddddddddddddddddddd#ffffffffffffffffffffffffffffffff]';
    const nodes = elements(SourceText({doc, select: vi.fn()}));
    expect(nodes.some(node => node.type === 'button' || node.type === 'code')).toBe(false);
    expect(nodes.some(node => node.props.children === 'Passage citations could not be matched safely to this source text.')).toBe(true);
  });
  it('disables citation navigation during reader mutations', () => {
    const nodes = elements(SourceText({doc: fixture(), select: vi.fn(), disabled: true}));
    expect(nodes.filter(node => node.type === 'button').every(node => node.props.disabled)).toBe(true);
  });
  it.each(['partial', 'none', 'full'] as const)('reports capped mapping with %s coverage', coverage => {
    const doc = fixture();
    doc.citation_map!.complete = false;
    doc.citation_map!.coverage = coverage;
    if (coverage === 'none') doc.citation_map!.spans = [];
    if (coverage === 'full') doc.citation_map!.spans[0].end = 107;
    const nodes = elements(SourceText({doc, select: vi.fn()}));
    expect(nodes.some(node => node.props.role === 'status' && node.props.children === 'Passage citation mapping reached its limit. Some available citations may be omitted.')).toBe(true);
    expect(nodes.filter(node => node.props['data-source-text']).map(node => node.props.children).join('')).toBe(doc.passages![0].text);
  });
});
