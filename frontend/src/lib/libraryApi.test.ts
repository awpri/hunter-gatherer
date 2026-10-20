import { describe, expect, it, vi } from 'vitest';
import { LibraryApi, StaleRequestError } from './libraryApi';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

function response(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

describe('LibraryApi privacy boundary', () => {
  it('rejects an old private response when fetch ignores abort', async () => {
    const pending = deferred<Response>();
    const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending.promise);
    const api = new LibraryApi(fetcher);
    const request = api.request('memories', { include_sensitive: true });
    const rejected = expect(request).rejects.toBeInstanceOf(StaleRequestError);
    const signal = fetcher.mock.calls[0][1]?.signal;
    const privateResponse = response({ memories: [{ text: 'Private test fixture' }] });
    const parse = vi.spyOn(privateResponse, 'json');

    api.invalidate();
    expect(signal?.aborted).toBe(true);
    pending.resolve(privateResponse);

    await rejected;
    expect(parse).not.toHaveBeenCalled();
  });

  it('rejects private data when the privacy scope changes during JSON parsing', async () => {
    const body = deferred<unknown>();
    const parsing = deferred<void>();
    const privateResponse = response({});
    vi.spyOn(privateResponse, 'json').mockImplementation(() => { parsing.resolve(); return body.promise; });
    const api = new LibraryApi(vi.fn<typeof fetch>().mockResolvedValue(privateResponse));
    const request = api.request('graph', { include_sensitive: true });
    const rejected = expect(request).rejects.toMatchObject({ name: 'AbortError' });

    await parsing.promise;
    api.invalidate();
    body.resolve({ nodes: [{ label: 'Private test fixture' }] });

    await rejected;
  });

  it('allows a new public request while rejecting an older private request', async () => {
    const oldResponse = deferred<Response>();
    const fetcher = vi.fn<typeof fetch>().mockReturnValueOnce(oldResponse.promise).mockResolvedValueOnce(response({ memories: [] }));
    const api = new LibraryApi(fetcher);
    const oldRequest = api.request('memories', { include_sensitive: true });
    const rejected = expect(oldRequest).rejects.toBeInstanceOf(StaleRequestError);

    api.invalidate();
    await expect(api.request('memories', { include_sensitive: false })).resolves.toEqual({ memories: [] });
    expect(fetcher.mock.calls[1][0]).toBe('/api/library/memories?include_sensitive=false');
    expect(fetcher.mock.calls[1][1]?.signal?.aborted).toBe(false);
    oldResponse.resolve(response({ memories: [{ text: 'Private test fixture' }] }));
    await rejected;
  });

  it('keeps opt-in request-specific and preserves an explicit false privacy flag', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => response({}));
    const api = new LibraryApi(fetcher);
    await api.request('overview', { include_sensitive: true });
    // An omitted flag uses the server's public-only default; a prior request
    // must never silently opt the following request into private records.
    await api.request('documents');
    await api.request('health', { include_sensitive: false, verify_originals: false });

    expect(fetcher.mock.calls[0][0]).toBe('/api/library/overview?include_sensitive=true');
    expect(fetcher.mock.calls[1][0]).toBe('/api/library/documents');
    expect(fetcher.mock.calls[2][0]).toBe('/api/library/health?include_sensitive=false&verify_originals=false');
    expect(fetcher.mock.calls.every(([, options]) => options?.credentials === 'same-origin')).toBe(true);
  });
});

describe('LibraryApi request encoding', () => {
  it('cancels one browse without cancelling concurrent activity', async () => {
    const pending = deferred<Response>();
    const fetcher = vi.fn<typeof fetch>().mockReturnValueOnce(pending.promise).mockResolvedValueOnce(response({events: []}));
    const api = new LibraryApi(fetcher);
    const controller = new AbortController();
    const request = api.request('graph', {}, undefined, undefined, controller.signal);
    const rejected = expect(request).rejects.toBeInstanceOf(StaleRequestError);
    controller.abort();
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true);
    await expect(api.request('activity')).resolves.toEqual({events: []});
    pending.resolve(response({nodes: [{id: 'stale'}]}));
    await rejected;
  });

  it('does not fetch when the caller has already cancelled', async () => {
    const fetcher = vi.fn<typeof fetch>();
    const controller = new AbortController(); controller.abort();
    await expect(new LibraryApi(fetcher).request('graph', {}, undefined, undefined, controller.signal)).rejects.toBeInstanceOf(StaleRequestError);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('preserves the browser receiver for the native default fetch', async () => {
    const native = globalThis.fetch;
    globalThis.fetch = function (this: unknown) {
      if (this !== globalThis) throw new TypeError('Illegal invocation');
      return Promise.resolve(response({ connected: true }));
    } as typeof fetch;
    try {
      await expect(new LibraryApi().request('overview')).resolves.toEqual({ connected: true });
    } finally { globalThis.fetch = native; }
  });

  it('lets the browser supply the multipart boundary without a JSON content type', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ document_id: 'fixture-document' }));
    const api = new LibraryApi(fetcher);
    const upload = new FormData();
    upload.set('file', new Blob(['A public source fixture.'], { type: 'text/plain' }), 'fixture.txt');
    upload.set('sensitive', 'false');

    await api.request('documents/upload', {}, upload);

    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe('/api/library/documents/upload');
    expect(options?.method).toBe('POST');
    expect(options?.body).toBe(upload);
    expect(new Headers(options?.headers).has('Content-Type')).toBe(false);
    expect((options?.body as FormData).get('sensitive')).toBe('false');
  });

  it('serializes mutation privacy opt-in as a boolean, never a string', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ pinned: true }));
    const api = new LibraryApi(fetcher);

    await api.request('memories/fixture-memory/pin', {}, { pinned: true, include_sensitive: false });

    const options = fetcher.mock.calls[0][1];
    expect(options?.method).toBe('POST');
    expect(new Headers(options?.headers).get('Content-Type')).toBe('application/json');
    expect(JSON.parse(options?.body as string)).toEqual({ pinned: true, include_sensitive: false });
  });
});
