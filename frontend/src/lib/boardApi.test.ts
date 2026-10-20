import {afterEach, describe, expect, it, vi} from 'vitest';
import {BoardApi, BoardConflictError, MAX_VISUAL_BYTES, MAX_VISUAL_INPUT_BYTES, groupEditorChanges, isVisualEmoji, prepareVisualImage, validateVisualFile, visualImage} from './boardApi';
const board = {version: 1, revision: 4, layout: 'board', grouping: 'subjects', follow_agent: true, groups: {}, records: {}};
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
describe('versioned board changes', () => {
  it('sends revision and privacy for a partial change, preserving explicit null clears', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({...board, revision: 5}));
    await new BoardApi(fetcher).patch(4, {records: {'memory:fixture': {image: null}}}, true);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe('/api/library/board?include_sensitive=true');
    expect(init?.method).toBe('PATCH'); expect(init?.credentials).toBe('same-origin');
    expect(JSON.parse(init?.body as string)).toEqual({revision: 4, include_sensitive: true, records: {'memory:fixture': {image: null}}});
  });
  it('refreshes conflicts without automatically retrying an outdated write', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(response({detail: 'Changed'}, 409)).mockResolvedValueOnce(response({...board, revision: 7}));
    const api = new BoardApi(fetcher);
    try {await api.patch(4, {layout: 'columns'}); throw new Error('Expected conflict');} catch (error) {expect(error).toBeInstanceOf(BoardConflictError); expect((error as BoardConflictError).latest?.revision).toBe(7);}
    expect(fetcher).toHaveBeenCalledTimes(2); expect(fetcher.mock.calls[1][1]?.method).toBe('GET');
  });
  it('discards a stale private result even when the network ignores cancellation', async () => {
    let resolve!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(done => {resolve = done;}));
    const api = new BoardApi(fetcher), pending = api.load(true);
    const rejected = expect(pending).rejects.toMatchObject({name: 'AbortError'});
    api.invalidate(); resolve(response({...board, groups: {private: {title: 'Private fixture'}}})); await rejected;
  });
  it('surfaces validation errors in a readable message', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({detail: [{loc: ['body', 'title'], msg: 'Group name is too long.'}]}, 422));
    await expect(new BoardApi(fetcher).createGroup(4, {title: 'Fixture'})).rejects.toThrow('Group name is too long.');
  });
  it('creates groups with explicit privacy and canonical record ids', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({...board, group_id: 'group:manual:fixture'}));
    await new BoardApi(fetcher).createGroup(4, {title: 'Sketches', kind: 'topic', record_ids: ['memory:fixture']}, false);
    expect(fetcher.mock.calls[0][0]).toBe('/api/library/groups?include_sensitive=false');
    expect(JSON.parse(fetcher.mock.calls[0][1]?.body as string)).toMatchObject({title: 'Sketches', kind: 'topic', revision: 4, include_sensitive: false});
  });
});
describe('local visual boundaries', () => {
  const png = new Uint8Array([137,80,78,71,13,10,26,10]);
  it('rejects SVG, mismatched raster signatures, empty and oversized images', () => {
    expect(() => validateVisualFile({size: 100, type: 'image/svg+xml'}, png)).toThrow('PNG, JPEG, or WebP');
    expect(() => validateVisualFile({size: 100, type: 'image/jpeg'}, png)).toThrow();
    expect(() => validateVisualFile({size: 0, type: 'image/png'}, png)).toThrow('nonempty');
    expect(() => validateVisualFile({size: MAX_VISUAL_BYTES + 1, type: 'image/png'}, png)).toThrow('1 MiB');
  });
  it('accepts phone photos within the separate input bound for local compression', () => {
    expect(() => validateVisualFile({size: 5 * MAX_VISUAL_BYTES, type: 'image/png'}, png, MAX_VISUAL_INPUT_BYTES)).not.toThrow();
    expect(() => validateVisualFile({size: MAX_VISUAL_INPUT_BYTES + 1, type: 'image/png'}, png, MAX_VISUAL_INPUT_BYTES)).toThrow('15 MiB');
  });
  it('recognizes saved emoji beyond the compact picker while keeping named icons distinct', () => {
    expect(isVisualEmoji('💾')).toBe(true); expect(isVisualEmoji('🌿')).toBe(true);
    expect(isVisualEmoji('topics')).toBe(false); expect(isVisualEmoji('<script>')).toBe(false);
  });
  it('accepts local raster URLs only for previews', () => {
    expect(visualImage('data:image/png;base64,iVBORw==')).toBe('data:image/png;base64,iVBORw==');
    for (const value of ['https://example.org/image.jpg', 'data:image/svg+xml;base64,abcd', 'javascript:alert(1)', 'data:image/png;base64,' + 'A'.repeat(2 * MAX_VISUAL_BYTES)]) expect(visualImage(value)).toBeUndefined();
  });
});


describe('image preparation on the device', () => {
  afterEach(() => vi.unstubAllGlobals());
  function imageEnvironment(size = 100) {
    const bitmap = {width: 4000, height: 3000, close: vi.fn()};
    const drawImage = vi.fn();
    const canvas = {width: 0, height: 0, getContext: () => ({drawImage}), toBlob: vi.fn((done: (blob: Blob) => void) => done(new Blob([new Uint8Array(size)], {type: 'image/webp'})))};
    vi.stubGlobal('createImageBitmap', vi.fn().mockResolvedValue(bitmap));
    vi.stubGlobal('document', {createElement: vi.fn().mockReturnValue(canvas)});
    vi.stubGlobal('FileReader', class {result = 'data:image/webp;base64,UklGRg=='; onload?: () => void; readAsDataURL() {this.onload?.();}});
    const bytes = new Uint8Array([137,80,78,71,13,10,26,10]);
    const file = {size: 5 * MAX_VISUAL_BYTES, type: 'image/png', slice: () => new Blob([bytes])} as File;
    return {bitmap, drawImage, canvas, file};
  }
  it('resizes a phone photo to 1200px and encodes a bounded WebP', async () => {
    const {bitmap, canvas, drawImage, file} = imageEnvironment();
    await expect(prepareVisualImage(file)).resolves.toBe('data:image/webp;base64,UklGRg==');
    expect(canvas.width).toBe(1200); expect(canvas.height).toBe(900);
    expect(drawImage).toHaveBeenCalledWith(bitmap, 0, 0, 1200, 900);
    expect(canvas.toBlob).toHaveBeenCalledWith(expect.any(Function), 'image/webp', .82);
    expect(bitmap.close).toHaveBeenCalledOnce();
  });
  it('rejects an image still too large after compression and releases its bitmap', async () => {
    const {bitmap, canvas, file} = imageEnvironment(MAX_VISUAL_BYTES + 1);
    await expect(prepareVisualImage(file)).rejects.toThrow('too detailed');
    expect(canvas.toBlob).toHaveBeenCalledTimes(3); expect(bitmap.close).toHaveBeenCalledOnce();
  });
});


describe('group editing by arrangement', () => {
  it('omits memberships and kind from derived kind/source visual edits', () => {
    const draft = {title: 'Ideas', description: 'Sketches', icon: '🎨', image: undefined, kind: 'topic' as const, sensitive: true, record_ids: ['memory:fixture']};
    for (const id of ['kind:Fact', 'source:Saved files']) expect(groupEditorChanges(id, draft)).toEqual({title: 'Ideas', description: 'Sketches', icon: '🎨', image: undefined});
    expect(groupEditorChanges('group:manual:fixture', draft)).toEqual(draft);
    expect(groupEditorChanges('group:subject:fixture', draft)).toEqual(draft);
  });
});

it('writes reviewed aliases without changing source records, retaining null for undo', async () => {
  const fetcher = vi.fn<typeof fetch>().mockImplementation(async()=>new Response(JSON.stringify({version:1,revision:5,layout:'board',grouping:'subjects',follow_agent:true,groups:{},records:{},aliases:{}}),{status:200,headers:{'Content-Type':'application/json'}}));
  const api = new BoardApi(fetcher);
  await api.patch(4,{aliases:{'group:source':'group:target'}},false);
  expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({revision:4,include_sensitive:false,aliases:{'group:source':'group:target'}});
  await api.patch(5,{aliases:{'group:source':null}},false);
  expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body)).aliases).toEqual({'group:source':null});
});
