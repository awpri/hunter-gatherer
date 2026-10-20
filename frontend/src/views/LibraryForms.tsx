import { useLayoutEffect, useRef, useState, type FormEvent } from 'react';
import { libraryApi, errorMessage, isAbort } from '../lib/libraryApi';
import { Modal } from './LibraryShared';
import {VisualPicker} from '../components/VisualPicker';
import type {VisualMetadata} from '../lib/boardApi';
export const RECORD_KINDS = ['fact', 'preference', 'decision', 'plan', 'procedure', 'instruction', 'writing', 'source'];
export function LibraryForm({mode, close, complete}: {mode: 'memory' | 'file'; close: () => void; complete: (message: string) => void}) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const [sensitive, setSensitive] = useState(false);
  const [visual, setVisual] = useState<VisualMetadata>({}), [readingVisual, setReadingVisual] = useState(false);
  const [showVisual, setShowVisual] = useState(false);
  const mounted = useRef(true); const pending = useRef(false);
  useLayoutEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (pending.current || readingVisual) return; const data = new FormData(event.currentTarget); setError('');
    if (mode === 'file') {
      const file = data.get('file') as File;
      if (!file?.size) {setError('Choose a nonempty file to archive.'); return;}
      if (file.size > 25 * 1024 * 1024) {setError('Choose a file smaller than 25 MiB.'); return;}
    }
    pending.current = true;setBusy(true);let importMessage='';
    try {
      if (mode === 'file') { data.set('sensitive', String(sensitive));const result=await libraryApi.request<{sensitive?:boolean;worker_error?:string}>('imports',{},data);if(result.worker_error==='sensitive_opt_in_required')importMessage='This source requires private access. Include private records, then open Imports to continue its job.'; }
      else {
        const result = await libraryApi.request<{stored: boolean; decision?: string}>('memories', {}, {
        text: String(data.get('text')).trim(), kind: data.get('kind'), importance: Number(data.get('importance')),
        tags: String(data.get('tags') || '').split(',').map(tag => tag.trim()).filter(Boolean),
        icon: visual.icon || undefined, image: visual.image || undefined,
        category: String(data.get('category') || '').trim(), permanent: data.get('permanent') === 'on', sensitive,
        });
        if (!mounted.current) return;
        if (!result.stored) {
          setError(result.decision === 'duplicate' ? 'This memory is already in the library.' : `No new memory was saved${result.decision ? ` (${result.decision.replaceAll('_', ' ')})` : ''}.`);
          return;
        }
      }
      if (!mounted.current) return;
      complete(importMessage||(mode==='file'?(sensitive?'Private import queued. Include private records to view its progress.':'Import queued. Track extraction and indexing in Imports.'):sensitive?'Private record saved. Turn on “Include private records” to view it.':'Memory added to your library.'));
    } catch (error) { if (mounted.current && !isAbort(error)) setError(errorMessage(error)); }
    finally {pending.current = false; if (mounted.current) setBusy(false);}
  }
  return <Modal title={mode === 'file' ? 'Import file' : 'Add memory'} close={close} busy={busy || readingVisual}><form onSubmit={submit} className="form-stack" aria-busy={busy}><fieldset disabled={busy} className="form-stack" style={{border: 0, padding: 0, margin: 0, minWidth: 0}}>
    <p className="form-intro">{mode === 'file' ? 'Originals and searchable text are kept together.' : 'Save a fact, preference, idea, or plan.'}</p>
    {mode === 'file' ? <><label>Source file<input name="file" type="file" accept=".txt,.md,.pdf,.docx,.pptx" required/><span className="field-note">PDF, DOCX, PPTX, Markdown, TXT · up to 25 MiB</span></label><label>Title<input name="title" placeholder="Optional — defaults to the file name"/></label><div className="form-columns"><label>Author<input name="author"/></label><label>Source date<input name="document_date" type="date"/></label></div><label>Category<input name="category" placeholder="Optional — used for automatic grouping"/></label></> : <><label>Memory<textarea name="text" rows={7} placeholder="What would you like to remember?" required maxLength={50000}/></label><div className="form-columns"><label>Kind<select name="kind" defaultValue="fact">{RECORD_KINDS.map(kind => <option key={kind} value={kind}>{kind.replaceAll('_', ' ')}</option>)}</select></label><label>Importance (1–10)<input name="importance" type="number" min="1" max="10" step="1" defaultValue="5" required/></label></div><label>Category<input name="category" placeholder="Optional — used for automatic grouping"/></label><label>Tags<input name="tags" placeholder="Separate tags with commas"/></label><label className="check-label"><input type="checkbox" name="permanent" defaultChecked/>Keep permanently</label></>}
    <>{mode === 'memory' && <div className="memory-visual-composer"><button type="button" className="text-button" aria-expanded={showVisual} aria-controls="memory-visual-picker" disabled={busy || readingVisual} onClick={() => setShowVisual(value => !value)}>{showVisual ? 'Hide visual options' : 'Add an icon or image'}</button>{showVisual && <div id="memory-visual-picker"><VisualPicker value={visual} onChange={setVisual} disabled={busy} onBusyChange={setReadingVisual}/></div>}</div>}</>
    <label className="check-label"><input type="checkbox" checked={sensitive} onChange={e => setSensitive(e.target.checked)}/>Mark this record private</label>
    {error && <p className="inline-error" role="alert">{error}</p>}
    <footer className="form-actions"><button type="button" className="button" disabled={busy || readingVisual} onClick={close}>Cancel</button><button type="submit" className="button primary" disabled={busy || readingVisual}>{busy ? 'Saving…' : mode === 'file' ? 'Import file' : 'Add memory'}</button></footer>
  </fieldset></form></Modal>;
}
