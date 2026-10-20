import {useMemo, useRef, useState, type FormEvent} from 'react';
import {createPortal} from 'react-dom';
import {BoardConflictError, groupEditorChanges, isDerivedBoardGroup, type BoardGroupSettings, type VisualMetadata} from '../lib/boardApi';
import {errorMessage, isAbort} from '../lib/libraryApi';
import {Modal} from '../views/LibraryShared';
import {VisualPicker} from './VisualPicker';
export interface BoardRecordOption extends VisualMetadata {id: string; label: string; type?: string; sensitive?: boolean}
export interface BoardGroupOption extends BoardGroupSettings {id: string; label: string; count?: number; manual?: boolean; visualOnly?: boolean}
export type BoardEditorTarget = {type: 'group'; group?: BoardGroupOption} | {type: 'record'; record: BoardRecordOption; visual?: VisualMetadata};
export interface BoardEditorProps {
  target: BoardEditorTarget; records: BoardRecordOption[];
  close: () => void; save: (value: BoardGroupSettings) => Promise<void>; refresh: () => Promise<void>;
  reset?: () => Promise<void>; includeSensitive?: boolean;
}
export function BoardEditor({target, records, close, save, refresh, reset, includeSensitive = false}: BoardEditorProps) {
  const group = target.type === 'group' ? target.group : undefined;
  const visualOnly = !!group && (group.visualOnly || isDerivedBoardGroup(group.id));
  const initial = group || (target.type === 'record' ? target.visual : undefined) || {};
  const [title, setTitle] = useState(group?.title || group?.label || ''), [description, setDescription] = useState(group?.description || '');
  const [privateGroup, setPrivateGroup] = useState(group?.sensitive || false);
  const [kind, setKind] = useState<'group' | 'topic'>(group?.kind || 'group');
  const [visual, setVisual] = useState<VisualMetadata>({icon: initial.icon || '', image: initial.image || ''});
  const [members, setMembers] = useState(new Set(group?.record_ids || []));
  const [query, setQuery] = useState(''), [error, setError] = useState(''), [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false), [reading, setReading] = useState(false); const pending = useRef(false);
  const knownIds = useMemo(() => new Set(records.map(record => record.id)), [records]);
  const filtered = useMemo(() => records.filter(record => record.label.toLowerCase().includes(query.toLowerCase())), [records, query]);
  const privateMembers = records.some(record => record.sensitive && members.has(record.id));
  const missing = [...members].filter(id => !knownIds.has(id)).length;
  async function perform(action: () => Promise<void>) {
    if (pending.current || reading) return;
    pending.current = true; setBusy(true); setError(''); setConflict(false);
    try {await action(); close();}
    catch (error) {if (!isAbort(error)) {setError(errorMessage(error)); setConflict(error instanceof BoardConflictError);}}
    finally {pending.current = false; setBusy(false);}
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (target.type === 'group' && !title.trim()) {setError('Give this group a name.'); return;}
    const value: BoardGroupSettings = {icon: visual.icon || undefined, image: visual.image || undefined};
    if (target.type === 'group') Object.assign(value, {title: title.trim(), description: description.trim(), kind, record_ids: [...members], sensitive: privateGroup || privateMembers});
    void perform(() => save(groupEditorChanges(group?.id, value)));
  }
  return createPortal(<Modal title={target.type === 'record' ? 'Edit record visual' : group ? 'Edit group' : 'New group'} close={close} busy={busy || reading}>
    <form className="form-stack board-editor" onSubmit={submit} aria-busy={busy}>
      <fieldset disabled={busy} className="board-editor-fields">
        {target.type === 'group' ? <><div className={`form-columns ${visualOnly ? 'board-name-only' : ''}`}><label>Name<input autoFocus value={title} onChange={event => setTitle(event.target.value)} placeholder="Untitled group" maxLength={120} required/></label>{!visualOnly && <label>Type<select value={kind} onChange={event => setKind(event.target.value as 'group' | 'topic')}><option value="group">Group</option><option value="topic">Topic</option></select></label>}</div><label>Description<textarea rows={3} value={description} onChange={event => setDescription(event.target.value)} placeholder="Add a short note about this space" maxLength={2000}/></label></> : <p className="board-record-caption">{target.record.label}</p>}
        <VisualPicker value={visual} onChange={setVisual} disabled={busy} onBusyChange={setReading}/>
        {target.type === 'group' && !visualOnly && <div className="board-members"><div className="board-members-heading"><span>Records</span><small>{members.size} selected</small></div><input type="search" aria-label="Find records to include" placeholder="Find records…" value={query} onChange={event => setQuery(event.target.value)}/><div className="board-member-options" role="group" aria-label="Group records">{filtered.map(record => <label key={record.id} className="check-label"><input type="checkbox" checked={members.has(record.id)} onChange={event => setMembers(previous => {const next = new Set(previous); if (event.target.checked) next.add(record.id); else next.delete(record.id); return next;})}/><span>{record.label}</span><small>{record.type === 'document' ? 'File' : 'Memory'}{record.sensitive ? ' · Private' : ''}</small></label>)}{!filtered.length && <p className="field-note">{records.length ? 'No matching records.' : 'Add records to your library to include them here.'}</p>}</div>{missing > 0 && <p className="field-note">{missing} selected {missing === 1 ? 'record is' : 'records are'} outside the current view.</p>}</div>}
        {target.type === 'group' && !visualOnly && includeSensitive && <div><label className="check-label"><input type="checkbox" checked={privateGroup || privateMembers} disabled={busy || privateMembers} onChange={event => setPrivateGroup(event.target.checked)}/>Private group</label>{privateMembers && <p className="field-note">Groups with private records are visible only with private records enabled.</p>}</div>}
      </fieldset>
      {error && <p className="inline-error" role="alert">{error}</p>}
      {conflict && <button className="button small" type="button" disabled={busy || reading} onClick={() => void perform(refresh)}>Reload latest board</button>}
      <footer className="form-actions">{reset && <button type="button" className="text-button board-reset" disabled={busy || reading} onClick={() => void perform(reset)}>{target.type === 'record' ? 'Reset visual' : group?.manual ? 'Remove group' : 'Reset group'}</button>}<button type="button" className="button" disabled={busy || reading} onClick={close}>Cancel</button><button type="submit" className="button primary" disabled={busy || reading || conflict}>{busy ? 'Saving…' : group || target.type === 'record' ? 'Save changes' : 'Create group'}</button></footer>
    </form>
  </Modal>, document.body);
}
