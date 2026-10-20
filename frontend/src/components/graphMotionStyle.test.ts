import {describe, expect, it} from 'vitest';
// The test runner provides Node; production compilation does not ship its types.
// @ts-expect-error Node declarations are intentionally absent from the frontend.
import {readFileSync} from 'node:fs';
const styles: string = readFileSync(new URL('./ConnectionMap.css', import.meta.url), 'utf8');

describe('paused graph arrivals', () => {
  it('reveals new groups, records, and edges immediately while motion is paused', () => {
    const rule = styles.match(/\.motion-paused \.cm-record-surface[^}]+\}/)?.[0] || '';
    for (const selector of ['.cm-group', '.cm-node-arrival', '.cm-edge-entry', '.cm-fan-card']) expect(rule).toContain(selector);
    expect(rule).toContain('animation: none');
    expect(rule).not.toContain('animation-play-state: paused');
  });
});
