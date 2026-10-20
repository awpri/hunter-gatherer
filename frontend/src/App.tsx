import {VisualRegionReader} from './views/VisualRegionReader';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { ConnectionMap, type LibraryGraph, type GraphNode } from './components/ConnectionMap';
import { HunterGathererMark } from './components/HunterGathererMark';
import {readWorkspaceView,saveWorkspaceView,recordSearchTab,type WorkspaceView,type LibraryTab} from './lib/workspaceView';
import { organizeGraph } from './components/graphLayout';
import type {BoardGroupOption} from './components/BoardEditor';
import { BoardControls } from './components/BoardControls';
import type { BoardEditorTarget } from './components/BoardEditor';
import { boardApi, BoardConflictError, type BoardSettings } from './lib/boardApi';
import { useAgentFollow } from './lib/useAgentFollow';
import { LibraryGroupsNav } from './components/LibraryGroupsNav';
import { FileTypeIcon } from './components/FileTypeIcon';
import { useLibraryActivity } from './lib/useLibraryActivity';
import { useAppearance } from './lib/appearance';
import { AppearancePanel } from './components/AppearancePanel';
import { UnifiedSearch, type SearchCommand, type SearchMode } from './components/UnifiedSearch';
import { libraryApi, dateLabel, errorMessage, isAbort, kindOf, refsOf, type LibraryMemory, type LibraryDocument, type Overview, type Page, type LibraryGroups, type HealthReport } from './lib/libraryApi';
import { Icon, EmptyState, ErrorNotice } from './views/LibraryShared';
import { LibraryForm } from './views/LibraryForms';
import { LibraryInspector, type Selection } from './views/LibraryInspector';
import { LibraryReview } from './views/LibraryReview';
import { LibraryImports } from './views/LibraryImports';
import { SourceReader } from './views/SourceReader';
import {NavigationPositions} from './lib/navigationPosition';
import { EvidenceWorkspace,type OriginalPageSelection } from './views/EvidenceWorkspace';
import { LibraryHealth } from './views/LibraryHealth';
import { LibraryLoading } from './components/LibraryLoading';
import './components/LibraryPolish.css';
import './components/WorkspaceCompact.css';
import {healthLabel, readHealthSummary, saveHealthSummary, summarizeHealth, type HealthSummary} from './lib/healthStatus';

type View = WorkspaceView;
const NAV: {id: View; label: string; icon: string}[] = [
  {id:'evidence', label:'Ask', icon:'knowledge'},
  {id:'connections', label:'Graph', icon:'connections'}, {id:'library', label:'Library', icon:'library'},
  {id:'review', label:'Review', icon:'review'}, {id:'health', label:'Health', icon:'health'}, {id:'connection', label:'Connection', icon:'settings'},
];
const COLORS = ['#547bb5','#4c9b86','#a879b8','#c28a4c','#cd6e73','#5c9cb8','#8a9860','#8b7eb5'];

export default function App() {
  const [includePrivate, setIncludePrivate] = useState(false);
  function privacyChange(value: boolean) {libraryApi.invalidate(); boardApi.invalidate(); setIncludePrivate(value);}
  return <LibraryWorkspace key={String(includePrivate)} includePrivate={includePrivate} privacyChange={privacyChange}/>;
}
function LibraryWorkspace({includePrivate, privacyChange}: {includePrivate: boolean; privacyChange: (value: boolean) => void}) {
  const [view, setView] = useState<View>(readWorkspaceView);
  const [healthSummary, setHealthSummary] = useState<HealthSummary | null>(null);
  const [connectionState, setConnectionState] = useState<'connecting' | 'connected' | 'unavailable'>('connecting');
  const [overview, setOverview] = useState<Overview | null>(null);
  const [groups, setGroups] = useState<LibraryGroups | null>(null);
  const [board, setBoard] = useState<BoardSettings | null>(null);
  const [boardEditorOpen,setBoardEditorOpen]=useState(false);
  const [boardTarget, setBoardTarget] = useState<BoardEditorTarget | null>(null);
  const boardRef = useRef(board); boardRef.current = board;
  const resizing = useRef(false);
  const [tab, setTab] = useState<LibraryTab>('memories');
  const [query, setQuery] = useState(''); const [search, setSearch] = useState('');
  const [askQuery,setAskQuery]=useState(''); const [commandQuery,setCommandQuery]=useState('');
  const [searchMode,setSearchMode]=useState<SearchMode>(()=>readWorkspaceView()==='evidence'?'ask':'records');
  const searchQuery=searchMode==='ask'?askQuery:searchMode==='commands'?commandQuery:query;
  function changeSearchQuery(text:string){if(searchMode==='ask')setAskQuery(text);else if(searchMode==='commands')setCommandQuery(text);else{setQuery(text);if(!text){setSearch('');setOffset(0);}}}
  const [kind, setKind] = useState(''); const [group, setGroup] = useState(''); const [pinned, setPinned] = useState(false);
  const [offset, setOffset] = useState(0); const [page, setPage] = useState<Page<LibraryMemory> | Page<LibraryDocument> | null>(null);
  const [graph, setGraph] = useState<LibraryGraph | null>(null); const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [reviewSource,setReviewSource]=useState('');
  const [evidenceSeed,setEvidenceSeed] = useState({query:'',token:0});
  const [selectedOriginalPage,setSelectedOriginalPage]=useState<OriginalPageSelection|null>(null);
  const [collectionBusy,setCollectionBusy]=useState(false);
  function selectOriginalPage(reference:import('./lib/pageImages').OriginalPageReference,doc:LibraryDocument){
    if(collectionBusy)return;setSelectedOriginalPage(previous=>({reference:{...reference},title:doc.title,token:(previous?.token||0)+1}));
    setView('library');setTab('collections');setSelection(null);setHistory([]);setSelectedNode(null);
    requestAnimationFrame(()=>document.querySelector<HTMLInputElement>('.collection-draft input')?.focus());
  }
  const [history,setHistory] = useState<Selection[]>([]);
  function openSelection(next:Selection){setHistory(previous=>selection?[...previous,{...selection,scroll:(document.querySelector('.inspector') as HTMLElement|null)?.scrollTop||0}].slice(-30):previous);setSelection(next);}
  function goBack(){const previous=history[history.length-1];if(previous){setHistory(history.slice(0,-1));setSelection(previous);}}
  function gatherEvidence(text:string){setAskQuery(text);setSearchMode('ask');setEvidenceSeed(previous=>({query:text,token:previous.token+1}));setSelection(null);setHistory([]);setSelectedNode(null);setView('evidence');}
  const [selection, setSelection] = useState<Selection | null>(null); const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(false); const [error, setError] = useState(''); const [overviewError, setOverviewError] = useState('');
  const [form, setForm] = useState<'memory' | 'file' | null>(null); const [notice, setNotice] = useState('');
  const [appearanceOpen, setAppearanceOpen] = useState(false);
  const [recordLayout, setRecordLayout] = useState<'rows' | 'cards'>(() => {
    try { return localStorage.getItem('proharness.library.layout') === 'cards' ? 'cards' : 'rows'; } catch { return 'rows'; }
  });
  const {appearance, setAppearance} = useAppearance();
  const closeAppearance = useCallback(() => setAppearanceOpen(false), []);
  const searchInput = useRef<HTMLInputElement>(null);
  const lastBrowse = useRef('');
  const positions=useRef(new NavigationPositions());
  const contentRef=useRef<HTMLElement>(null);
  const refreshActivity = useCallback(() => setRevision(value => value + 1), []);
  const {activity, status: activityStatus, connected: activityConnected} = useLibraryActivity(includePrivate, refreshActivity);
  const {focusRequest,onUserNavigate} = useAgentFollow(activity.focus,view==='connections'&&!!board&&board.follow_agent&&!form&&!appearanceOpen&&!boardEditorOpen,(graph?.nodes||[]).map(node=>node.id));
  const cameraKey=JSON.stringify([search,kind,group,pinned,includePrivate,board?.layout,board?.grouping,board?.groups]);
  const scrollKey=JSON.stringify([view,tab,search,kind,group,pinned,offset,recordLayout]);
  useLayoutEffect(()=>{if(contentRef.current&&!loading)contentRef.current.scrollTop=positions.current.scroll(scrollKey);},[scrollKey,loading,page]);
  const browseKey = JSON.stringify([view, tab, search, kind, group, pinned, offset, includePrivate]);
  const connectionTone = connectionState === 'unavailable' ? 'red' : activityConnected && connectionState === 'connected' ? 'green' : 'gray';
  const connectionLabel = connectionState === 'unavailable' ? 'Connection: library API unavailable' : connectionTone === 'green' ? 'Connection: library API and live activity connected' : connectionState === 'connected' ? 'Connection: library API connected; live activity reconnecting' : 'Connection: checking library API';
  useEffect(() => {setHealthSummary(overview?.store.id ? readHealthSummary(overview.store.id) : null);}, [overview?.store.id]);
  function healthScanned(report: HealthReport) {const summary = summarizeHealth(report, includePrivate); setHealthSummary(summary); if (overview?.store.id) saveHealthSummary(overview.store.id, summary);}
  const browsing = (view === 'library' && tab !== 'collections' && tab !== 'imports') || view === 'connections';
  useEffect(()=>saveWorkspaceView(view),[view]);
  useEffect(() => {try {localStorage.setItem('proharness.library.layout', recordLayout);} catch { /* Layout remains usable without storage. */ }}, [recordLayout]);

  useEffect(() => {const timeout = setTimeout(() => {setSearch(query.trim()); setOffset(0);}, 220); return () => clearTimeout(timeout);}, [query]);
  useEffect(() => {
    let live = true; setOverviewError(''); const controller = new AbortController();
    Promise.all([
      libraryApi.request<Overview>('overview', {include_sensitive: includePrivate}, undefined, undefined, controller.signal).then(info => {if (live) {setOverview(info); setConnectionState('connected');} return info;}).catch(error => {if (live && !isAbort(error)) setConnectionState('unavailable'); throw error;}),
      libraryApi.request<LibraryGroups>('groups', {include_sensitive: includePrivate}, undefined, undefined, controller.signal),
      boardApi.load(includePrivate,controller.signal),
    ]).then(([info, nextGroups, nextBoard]) => {if (live) {setOverview(info); setGroups(nextGroups);setBoard(nextBoard);}})
      .catch(error => {if (live && !isAbort(error)) setOverviewError(errorMessage(error));});
    return () => {live = false; controller.abort();};
  }, [includePrivate, revision]);
  useEffect(() => {
    if (!browsing) {setLoading(false);return;}
    let live = true; setError(''); const controller = new AbortController();
    const resetResults = lastBrowse.current !== browseKey;
    lastBrowse.current = browseKey;
    setLoading(resetResults);
    if (resetResults) {setPage(null); setGraph(null);}
    const params = {mode: 'grouped', query: search, kind, group, pinned: pinned || undefined, limit: view === 'connections' ? 150 : 30, offset, include_sensitive: includePrivate};
    (view === 'connections' ? libraryApi.request<LibraryGraph>('graph', params, undefined, undefined, controller.signal) : libraryApi.request<Page<LibraryMemory> | Page<LibraryDocument>>(tab === 'memories' ? 'memories' : 'documents', params, undefined, undefined, controller.signal))
      .then(data => {if (!live) return; if (view === 'connections') setGraph(data as LibraryGraph); else setPage(data as Page<LibraryMemory> | Page<LibraryDocument>);})
      .catch(error => {if (live && !isAbort(error)) setError(errorMessage(error));}).finally(() => {if (live) setLoading(false);});
    return () => {live = false; controller.abort();};
  }, [view, browsing, tab, search, kind, group, pinned, offset, includePrivate, revision, browseKey]);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        if (form || appearanceOpen || document.querySelector('[role="dialog"]')) return;
        event.preventDefault(); searchInput.current?.focus(); searchInput.current?.select();
      }
      if (event.key === '/' && !event.metaKey && !event.ctrlKey && !event.altKey && !form && !appearanceOpen && browsing && !document.querySelector('[role="dialog"]') && !(event.target instanceof HTMLElement && (event.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)))) {
        event.preventDefault(); searchInput.current?.focus();
      }
    };
    window.addEventListener('keydown', handler); return () => window.removeEventListener('keydown', handler);
  }, [form, appearanceOpen, browsing]);
  useEffect(() => {
    const refresh = () => {if (!document.hidden) setRevision(value => value + 1);};
    document.addEventListener('visibilitychange', refresh);
    return () => document.removeEventListener('visibilitychange', refresh);
  }, []);
  useEffect(() => {if (!notice) return; const timer = setTimeout(() => setNotice(''), 6000); return () => clearTimeout(timer);}, [notice]);
  const closeForm = useCallback(() => setForm(null), []);
  const changed = useCallback((message: string) => {setNotice(message); setOffset(0); setRevision(v => v+1); setForm(null);setSelection(null);setHistory([]);setSelectedNode(null);}, []);
  function navigate(next: View) {
    if(next==='evidence')setSearchMode('ask');else if(next==='connections'||next==='library')setSearchMode('records');
    if(next==='review')setReviewSource('');
    if (next === 'connections') {setKind(''); setPinned(false);}
    const continuing=(view==='connections'||view==='library')&&(next==='connections'||next==='library')&&tab!=='collections'&&tab!=='imports';setView(next);if(!continuing){setSelection(null);setHistory([]);setSelectedNode(null);}
  }
  function chooseGroup(id: string) {setGroup(id); setOffset(0); setSelection(null); setSelectedNode(null); if (!browsing) navigate('connections');}
  function chooseNode(node: GraphNode) {setSelectedNode(node); if(node.type !== 'topic')openSelection({type: node.type, id: node.record_id});else setSelection(null);}
  function boardChanged(next:BoardSettings) {setBoard(next);setRevision(value=>value+1);}
  const editableBoardGroups = useMemo<BoardGroupOption[]>(() => {
    if (!graph || !board || board.grouping === 'subjects') return groups?.groups || [];
    const nodes = graph.nodes.filter(node => !graph.groups?.length || node.type !== 'topic');
    return organizeGraph(nodes, graph.edges, graph.groups || [], board.grouping).groups.map(item => ({
      id: item.id, label: item.label || 'Group', record_ids: item.record_ids, visualOnly: true, ...board.groups[item.id],
    }));
  }, [graph, board, groups]);
  function editGroup(id:string) {const item=editableBoardGroups.find(group=>group.id===id);if(item)setBoardTarget({type:'group',group:{...item,...board?.groups[id]}});}
  function editRecord(id:string) {const item=graph?.nodes.find(node=>node.id===id);if(item)setBoardTarget({type:'record',record:item,visual:board?.records[id]||{icon:item.icon,image:item.image}});}
  async function resizeGroup(id:string,size:{width:number;height:number}) {
    if (!boardRef.current) {const message = 'The board is still loading.'; setNotice(message); throw new Error(message);}
    if (resizing.current) {const message = 'A group resize is still being saved.'; setNotice(message); throw new Error(message);}
    resizing.current=true;
    try {boardChanged(await boardApi.patch(boardRef.current.revision,{groups:{[id]:{width:Math.round(size.width),height:Math.round(size.height)}}},includePrivate));}
    catch(error){if(!isAbort(error)){setNotice(errorMessage(error));if(error instanceof BoardConflictError&&error.latest)setBoard(error.latest);}throw error;}
    finally{resizing.current=false;}
  }
  function reset() {setKind(''); setGroup(''); setPinned(false); setQuery(''); setOffset(0); setSelection(null); setSelectedNode(null);}
  const memories = page?.memories as LibraryMemory[] | undefined;
  const documents = page?.documents as LibraryDocument[] | undefined;
  const activeGroup = groups?.groups.find(item => item.id === group);
  const hasInspector = !!selection || selectedNode?.type === 'topic';
  const title = view==='evidence'?'Ask library':NAV.find(item => item.id === view)?.label;
  const groupOf = (id?: string) => groups?.groups.find(item => item.id === id);
  const commands: SearchCommand[] = [
    {id:'gather-evidence',label:'Ask library',description:'Find quotations and inspect their sources',section:'Library',icon:<Icon name="knowledge"/>,keywords:['sources','requirements','research','gather','evidence'],onSelect:()=>navigate('evidence')},
    {id:'imports',label:'Import jobs',section:'Library',icon:<Icon name="upload"/>,keywords:['background','retry','resume'],onSelect:()=>{navigate('library');setTab('imports');}},
    {id:'collections',label:'Collections',section:'Library',icon:<Icon name="project"/>,keywords:['portfolio','application','export'],onSelect:()=>{navigate('library');setTab('collections');}},
    ...NAV.filter(item=>item.id!=='evidence').map(item => ({id: `view-${item.id}`, label: item.label, section: 'Navigate', icon: <Icon name={item.icon}/>, onSelect: () => navigate(item.id)})),
    {id:'files', label:'Files', section:'Navigate', icon:<Icon name="file"/>, keywords:['documents','pdf','docx','pptx'], onSelect:() => {reset(); navigate('library'); setTab('files');}},
    {id:'add-memory', label:'Add memory', section:'Create', icon:<Icon name="plus"/>, keywords:['new','note'], onSelect:() => setForm('memory')},
    {id:'import-file', label:'Import file', section:'Create', icon:<Icon name="upload"/>, keywords:['pdf','document','upload'], onSelect:() => setForm('file')},
    {id:'pinned', label:'Pinned memories', section:'Navigate', icon:<Icon name="pin"/>, onSelect:() => {reset(); navigate('library'); setTab('memories'); setPinned(true);}},
    {id:'appearance', label:'Customize appearance', section:'Appearance', icon:<Icon name="sliders"/>, keywords:['theme','color','motion','density','settings'], onSelect:() => setAppearanceOpen(true)},
    {id:'theme-dark', label:'Dark mode', section:'Appearance', icon:<Icon name="moon"/>, onSelect:() => setAppearance({theme:'dark'})},
    {id:'theme-light', label:'Light mode', section:'Appearance', icon:<Icon name="sun"/>, onSelect:() => setAppearance({theme:'light'})},
    {id:'layout-cards', label:'Card view', section:'Library', icon:<Icon name="grid"/>, keywords:['tiles','gallery'], onSelect:() => {navigate('library'); setRecordLayout('cards');}},
    {id:'layout-rows', label:'Row view', section:'Library', icon:<Icon name="list"/>, keywords:['list','compact'], onSelect:() => {navigate('library'); setRecordLayout('rows');}},
    {id:'refresh', label:'Refresh library', section:'Library', icon:<Icon name="refresh"/>, onSelect:refreshActivity},
    {id:'clear', label:'Clear filters', section:'Library', icon:<Icon name="close"/>, onSelect:reset},
    ...(groups?.groups || []).map(item => ({id:`group-${item.id}`, label:item.label, description:`${item.memory_count} memories · ${item.document_count} files`, section:'Groups', icon:<span className="group-dot" style={{background:COLORS[item.color_index % COLORS.length]}}/>, onSelect:() => {setQuery(''); setSearch(''); chooseGroup(item.id);}})),
  ];

  return <div className="library-shell compact-shell">
    <a className="skip-link" href="#main-content">Skip to content</a>
    <aside className="library-sidebar">
      <header className="library-brand">
        <button className={`brand-mark ${activityStatus?.origin === 'agent' ? 'agent-active' : ''}`} aria-label="hunter/gatherer home" title="hunter/gatherer" onClick={() => navigate('evidence')}><HunterGathererMark/></button>
        <div className="sidebar-tools"><button className="icon-button" aria-label="Customize appearance" title={`Appearance · ${appearance.theme}`} onClick={() => setAppearanceOpen(true)}><Icon name="sliders" size={17}/></button></div>
      </header>
      <nav className="workspace-navigation" aria-label="Main navigation">{NAV.map(item => <button key={item.id} aria-current={view === item.id ? 'page' : undefined} className={`navigation-item ${view === item.id ? 'active' : ''} ${['review','health','connection'].includes(item.id) ? 'utility-navigation' : ''}`} onClick={() => navigate(item.id)}><Icon name={item.icon} size={18}/><span>{item.label}</span>{item.id === 'review' && !!overview?.counts.pending && <small>{overview.counts.pending}</small>}{item.id === 'health' && <i className={`nav-status-dot tone-${healthSummary?.tone || 'gray'}`} role="img" aria-label={healthLabel(healthSummary)} title={healthLabel(healthSummary)}/>}{item.id === 'connection' && <i className={`nav-status-dot tone-${connectionTone}`} role="img" aria-label={connectionLabel} title={connectionLabel}/> }</button>)}</nav>
      <section className="groups-sidebar" aria-label="Library groups">
        <div className="group-top"><span>Groups</span><span className="auto-label" title="Grouped automatically by categories, tags, and current sources">Auto</span></div>
        <LibraryGroupsNav groups={groups} active={group} pinned={pinned} choose={id=>{setPinned(false);chooseGroup(id);}}/>
      </section>
      <div className="sidebar-bottom"><button className={`group-item ${pinned ? 'selected' : ''}`} onClick={() => {navigate('library'); setTab('memories'); setGroup(''); setPinned(!pinned);}}><Icon name="pin" size={15}/><span>Pinned memories</span></button><div className="sidebar-counts"><button onClick={() => {reset(); navigate('library'); setTab('memories');}}>{overview?.counts.memories ?? '—'} {overview?.counts.memories === 1 ? 'memory' : 'memories'}</button><button onClick={() => {reset(); navigate('library'); setTab('files');}}>{overview?.counts.documents ?? '—'} {overview?.counts.documents === 1 ? 'file' : 'files'}</button></div></div>
    </aside>
    <main id="main-content" className={`workspace workspace-${view}`}>
      <header className="workspace-top">
        <div className={`view-title ${view === 'connections' ? 'graph-title-hidden' : ''}`} key={view}><h1>{title}</h1>{browsing && activeGroup && <><span className="breadcrumb-slash">/</span><button className="active-group-name" title="Show all groups" onClick={() => chooseGroup('')}>{activeGroup.label}<Icon name="close" size={12}/></button></>}</div>
        <div className={`activity-status ${activityStatus ? 'is-active' : ''} ${activityStatus?.phase === 'error' ? 'has-error' : ''}`} role="status" aria-live="polite" aria-atomic="true" title={activityConnected ? 'Live activity from this hunter/gatherer service' : 'Reconnecting live activity'}><span className={`activity-dot ${activityConnected ? 'connected' : ''}`}/>{activityStatus && <span>{activityStatus.origin === 'agent' ? 'Agent · ' : ''}{activityStatus.label}</span>}</div>
        <UnifiedSearch inputRef={searchInput} mode={searchMode} onModeChange={setSearchMode} query={searchQuery} onQuery={changeSearchQuery} commands={commands} groupLabel={activeGroup?.label} onClearGroup={() => chooseGroup('')} onGather={gatherEvidence} onSearch={text => {setSearchMode('records');setQuery(text);setSearch(text.trim());setOffset(0);if (!browsing) {setTab(recordSearchTab(tab));navigate('library');}}}/>

        <div className="top-actions"><label className={`privacy-control ${includePrivate ? 'enabled' : ''}`} title="Include private records"><input type="checkbox" aria-label="Include private records" checked={includePrivate} onChange={e => privacyChange(e.target.checked)}/><Icon name={includePrivate ? 'unlock' : 'lock'} size={16}/><span>Private</span></label><button className="button" aria-label="Import file" title="Import file" onClick={() => setForm('file')}><Icon name="upload" size={15}/><span>Import</span></button><button className="button primary" aria-label="Add memory" title="Add memory" onClick={() => setForm('memory')}><Icon name="plus" size={16}/><span>Add memory</span></button></div>
      </header>
      {notice && <div className="notice" role="status"><span>{notice}</span><button className="icon-button" onClick={() => setNotice('')} aria-label="Dismiss notification"><Icon name="close" size={15}/></button></div>}
      {overviewError && <ErrorNotice message={overviewError} retry={() => setRevision(v => v+1)}/>}
      {view === 'library' && <div className="browse-toolbar">
        {view === 'library' ? <div className="segmented" role="group" aria-label="Record type"><button aria-pressed={tab === 'memories'} className={tab === 'memories' ? 'selected' : ''} onClick={() => {setTab('memories');setHistory([]);setOffset(0);setSelection(null);}}>Memories</button><button aria-pressed={tab === 'files'} className={tab === 'files' ? 'selected' : ''} onClick={() => {setTab('files');setHistory([]);setPinned(false); setKind(''); setOffset(0); setSelection(null);}}>Files</button><button aria-pressed={tab === 'collections'} className={tab === 'collections'?'selected':''} onClick={()=>{setTab('collections');setSelection(null);setHistory([]);}}>Collections</button><button aria-pressed={tab==='imports'} className={tab==='imports'?'selected':''} onClick={()=>{setTab('imports');setSelection(null);setHistory([]);}}>Imports</button></div> : <span className="toolbar-count"><Icon name="connections" size={15}/>{graph ? `${graph.nodes.filter(node => node.type !== 'topic').length} records` : loading ? 'Loading…' : 'Records'}</span>}
        {view === 'library' && tab === 'memories' && <select aria-label="Memory kind" value={kind} onChange={e => {setKind(e.target.value); setOffset(0); setSelection(null);}}><option value="">All kinds</option>{overview?.kinds.map(item => <option value={item.name} key={item.name}>{item.name.replaceAll('_',' ')}</option>)}</select>}
        {tab!=='collections'&&tab!=='imports'&&(query || kind || group || pinned) && <button className="text-button clear-filters" onClick={reset}>Clear filters</button>}
        {view === 'library' && tab === 'memories' && <button className={`toolbar-pin ${pinned ? 'selected' : ''}`} aria-pressed={pinned} title="Pinned memories" onClick={() => {setPinned(!pinned); setOffset(0);}}><Icon name="pin" size={14}/><span>Pinned</span></button>}
      </div>}
      <div className={`content-grid ${hasInspector ? 'has-selection' : 'no-inspector'}`}>
        <section ref={contentRef} onScroll={event=>{if(!loading)positions.current.saveScroll(scrollKey,event.currentTarget.scrollTop);}} className={`library-content view-${view}`} aria-label={`${title} content`}>
          {browsing && loading && <LibraryLoading graph={view === 'connections'} cards={recordLayout === 'cards'}/>}
          {browsing && error && <ErrorNotice message={error} retry={() => setRevision(v => v+1)}/>}
          {view === 'library' && tab !== 'collections' && tab !== 'imports' && <>
            {page && <div className="list-caption"><span>{page.total} {tab === 'memories' ? (page.total === 1 ? 'memory' : 'memories') : (page.total === 1 ? 'file' : 'files')}</span><div className="list-options"><span className="list-sort-label">Newest first</span><div className="record-layout-switch" role="group" aria-label="Library layout"><button title="Rows" aria-label="Row view" aria-pressed={recordLayout === 'rows'} onClick={() => setRecordLayout('rows')}><Icon name="list" size={15}/></button><button title="Cards" aria-label="Card view" aria-pressed={recordLayout === 'cards'} onClick={() => setRecordLayout('cards')}><Icon name="grid" size={15}/></button></div></div></div>}
            {page?.total === 0 && <EmptyState title="No records">{query || kind || group || pinned ? 'Try another search or clear filters.' : tab === 'memories' ? 'Add a memory to get started.' : 'Import a file to get started.'}</EmptyState>}
            <div className={`records-layout records-${recordLayout}`} key={`${tab}:${recordLayout}`}>
            {memories?.map((memory, index) => <button key={memory.id} aria-pressed={selection?.id === memory.id} style={{'--row-index':Math.min(index,10)} as CSSProperties} className={`record-row ${selection?.id === memory.id ? 'selected' : ''} ${activity.nodes[`memory:${memory.id}`] ? `live-${activity.nodes[`memory:${memory.id}`].kind}` : ''}`} onClick={() => openSelection({type:'memory',id:memory.id})}>
              {activity.nodes[`memory:${memory.id}`] && <span key={activity.nodes[`memory:${memory.id}`].token} className="row-activity" aria-hidden="true"/>}<span className="memory-icon"><Icon name="memory" size={20}/></span><span className="row-main"><span className="row-title">{memory.text}</span><span className="row-metadata"><span>{kindOf(memory).replaceAll('_',' ')}</span>{memory.metadata?.core && <Icon name="pin" size={12}/>}<span className="row-group">{groupOf(memory.group_id)?.label}</span>{!!refsOf(memory).length && <span>{refsOf(memory).length} sources</span>}{memory.metadata?.sensitive && <Icon name="lock" size={12}/>}</span></span><span className="row-date">{dateLabel(memory.created_at)}</span><Icon name="chevron" size={15}/>
            </button>)}
            {documents?.map((document, index) => <button key={document.document_id} aria-pressed={selection?.id === document.document_id} style={{'--row-index':Math.min(index,10)} as CSSProperties} className={`record-row document-row ${selection?.id === document.document_id ? 'selected' : ''} ${activity.nodes[`document:${document.document_id}`] ? `live-${activity.nodes[`document:${document.document_id}`].kind}` : ''}`} onClick={() => openSelection({type:'document',id:document.document_id})}>
              {activity.nodes[`document:${document.document_id}`] && <span key={activity.nodes[`document:${document.document_id}`].token} className="row-activity" aria-hidden="true"/>}<FileTypeIcon metadata={document} size={29}/><span className="row-main"><span className="row-title">{document.title}</span><span className="row-metadata"><span>{groupOf(document.group_id)?.label}</span>{document.author && <span>{document.author}</span>}{document.version > 1 && <span>v{document.version}</span>}{!!document.warnings?.length && <span className="warning-text">{document.warnings.length} {document.warnings.length === 1 ? 'note' : 'notes'}</span>}{document.sensitive && <Icon name="lock" size={12}/>}</span></span><span className="row-date">{dateLabel(document.created_at)}</span><Icon name="chevron" size={15}/>
            </button>)}
            </div>
            {page && (offset > 0 || page.next_offset != null) && <div className="pagination"><button className="button small" disabled={!offset} onClick={() => setOffset(Math.max(0, offset-30))}>Previous</button><span>{offset+1}–{offset+(memories?.length || documents?.length || 0)} of {page.total}</span><button className="button small" disabled={page.next_offset == null} onClick={() => setOffset(page.next_offset!)}>Next</button></div>}
          </>}
          {view === 'connections' && graph && <ConnectionMap initialCamera={positions.current.camera(cameraKey)} saveCamera={snapshot=>positions.current.saveCamera(cameraKey,snapshot)} toolbarContent={<BoardControls onEditorOpenChange={setBoardEditorOpen} compact settings={board} groups={editableBoardGroups} records={graph.nodes.filter(node=>node.type!=='topic')} includeSensitive={includePrivate} onChange={boardChanged} editTarget={boardTarget} onEditClose={()=>setBoardTarget(null)}/>} key={browseKey} activity={activity} graph={graph} boardSettings={board||undefined} focusRequest={focusRequest} onUserNavigate={onUserNavigate} onGroupResize={resizeGroup} onEditGroup={editGroup} onEditRecord={editRecord} selectedId={selectedNode?.id} onSelect={chooseNode} onGroupSelect={chooseGroup}/>}
          {view==='library'&&tab==='imports'&&<LibraryImports includePrivate={includePrivate} revision={revision} select={openSelection} notify={setNotice}/>}
          {/* One owner retains candidate sources and unsaved work across navigation. Privacy changes remount LibraryWorkspace. */}
          <div hidden={view !== 'evidence' && !(view === 'library' && tab === 'collections')}><EvidenceWorkspace active={view === 'evidence' || (view === 'library' && tab === 'collections')} mode={view==='library'&&tab==='collections'?'collections':'evidence'} seed={evidenceSeed} includePrivate={includePrivate} select={openSelection} notify={setNotice} selectedOriginalPage={selectedOriginalPage} onBusyChange={setCollectionBusy}/></div>
          {view === 'review' && <LibraryReview source={reviewSource} includePrivate={includePrivate} select={openSelection} changed={changed} revision={revision}/>}
          {view === 'health' && <LibraryHealth includePrivate={includePrivate} select={openSelection} onScan={healthScanned} lastScan={healthSummary}/>}
          {view === 'connection' && <div className="connection-view"><div className="connection-banner"><span className={`nav-status-dot tone-${connectionTone}`} role="img" aria-label={connectionLabel} title={connectionLabel}/><h2>{connectionState === 'connected' ? 'API connected' : connectionState === 'connecting' ? 'Connecting' : 'Unavailable'}</h2></div><dl className="service-details"><div><dt>Store</dt><dd>{overview?.store.id || '—'}</dd></div><div><dt>API</dt><dd><code>{window.location.origin}/api/library</code></dd></div><div><dt>MCP</dt><dd><code>{window.location.origin}/mcp</code></dd></div><div><dt>Database</dt><dd><code>{overview?.store.database || '—'}</code></dd></div></dl><button className="button" onClick={() => setRevision(v => v+1)}>Refresh</button></div>}
        </section>
        {hasInspector && (selectedNode?.type === 'topic' && !selection ? <aside className="inspector"><header className="inspector-top"><span>Topic</span><button className="icon-button" aria-label="Close topic" onClick={() => setSelectedNode(null)}><Icon name="close"/></button></header><div className="record-detail-heading"><h2>{selectedNode.label}</h2><button className="button small" onClick={() => {setQuery(selectedNode.label);setSearch(selectedNode.label);navigate('library');}}>Find records</button></div></aside> : selection?.region ? <VisualRegionReader region={selection.region} includePrivate={includePrivate} select={openSelection} back={history.length?goBack:undefined} close={()=>{setSelection(null);setHistory([]);setSelectedNode(null);}}/> : selection?.type === 'document' ? <SourceReader onSelectOriginalPage={selectOriginalPage} selectionDisabled={collectionBusy} review={()=>{setReviewSource(selection.id);setView('review');setHistory([]);setSelectedNode(null);}} selection={selection} includePrivate={includePrivate} select={openSelection} back={history.length?goBack:undefined} close={()=>{setSelection(null);setHistory([]);setSelectedNode(null);}} changed={message=>{setNotice(message);setRevision(value=>value+1);}}/> : <LibraryInspector back={history.length?goBack:undefined} revision={revision} selection={selection} includePrivate={includePrivate} close={() => {setSelection(null);setHistory([]);setSelectedNode(null);}} select={openSelection} changed={changed}/>)}
      </div>
    </main>
    {form && <LibraryForm mode={form} close={closeForm} complete={message=>{changed(message);if(form==='file'){setView('library');setTab('imports');}}}/>}
    <AppearancePanel open={appearanceOpen} onClose={closeAppearance}/>

  </div>;
}
