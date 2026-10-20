import {useEffect, useId, useRef, useState} from 'react';
import {boardApi, BoardConflictError, type BoardSettings, type BoardLayout, type BoardGrouping, type BoardGroupSettings} from '../lib/boardApi';
import {errorMessage, isAbort, StaleRequestError} from '../lib/libraryApi';
import {Icon} from '../views/LibraryShared';
import {BoardEditor, type BoardEditorTarget, type BoardGroupOption, type BoardRecordOption} from './BoardEditor';
import {GroupAliases} from './GroupAliases';
import './BoardControls.css';
export interface BoardControlsProps {
  compact?: boolean; settings: BoardSettings | null; groups: BoardGroupOption[]; records: BoardRecordOption[];
  onChange: (settings: BoardSettings) => void; includeSensitive?: boolean;
  /** Optional graph-driven selection opens an editor, without owning map state. */
  editTarget?: BoardEditorTarget | null; onEditClose?: () => void; onEditorOpenChange?: (open: boolean) => void;
}
export function BoardControls({compact = false, settings, groups, records, onChange, includeSensitive = false, editTarget, onEditClose, onEditorOpenChange}: BoardControlsProps) {
  const toolsId = useId();
  const layoutDescriptionId = useId();
  const container = useRef<HTMLElement>(null);
  const [aliasesOpen,setAliasesOpen]=useState(false);
  const [expanded, setExpanded] = useState(false), [target, setTarget] = useState<BoardEditorTarget | null>(null);
  const [layout, setLayout] = useState<BoardLayout>(settings?.layout || 'board'), [grouping, setGrouping] = useState<BoardGrouping>(settings?.grouping || 'subjects');
  const layoutDescription = layout === 'constellation' ? 'A spatial network of groups and citations.' : layout === 'columns' ? 'Records in ordered columns.' : 'Groups in tidy panels.';
  const [follow, setFollow] = useState(settings?.follow_agent ?? true), [busy, setBusy] = useState(false), [error, setError] = useState(''), [status, setStatus] = useState('');
  const [conflict, setConflict] = useState(false); const pending = useRef(false), generation = useRef(0);
  const scope = useRef(includeSensitive);
  const editorRevision = useRef<number | null>(null), preferenceRevision = useRef(settings?.revision ?? 0);
  useEffect(() => {if (target) editorRevision.current = settings?.revision ?? null;}, [target]);
  useEffect(() => () => {generation.current++;}, []);
  useEffect(() => {onEditorOpenChange?.(!!target||aliasesOpen); return () => onEditorOpenChange?.(false);}, [target,aliasesOpen,onEditorOpenChange]);
  if (scope.current !== includeSensitive) {scope.current = includeSensitive; generation.current++;}
  useEffect(() => {setTarget(null); setError(''); setStatus(''); setConflict(false); setBusy(false); pending.current = false;}, [includeSensitive]);
  useEffect(() => {setTarget(editTarget || null);}, [editTarget]);
  const dirty = !!settings && (layout !== settings.layout || grouping !== settings.grouping || follow !== settings.follow_agent);
  const previous = useRef<BoardSettings | null>(settings);
  useEffect(() => {
    // Preserve a local draft while the agent updates membership/settings.
    const old = previous.current;
    if (!old || (layout === old.layout && grouping === old.grouping && follow === old.follow_agent)) {setLayout(settings?.layout || 'board'); setGrouping(settings?.grouping || 'subjects'); setFollow(settings?.follow_agent ?? true); preferenceRevision.current = settings?.revision ?? 0;}
    previous.current = settings;
  }, [settings]);
  function closeEditor() {setTarget(null); onEditClose?.();}
  async function refresh() {
    const ownGeneration = generation.current; const fresh = await boardApi.load(includeSensitive);
    if (generation.current !== ownGeneration) return;
    onChange(fresh); preferenceRevision.current = fresh.revision; setLayout(fresh.layout); setGrouping(fresh.grouping); setFollow(fresh.follow_agent); setError(''); setConflict(false); setStatus('Board refreshed.');
  }
  function beginPreferenceDraft() {if (!dirty && settings) preferenceRevision.current = settings.revision;}
  async function savePreferences() {
    if (!settings || pending.current) return;
    const ownGeneration = generation.current; pending.current = true; setBusy(true); setError(''); setStatus('');
    try {const next = await boardApi.patch(preferenceRevision.current, {layout, grouping, follow_agent: follow}, includeSensitive); if (ownGeneration === generation.current) {onChange(next); setConflict(false); setStatus('Board saved.');}}
    catch (error) {if (ownGeneration === generation.current && !isAbort(error)) {setError(errorMessage(error)); setConflict(error instanceof BoardConflictError);}}
    finally {if (ownGeneration === generation.current) {setBusy(false); pending.current = false;}}
  }
  async function saveEditor(value: BoardGroupSettings) {
    if (!settings || !target) return;
    const ownGeneration = generation.current;
    const revision = editorRevision.current ?? settings.revision;
    const next = target.type === 'record' ? await boardApi.patch(revision, {records: {[target.record.id]: {icon: value.icon || null, image: value.image || null}}}, includeSensitive)
      : target.group ? await boardApi.patch(revision, {groups: {[target.group.id]: {...value, icon: value.icon || null, image: value.image || null}}}, includeSensitive)
      : await boardApi.createGroup(revision, {...value, icon: value.icon || undefined, image: value.image || undefined, title: value.title!}, includeSensitive);
    if (ownGeneration !== generation.current) throw new StaleRequestError();
    if (ownGeneration === generation.current) {onChange(next); setStatus(target.type === 'record' ? 'Record visual saved.' : target.group ? 'Group saved.' : 'Group created.');}
  }
  async function resetEditor() {
    if (!settings || !target) return;
    const ownGeneration = generation.current;
    const revision = editorRevision.current ?? settings.revision;
    const next = target.type === 'record' ? await boardApi.patch(revision, {records: {[target.record.id]: null}}, includeSensitive)
      : target.group ? await boardApi.patch(revision, {groups: {[target.group.id]: null}}, includeSensitive) : settings;
    if (ownGeneration !== generation.current) throw new StaleRequestError();
    if (ownGeneration === generation.current) {onChange(next); setStatus('Customization removed.');}
  }
  useEffect(() => {
    if (!expanded || !compact || target || aliasesOpen) return;
    const dismiss = (event: PointerEvent) => {if (!container.current?.contains(event.target as Node)) setExpanded(false);};
    document.addEventListener('pointerdown', dismiss);
    return () => document.removeEventListener('pointerdown', dismiss);
  }, [expanded,compact,target,aliasesOpen]);
  const mergedGroup = (group: BoardGroupOption) => ({...group, ...settings?.groups[group.id]});
  return <section ref={container} onKeyDown={event => {if (event.key === 'Escape' && expanded && !(event.target as Element).closest('[role="dialog"]')) {event.stopPropagation(); setExpanded(false); container.current?.querySelector<HTMLButtonElement>('.board-customize')?.focus();}}} className={`board-controls ${compact ? 'board-controls-compact' : ''} ${expanded ? 'is-expanded' : ''}`} aria-label="Board customization">
    <div className="board-controls-bar"><button type="button" className="button small board-customize" aria-expanded={expanded} aria-controls={toolsId} onClick={() => setExpanded(value => !value)}><Icon name="sliders" size={14}/><span>Customize</span><Icon name="chevron" size={12}/></button><button type="button" className="button small" disabled={!settings || busy} onClick={() => setTarget({type: 'group'})}><Icon name="plus" size={14}/><span>New group</span></button>{settings && !compact && <span className="board-controls-summary">{settings.layout === 'constellation' ? 'Constellation' : settings.layout === 'columns' ? 'Columns' : 'Board'}<span>·</span>{settings.follow_agent ? 'Following agent' : 'Follow paused'}</span>}<span className="board-save-status" role="status">{status}</span></div>
    {expanded && <div id={toolsId} className="board-tools"><div className="board-preferences"><label>Layout<select aria-describedby={layoutDescriptionId} value={layout} disabled={!settings || busy} onChange={event => {beginPreferenceDraft(); setLayout(event.target.value as BoardLayout); setStatus('');}}><option value="board">Board</option><option value="constellation">Constellation</option><option value="columns">Columns</option></select></label><label>Group by<select value={grouping} disabled={!settings || busy} onChange={event => {beginPreferenceDraft(); setGrouping(event.target.value as BoardGrouping); setStatus('');}}><option value="subjects">Subjects</option><option value="kind">Record kind</option><option value="source">Source</option></select></label><label className="board-follow"><input type="checkbox" checked={follow} disabled={!settings || busy} onChange={event => {beginPreferenceDraft(); setFollow(event.target.checked); setStatus('');}}/>Follow agent activity</label><button type="button" className="button primary small" disabled={!dirty || busy || conflict} onClick={() => void savePreferences()}>{busy ? 'Saving…' : 'Save board'}</button>{dirty && <button type="button" className="text-button" disabled={busy} onClick={() => {preferenceRevision.current = settings!.revision; setLayout(settings!.layout); setGrouping(settings!.grouping); setFollow(settings!.follow_agent); setError(''); setConflict(false);}}>Undo</button>}<p id={layoutDescriptionId} className="board-layout-description">{layoutDescription}</p></div><div className="board-edit-selectors"><button className="button small" disabled={!settings||busy||grouping!=='subjects'} onClick={()=>setAliasesOpen(true)}>Combine groups</button><label>Edit group<select value="" disabled={!settings || busy || !groups.length} onChange={event => {const group = groups.find(group => group.id === event.target.value); if (group) setTarget({type: 'group', group: mergedGroup(group)});}}><option value="">Choose a group…</option>{groups.map(group => <option key={group.id} value={group.id}>{settings?.groups[group.id]?.title || group.label}</option>)}</select></label><label>Edit record visual<select value="" disabled={!settings || busy || !records.length} onChange={event => {const record = records.find(record => record.id === event.target.value); if (record) setTarget({type: 'record', record, visual: {icon: record.icon, image: record.image, ...settings?.records[record.id]}});}}><option value="">Choose a record…</option>{records.map(record => <option key={record.id} value={record.id}>{record.label}</option>)}</select></label></div></div>}
    {error && <div className="board-control-error" role="alert"><span>{error}</span>{conflict && <button type="button" className="button small" disabled={busy} onClick={() => void refresh().catch(error => setError(errorMessage(error)))}>Reload latest board</button>}</div>}
    {aliasesOpen&&settings&&<GroupAliases settings={settings} groups={groups} includeSensitive={includeSensitive} onChange={onChange} close={()=>setAliasesOpen(false)}/>}
    {target && <BoardEditor key={`${includeSensitive}:${target.type}:${target.type === 'group' ? target.group?.id || 'new' : target.record.id}`} target={target} records={records} includeSensitive={includeSensitive} close={closeEditor} save={saveEditor} refresh={refresh} reset={target.type === 'record' || target.group ? resetEditor : undefined}/>}
  </section>;
}
