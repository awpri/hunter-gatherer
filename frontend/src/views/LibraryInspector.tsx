import type {VisualRegion} from '../lib/visualRegions';
import type {OriginalFileReference} from '../lib/originalFiles';
import type {OriginalPageReference} from '../lib/pageImages';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { libraryApi, dateLabel, errorMessage, isAbort, kindOf, refsOf, type LibraryDocument, type LibraryMemory, type EvidenceRef } from '../lib/libraryApi';
import { ErrorNotice, Icon } from './LibraryShared';
import { FileTypeIcon } from '../components/FileTypeIcon';
import { fileType } from '../lib/fileTypes';
import { SourceText } from './SourceText';
export type Selection = {region?:VisualRegion;type: 'memory' | 'document'; id: string; page?: number; chunk?: string; start?:number; scroll?:number; originalPage?:OriginalPageReference; originalFile?:OriginalFileReference};
export function LibraryInspector({selection, includePrivate, close, select, changed, revision = 0,back}: {selection: Selection | null; includePrivate: boolean; close: () => void; select: (selection: Selection) => void; changed: (message: string) => void; revision?: number;back?:()=>void}) {
  const [memory, setMemory] = useState<LibraryMemory | null>(null); const [doc, setDoc] = useState<LibraryDocument | null>(null);
  const [chunk, setChunk] = useState<{quote: string; context?: string; location?: {page?: number; section?: string}; citation?: string} | null>(null);
  const [error, setError] = useState(''); const [actionError, setActionError] = useState(''); const [busy, setBusy] = useState(false); const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false); const [text, setText] = useState(''); const [forgetting, setForgetting] = useState(false); const [start, setStart] = useState(0);
  const inspector = useRef<HTMLElement>(null); const editor = useRef<HTMLTextAreaElement>(null);
  const origin = useRef<HTMLElement | null>(null); const mounted = useRef(true);
  const selectionKey = JSON.stringify([selection?.type, selection?.id, selection?.page, selection?.chunk, includePrivate]);
  const identity = useRef({key: selectionKey, generation: 0});
  if (identity.current.key !== selectionKey) identity.current = {key: selectionKey, generation: identity.current.generation + 1};
  const pending = useRef(false); const loadedKey = useRef(''); const loadedRequest = useRef(''); const [loadedRevision, setLoadedRevision] = useState(revision);
  useLayoutEffect(() => {
    mounted.current = true;
    const panel = inspector.current; const previous = document.activeElement;
    if (previous instanceof HTMLElement && !panel?.contains(previous)) origin.current = previous;
    return () => {
      mounted.current = false;
      if (panel?.contains(document.activeElement) && origin.current?.isConnected) origin.current.focus();
    };
  }, []);
  useEffect(() => {
    const active = document.activeElement;
    if (active instanceof HTMLElement && !inspector.current?.contains(active)) origin.current = active;
    inspector.current?.focus({preventScroll: true});
  }, [selectionKey]);
  useEffect(() => {if (editing) editor.current?.focus();}, [editing]);
  useEffect(() => {setStart(0);}, [selection?.id, selection?.page, selection?.chunk]);
  useEffect(() => {
    const sameRecord = loadedKey.current === selectionKey;
    // Keep the user's correction draft intact while live activity refreshes.
    if (sameRecord && (editing || busy)) return;
    const requestKey = JSON.stringify([selectionKey, start, revision]);
    if (sameRecord && loadedRequest.current === requestKey) return;
    loadedKey.current = selectionKey;
    if (!sameRecord) {pending.current = false; setBusy(false); setActionError('');}
    const controller = new AbortController();
    let live = true; setMemory(null); setDoc(null); setChunk(null); setError(''); setEditing(false); setForgetting(false);
    if (!selection) return;
    setLoading(true);
    const query = {include_sensitive: includePrivate, page: selection.page, start, max_chars: 12000};
    (async () => {
      try {
        if (selection.type === 'memory') {
          const result = await libraryApi.request<LibraryMemory | {found: boolean; memory: LibraryMemory}>(`memories/${encodeURIComponent(selection.id)}`, query, undefined, undefined, controller.signal);
          if (live) {setMemory('memory' in result ? result.memory : result); requestAnimationFrame(() => {if (inspector.current) inspector.current.scrollTop = selection.scroll || 0;});}
        } else {
          const result = await libraryApi.request<LibraryDocument>(`documents/${encodeURIComponent(selection.id)}`, query, undefined, undefined, controller.signal);
          if (live) {setDoc(result);}
          if (selection.chunk) {
            const hit = await libraryApi.request<typeof chunk>(`chunks/${encodeURIComponent(selection.chunk)}`, {include_sensitive: includePrivate}, undefined, undefined, controller.signal);
            if (live) setChunk(hit);
          }
        }
        if (live) {loadedRequest.current = requestKey; setLoadedRevision(revision);}
      } catch (error) {if (live && !isAbort(error)) setError(errorMessage(error));}
      finally {if (live) setLoading(false);}
    })();
    return () => {live = false; controller.abort();};
  }, [selectionKey, includePrivate, start, revision, editing, busy]);
  async function action(path: string, body: Record<string, unknown>, message: string) {
    if (pending.current) return;
    const generation = identity.current.generation;
    const current = () => mounted.current && identity.current.generation === generation;
    pending.current = true; setBusy(true); setActionError('');
    try {await libraryApi.request(path, {}, {...body, include_sensitive: includePrivate}); if (current()) changed(message);}
    catch (error) {if (current() && !isAbort(error)) setActionError(errorMessage(error));}
    finally {if (current()) {pending.current = false; setBusy(false);}}
  }
  function closeInspector() {if (pending.current) return; if (origin.current?.isConnected) origin.current.focus(); close();}
  const citationButton = (ref: EvidenceRef, index: number) => <button key={ref.chunk_id || index} className="source-link" disabled={busy} onClick={() => select({type: 'document', id: ref.document_id, page: ref.location?.page || undefined, chunk: ref.chunk_id})}><span className="source-index">{String(index + 1).padStart(2, '0')}</span><span><strong>{ref.location?.section || (ref.location?.page ? `Page ${ref.location.page}` : 'Original passage')}</strong><span>{ref.quote?.slice(0, 125) || ref.citation || 'Open source context'}{ref.quote && ref.quote.length > 125 ? '…' : ''}</span></span><Icon name="arrow" size={16}/></button>;
  if (!selection) return null;
  return <aside ref={inspector} className="inspector" aria-label="Record details" aria-busy={busy || loading} tabIndex={-1}><header className="inspector-top"><div className="action-row">{back&&<button className="text-button" onClick={back}>← Back</button>}<span className="inspector-eyebrow">{selection.type === 'memory' ? 'Memory' : 'File'}</span></div><button className="icon-button" aria-label="Close details" disabled={busy} onClick={closeInspector}><Icon name="close"/></button></header>
    {busy && <p className="field-note" role="status">Saving changes…</p>}
    {editing && loadedRevision !== revision && <p className="field-note" role="status">The library changed. Your draft is kept; close the editor to refresh this record.</p>}
    {loading && <div className="loading-state" role="status">Opening record…</div>}{error && <ErrorNotice message={error}/>}
    {actionError && <p className="inline-error" role="alert">{actionError}</p>}
    {memory && <><div className="record-detail-heading"><span className={`kind-label kind-${kindOf(memory)}`}>{kindOf(memory).replaceAll('_', ' ')}</span>{memory.metadata?.core && <span className="chip">Pinned</span>}<p>{dateLabel(memory.created_at)} · {memory.citation}</p></div>
      <dl className="detail-meta"><div><dt>Verification</dt><dd>{(memory.verification || memory.metadata?.verification || 'unverified').replaceAll('_', ' ')}</dd></div><div><dt>Importance</dt><dd>{memory.importance} / 10</dd></div><div><dt>Retention</dt><dd>{memory.metadata?.retention === 'permanent' ? 'Permanent' : 'Standard'}</dd></div>{memory.metadata?.category && <div><dt>Category</dt><dd>{memory.metadata.category}</dd></div>}</dl>
      <div className="detail-section"><h3>Content</h3>{editing ? <><label className="sr-only" htmlFor="replace-memory">Corrected memory</label><textarea ref={editor} disabled={busy} maxLength={50000} id="replace-memory" rows={10} value={text} onChange={e => setText(e.target.value)}/><p className="field-note">A correction creates a replacement. Previous citations are not carried over.</p><div className="action-row"><button className="button small primary" disabled={busy || !text.trim()} onClick={() => action(`memories/${memory.id}/replace`, {text: text.trim()}, 'Memory corrected. The earlier record is superseded.')}>Save correction</button><button className="button small" disabled={busy} onClick={() => {setEditing(false); inspector.current?.focus({preventScroll: true});}}>Cancel</button></div></> : <p className="record-body">{memory.text}</p>}{memory.tags?.length > 0 && <div className="tag-list">{memory.tags.map(tag => <span className="chip" key={tag}>{tag}</span>)}</div>}</div>
      <div className="detail-section"><h3>Sources <span>{refsOf(memory).length}</span></h3>{refsOf(memory).length ? refsOf(memory).map(citationButton) : <p className="muted small-text">No linked sources.</p>}</div>
      <div className="detail-actions"><button className="button small" disabled={busy} onClick={() => action(`memories/${memory.id}/pin`, {pinned: !memory.metadata?.core}, memory.metadata?.core ? 'Memory unpinned.' : 'Memory pinned.')}>{memory.metadata?.core ? 'Unpin memory' : 'Pin memory'}</button><button className="button small" disabled={busy} onClick={() => {setText(memory.text); setEditing(true);}}>Correct wording</button><button className="text-button danger" disabled={busy} onClick={() => setForgetting(true)}>Forget memory</button></div>
      {forgetting && <div className="confirm-box"><p>Remove this memory from retrieval? Originals, audit history, and backups remain recoverable.</p><div className="action-row"><button className="button small danger-button" disabled={busy} onClick={() => action(`memories/${memory.id}/forget`, {}, 'Memory removed from retrieval.')}>Confirm forget</button><button className="button small" disabled={busy} onClick={() => setForgetting(false)}>Cancel</button></div></div>}
    </>}
    {doc && <><div className="record-detail-heading"><FileTypeIcon metadata={doc} size={32}/><span className="kind-label" style={{marginLeft:10}}>{fileType(doc).extension || 'File'}</span><h2>{doc.title}</h2><p>{doc.author || 'Author unspecified'} · Version {doc.version}</p></div><dl className="detail-meta"><div><dt>Source date</dt><dd>{doc.document_date || 'Unspecified'}</dd></div><div><dt>Archived</dt><dd>{dateLabel(doc.created_at)}</dd></div><div><dt>Extraction</dt><dd>{doc.extraction_status?.replaceAll('_', ' ') || 'Unknown'}</dd></div>{doc.category && <div><dt>Category</dt><dd>{doc.category}</dd></div>}</dl>
      {!!doc.warnings?.length && <details className="warning-box"><summary>{doc.warnings.length} import {doc.warnings.length === 1 ? 'note' : 'notes'}</summary>{doc.warnings.map((warning, i) => <p key={i}>{warning}</p>)}</details>}
      {chunk && <div className="detail-section citation-excerpt"><h3>Cited passage {chunk.location?.section ? `· ${chunk.location.section}` : chunk.location?.page ? `· Page ${chunk.location.page}` : ''}</h3><blockquote>{chunk.quote}</blockquote><code>{chunk.citation}</code></div>}
      <div className="detail-section document-text"><h3>{selection.page ? `Source · ${fileType(doc).id === 'slides' ? 'Slide' : 'Page'} ${selection.page}` : 'Source text'}</h3><SourceText doc={doc} select={select} disabled={busy}/>
      <div className="action-row">{start > 0 && <button className="button small" disabled={busy} onClick={() => setStart(Math.max(0, start - 12000))}>Previous text</button>}{doc.next_start != null && <button className="button small" disabled={busy} onClick={() => setStart(doc.next_start!)}>Next text</button>}{selection.page && <button className="text-button" disabled={busy} onClick={() => select({type: 'document', id: selection.id})}>Read whole file</button>}</div></div>
      <div className="detail-section"><h3>Extract memories</h3><p className="muted small-text">Review excerpts before saving them.</p><button className="button small" disabled={busy} onClick={() => action(`documents/${doc.document_id}/propose`, {max_candidates: 20, offset: 0}, 'Excerpts proposed. Open Review to inspect and approve them.')}>{busy ? 'Proposing…' : 'Propose excerpts'}</button></div>
    </>}
  </aside>;
}
