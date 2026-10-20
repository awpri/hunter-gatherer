import {useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode} from 'react';
import {emptyActivity, visibleSourceActivity, type GraphActivity} from '../lib/activity';
import {fileType} from '../lib/fileTypes';
import {Icon} from '../views/LibraryShared';
import {FileTypeIcon} from './FileTypeIcon';
import {GraphRecordGlyph} from './GraphRecordGlyph';
import {nextScopedRecord} from './graphKeyboard';
import {useGraphMotion} from './useGraphMotion';
import {useCameraMotion} from './useCameraMotion';
import {ConnectionEffects} from './ConnectionEffects';
import {recordPresentation} from './graphPresentation';
import {graphFocusPresentation, viewportFocusDistance} from './graphFocusPresentation';
import {wheelGesture} from './graphInput';
import {panGain} from './graphCamera';
import {agentFollowCamera} from './graphAgentFollow';
import {organicOverviewPositions} from './graphConstellation';
import {constellationLayout} from './graphConstellationLayout';
import {magneticFocusPositions} from './graphMagneticFocus';
import {groupAtPoint, groupLensAmount} from './graphGroupFocus';
import {useMagneticLens} from './useMagneticLens';
import {GraphBackdrop} from './GraphBackdrop';
import {GraphHelp} from './GraphHelp';
import {clampResizeSize, ResizePreviewLedger} from './graphResize';
import {layoutOverview, neighborsFor, organizeGraph, sharedTopicLinks, type GraphBoardSettings, type GroupBounds, type GroupSize} from './graphLayout';
import {sourceCurve, neighborhood, zoomAt, revealPoint, fitWorld, focusRecordBody, type CameraView, type Point} from './graphGeometry';
import {restoreCamera,validCamera,type CameraSnapshot} from '../lib/navigationPosition';
import './ConnectionMap.css';

export type {MapLayout, GroupingMode, GraphBoardSettings} from './graphLayout';
export type GraphNode = {
  id: string; record_id: string; type: 'memory' | 'document' | 'topic'; label: string; preview: string;
  kind?: string; category?: string; importance?: number; verification?: string; sensitive?: boolean;
  tags?: string[]; group_id?: string; media_type?: string; source_path?: string; file_name?: string;
  icon?: string | null; image?: string | null;
};
export type GraphEdge = {id: string; source: string; target: string; kind: 'source' | 'topic'; label: string; citation?: string};
export type GraphGroup = {
  id: string; label: string; count: number; memory_count: number; document_count: number; record_ids: string[]; color_index: number;
  section_id?: string; section_label?: string; icon?: string; image?: string; description?: string; title?: string; kind?: string; width?: number; height?: number;
};
export type LibraryGraph = {counts?: {matching_records:number;shown_records:number;hidden_topics:number}; nodes: GraphNode[]; edges: GraphEdge[]; groups?: GraphGroup[]; sections?: {id: string; label: string; icon?: string}[]; scope: {memories: number; documents: number; topics: number}; truncated: boolean; warnings: string[]};
type Props = {
  toolbarContent?: ReactNode; initialCamera?:CameraSnapshot; saveCamera?:(snapshot:CameraSnapshot)=>void;
  graph: LibraryGraph; selectedId?: string | null; onSelect: (node: GraphNode) => void; query?: string;
  onGroupSelect?: (id: string) => void; activity?: GraphActivity; boardSettings?: GraphBoardSettings;
  onGroupResize?: (id: string, size: GroupSize) => void | Promise<void>; onEditGroup?: (id: string) => void; onEditRecord?: (id: string) => void;
  focusRequest?: {token: string; ids: string[]; kind: 'read' | 'write'}; onUserNavigate?: () => void;
};
const GROUP_ICONS: Record<string, string> = {'user': 'personal', 'graduation-cap': 'education', 'folder': 'project', 'check-circle': 'decision', 'sliders': 'preferences', 'wrench': 'workflow', 'file-text': 'source', 'hash': 'knowledge', 'inbox': 'archive'};
const RECORD_CARD = {width: 216, height: 204};
const COLORS = ['#628eb7', '#63a68e', '#a28bbe', '#b79562', '#bd7d8c', '#679da9', '#94a16b', '#868fba'];
const clipLabel = (text: string, max = 28) => text.length > max ? `${text.slice(0, max - 1)}…` : text;
const smoothstep = (value: number) => {const t = Math.max(0, Math.min(1, value)); return t * t * (3 - 2 * t);};
const groupStyle = (color: string): CSSProperties => ({'--cluster': color} as CSSProperties);

export function ConnectionMap({graph, selectedId, onSelect, onGroupSelect, activity, boardSettings, onGroupResize, onEditGroup, onEditRecord, focusRequest, onUserNavigate, toolbarContent, initialCamera, saveCamera}: Props) {
  const canvasRef = useRef<HTMLDivElement>(null), svgRef = useRef<SVGSVGElement>(null);
  const restored=useRef(validCamera(initialCamera)?initialCamera:undefined);
  const [size, setSize] = useState(restored.current?.viewport || {width: 900, height: 660});
  const [measured,setMeasured]=useState(false);
  const [showList, setShowList] = useState(false), [hovered, setHovered] = useState<string | null>(null);
  const [hoveredGroup, setHoveredGroup] = useState<string | null>(null);
  const [keyboardNode, setKeyboardNode] = useState<string | null>(null);
  const [previewTarget, setPreviewTarget] = useState<{node?: string; group?: string}>({});
  const [cursorPoint, setCursorPoint] = useState<Point | null>(null);
  const [focusAnchor, setFocusAnchor] = useState<Point | null>(null);
  const [isolate, setIsolate] = useState(false), [dragging, setDragging] = useState(false);
  const [localSizes, setLocalSizes] = useState<Record<string, GroupSize>>({});
  const motion = useGraphMotion(false, showList);
  const {view, viewRef, moveCamera, cancelCamera, zoomCamera, panCamera} = useCameraMotion(motion.reduced,restored.current?.view);
  const positionSnapshot=useRef({view,viewport:size,measured});positionSnapshot.current={view,viewport:size,measured};
  const savePosition=useRef(saveCamera);savePosition.current=saveCamera;
  useLayoutEffect(()=>()=>{if(positionSnapshot.current.measured&&fitted.current)savePosition.current?.({viewport:positionSnapshot.current.viewport,view:{...viewRef.current}});},[]);
  const fitScale = useRef(1);
  const autoCamera = useRef(false);
  const suppressClick = useRef(false);
  const pointers = useRef(new Map<number, Point>());
  const gesture = useRef<{view: CameraView; center: Point; distance: number} | null>(null);
  const resizeLedger = useRef(new ResizePreviewLedger());
  const resize = useRef<{pointer: number; token: number; id: string; start: Point; size: GroupSize; minimum: GroupSize} | null>(null);
  const latestResize = useRef<GroupSize | null>(null);
  const manualNavigate = useCallback(() => {cancelCamera(); autoCamera.current = false; onUserNavigate?.();}, [cancelCamera, onUserNavigate]);

  const organized = useMemo(() => organizeGraph(graph.nodes.filter(node => !graph.groups?.length || node.type !== 'topic'), graph.edges, graph.groups || [], boardSettings?.grouping), [graph, boardSettings?.grouping]);
  const isConstellation = boardSettings?.layout === 'constellation';
  const layout = useMemo(() => {
    const base = layoutOverview(organized.nodes, graph.edges, organized.groups, size, {...boardSettings, groupSizes: {...boardSettings?.groupSizes, ...localSizes}});
    return isConstellation ? constellationLayout(base, graph.edges, size.width / size.height) : base;
  }, [organized, graph.edges, size, boardSettings, localSizes, isConstellation]);
  const overviewScale = useMemo(() => fitWorld({x: 0, y: 0, width: layout.width, height: layout.height}, size, 1).scale, [layout.width, layout.height, size]);
  const overviewType = {
    '--cm-section-title-size': `${Math.max(24, Math.min(10 / overviewScale, 13 / view.scale))}px`,
    '--cm-beam-width': `${Math.max(2.3, 2.3 / overviewScale)}px`,
  } as CSSProperties;
  const allNodes = useMemo(() => organized.nodes.filter(node => layout.positions.has(node.id)).map(node => ({...node, ...boardSettings?.records?.[node.id]})), [organized, layout.positions, boardSettings?.records]);
  const byId = useMemo(() => new Map(allNodes.map(node => [node.id, node])), [allNodes]);
  const groupMeta = useMemo(() => new Map(organized.groups.map(group => [group.id, {...group, ...boardSettings?.groups?.[group.id]}])), [organized.groups, boardSettings?.groups]);
  const groupByNode = useMemo(() => new Map(layout.groups.flatMap(group => group.nodeIds.map(id => [id, group] as const))), [layout.groups]);
  const edges = useMemo<GraphEdge[]>(() => [
    ...graph.edges.filter(edge => edge.source !== edge.target && layout.positions.has(edge.source) && layout.positions.has(edge.target)),
    ...sharedTopicLinks(selectedId, graph.nodes, graph.edges).filter(edge => layout.positions.has(edge.source) && layout.positions.has(edge.target)),
  ], [graph, layout.positions, selectedId]);
  const neighbors = useMemo(() => neighborsFor(selectedId, edges), [selectedId, edges]);
  const awake = useMemo(() => neighborsFor(hovered || selectedId, edges), [hovered, selectedId, edges]);
  const visibleIds = useMemo(() => neighborhood(selectedId, edges), [selectedId, edges]);
  const nodes = useMemo(() => isolate && selectedId ? allNodes.filter(node => visibleIds.has(node.id)) : allNodes, [isolate, selectedId, allNodes, visibleIds]);
  const shownEdges = useMemo(() => edges.filter(edge => !isolate || !selectedId || (visibleIds.has(edge.source) && visibleIds.has(edge.target))), [edges, isolate, selectedId, visibleIds]);
  const groups = useMemo(() => layout.groups.filter(group => !isolate || !selectedId || group.nodeIds.some(id => visibleIds.has(id))), [layout.groups, isolate, selectedId, visibleIds]);
  const relativeZoom = view.scale / overviewScale;
  const organic = useMemo(() => boardSettings?.layout === 'columns' ? layout.positions : organicOverviewPositions(layout), [layout, boardSettings?.layout]);
  const focusPoint = cursorPoint ? {x: (cursorPoint.x - view.x) / view.scale, y: (cursorPoint.y - view.y) / view.scale}
    : focusAnchor || {x: (size.width / 2 - view.x) / view.scale, y: (size.height / 2 - view.y) / view.scale};
  const focusScreen = {x: focusPoint.x * view.scale + view.x, y: focusPoint.y * view.scale + view.y};
  const detailDistance = viewportFocusDistance(size.width, size.height, focusScreen.x, focusScreen.y);
  const spread = smoothstep((relativeZoom - 1.15) / .35);
  const cursorWorld = cursorPoint ? focusPoint : null;
  const cursorGroup = groupAtPoint(groups, cursorWorld);
  const cursorStrength = groupLensAmount(cursorGroup, cursorWorld, view.scale);
  const lens = useMagneticLens({
    point: focusPoint,
    radius: 220 / view.scale,
    amount: cursorStrength * (.22 + .5 * spread),
  }, motion.reduced, showList);
  const magnetic = useMemo(() => magneticFocusPositions(organic, cursorGroup ? [cursorGroup] : [], lens.point, lens.amount, {
    radius: lens.radius, maxDisplacement: Math.min(lens.radius * .11, 45 * (cursorGroup?.recordScale || 1)),
    cardGap: 228 * Math.max(1, ...layout.groups.map(group => group.recordScale)),
    cardHeight: 214 * Math.max(1, ...layout.groups.map(group => group.recordScale)),
  }), [organic, layout.groups, lens, cursorGroup]);
  const positions = magnetic;
  const active = useMemo(() => visibleSourceActivity(activity || emptyActivity(), new Set(nodes.map(node => node.id)), graph.edges), [activity, nodes, graph.edges]);
  const hoverNode = hovered ? byId.get(hovered) : undefined;
  const previewNode = previewTarget.node ? byId.get(previewTarget.node) : undefined;
  const previewGroup = previewTarget.group ? groupMeta.get(previewTarget.group) : undefined;
  const previewMembers = previewTarget.group ? groups.find(group => group.id === previewTarget.group)?.nodeIds.map(id => byId.get(id)).filter((node): node is GraphNode => !!node) || [] : [];
  const previewLinks = previewNode ? neighborhood(previewNode.id, graph.edges.filter(edge => edge.kind === 'source')).size - 1 : 0;
  const previewText = (previewNode?.preview || previewGroup?.description || previewMembers[0]?.preview || '').replace(/^#+\s*/, '').replace(/\s+/g, ' ');
  const inspectNode = (id: string) => {setHovered(id); setPreviewTarget({node: id});};
  useEffect(() => {if (selectedId) setPreviewTarget({node: selectedId});}, [selectedId]);

  const fit = useCallback((ids?: Set<string>, animate = true) => {
    let bounds = {x: 0, y: 0, width: layout.width, height: layout.height};
    let recordScale = 1;
    if (ids) {
      const points = [...organic].filter(([id]) => ids.has(id)).map(([, point]) => point);
      if (!points.length) return;
      recordScale = Math.max(1, ...[...ids].map(id => groupByNode.get(id)?.recordScale || 1));
      const x = Math.min(...points.map(point => point.x)) - 148 * recordScale, y = Math.min(...points.map(point => point.y)) - 112 * recordScale;
      bounds = {x, y, width: Math.max(...points.map(point => point.x)) + 148 * recordScale - x, height: Math.max(...points.map(point => point.y)) + 112 * recordScale - y};
    }
    const target = fitWorld(bounds, size, ids ? 1.05 / recordScale : 1);
    if (!ids) {fitScale.current = target.scale; setFocusAnchor(null);}
    else setFocusAnchor({x: (size.width / 2 - target.x) / target.scale, y: (size.height / 2 - target.y) / target.scale});
    moveCamera(target, animate);
  }, [layout, size, moveCamera, groupByNode, organic]);
  const focusGroup = useCallback((group: GroupBounds, manual = true) => {
    if (manual) manualNavigate();
    const points = group.nodeIds.map(id => organic.get(id)).filter((point): point is Point => Boolean(point));
    const target = focusRecordBody(points, size, group.recordScale) || fitWorld(group, size, 1.55);
    setFocusAnchor({x: (size.width / 2 - target.x) / target.scale, y: (size.height / 2 - target.y) / target.scale});
    setPreviewTarget({group: group.id});
    moveCamera(target);
  }, [size, organic, moveCamera, manualNavigate]);
  const commitResize = useCallback((id: string, requested: GroupSize, existingToken?: number) => {
    const size = clampResizeSize(requested);
    const ledger = resizeLedger.current;
    const token = existingToken ?? ledger.begin(id, size);
    if (existingToken !== undefined && !ledger.update(id, token, size)) return;
    setLocalSizes(ledger.snapshot());
    Promise.resolve().then(() => onGroupResize?.(id, size)).catch(() => {
      // App owns the error notice and authoritative board refresh.
    }).finally(() => {if (ledger.settle(id, token)) setLocalSizes(ledger.snapshot());});
  }, [onGroupResize]);
  const zoom = useCallback((factor: number, point = {x: size.width / 2, y: size.height / 2}, accelerate = false) => {
    autoCamera.current = false;
    onUserNavigate?.();
    setFocusAnchor({x: (point.x - viewRef.current.x) / viewRef.current.scale, y: (point.y - viewRef.current.y) / viewRef.current.scale});
    zoomCamera(factor, point, Math.max(.000001, fitScale.current * .4), 4, accelerate);
  }, [size, zoomCamera, onUserNavigate]);

  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const measure = (width: number, height: number) => {
      const next = {width: Math.max(280, width), height: Math.max(200, height)};
      setSize(previous => previous.width === next.width && previous.height === next.height ? previous : next);
      setMeasured(true);
    };
    const bounds = canvas.getBoundingClientRect();
    measure(bounds.width, bounds.height);
    const observer = new ResizeObserver(([entry]) => measure(entry.contentRect.width, entry.contentRect.height));
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [showList]);
  // Content and viewport changes must never act like the Fit all button.
  // Fit the first graph and explicit organization changes; resize around the
  // existing world center without changing the user's zoom.
  const fitKey = `${boardSettings?.layout || 'board'}:${boardSettings?.grouping || 'subjects'}`;
  const fitted = useRef('');
  const cameraViewport = useRef(size);
  useLayoutEffect(() => {
    if(!measured)return;
    const previousViewport = cameraViewport.current;
    cameraViewport.current = size;
    if(!fitted.current&&restored.current){fitted.current=fitKey;fitScale.current=overviewScale;moveCamera(restoreCamera(restored.current,size),false);restored.current=undefined;return;}
    if (fitted.current !== fitKey && (layout.positions.size || layout.groups.length)) {const first = !fitted.current; fitted.current = fitKey; fit(undefined, !first);return;}
    if (fitted.current && (previousViewport.width !== size.width || previousViewport.height !== size.height)) {
      moveCamera(restoreCamera({view:viewRef.current,viewport:previousViewport},size),false);
    }
  }, [measured,fitKey, fit, layout.positions.size, layout.groups.length,overviewScale,moveCamera,size]);
  const lastFocus = useRef('');
  useEffect(() => {
    if (!focusRequest) {if (autoCamera.current) {cancelCamera(); autoCamera.current = false;} return;}
    if (focusRequest.token === lastFocus.current || showList || dragging || resize.current) return;
    const ids = new Set(focusRequest.ids.filter(id => layout.positions.has(id)));
    if (!ids.size) return;
    lastFocus.current = focusRequest.token;
    const points = [...ids].flatMap(id => {
      const point = organic.get(id);
      return point ? [{...point, recordScale: groupByNode.get(id)?.recordScale || 1}] : [];
    });
    const current = viewRef.current;
    const target = agentFollowCamera(current, size, points, overviewScale);
    // Cancel a previous batch's pending movement even when the new activity is
    // already visible; it must not continue zooming toward an obsolete target.
    if (autoCamera.current) cancelCamera();
    autoCamera.current = target !== current;
    if (target !== current) moveCamera(target);
  }, [focusRequest, layout.positions, organic, groupByNode, size, overviewScale, showList, dragging, cancelCamera, moveCamera]);
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      const bounds = svg.getBoundingClientRect();
      const point = {x: event.clientX - bounds.left, y: event.clientY - bounds.top};
      const input = wheelGesture(event, bounds.height);
      setCursorPoint(point);
      if (input.kind === 'zoom') zoom(input.factor, point);
      else {
        autoCamera.current = false; onUserNavigate?.();
        const gain = panGain(viewRef.current.scale / fitScale.current);
        panCamera(input.dx * gain, input.dy * gain);
      }
    };
    svg.addEventListener('wheel', wheel, {passive: false});
    return () => svg.removeEventListener('wheel', wheel);
  }, [zoom, showList, panCamera, onUserNavigate]);

  const pointerCenter = () => {
    const points = [...pointers.current.values()];
    return {center: {x: points.reduce((sum, point) => sum + point.x, 0) / points.length, y: points.reduce((sum, point) => sum + point.y, 0) / points.length}, distance: points.length > 1 ? Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) : 0};
  };
  const beginGesture = () => {
    if (!pointers.current.size) {gesture.current = null; setDragging(false); return;}
    gesture.current = {view: viewRef.current, ...pointerCenter()};
  };
  const overview = () => {manualNavigate(); setIsolate(false); fit();};
  const selectNode = (node: GraphNode) => {manualNavigate(); onSelect(node); inspectNode(node.id);};

  return <section className={`connection-map ${isConstellation ? "cm-constellation" : "cm-board"}`} data-layout={boardSettings?.layout || "board"} aria-label="Library connection map" onKeyDown={event => {
    if ((event.target as Element).closest('[role=dialog]')) return;
    if (event.key === 'Escape') {setHovered(null); setHoveredGroup(null); overview();}
  }}>
    <div className="cm-header"><div className="cm-toolbar">
      <div className="cm-toolbar-row">
      <button className="cm-tool" onClick={() => {manualNavigate(); setShowList(value => !value);}}><Icon name={showList ? 'connections' : 'list'} size={14} />{showList ? 'Map' : 'List'}</button>
      {!showList && <button className="cm-tool" title="Fit every group in the canvas" onClick={overview}><Icon name="fit" size={14} />Fit all</button>}
      <span className="cm-scope"><Icon name="connections" size={13}/>{allNodes.filter(node => node.type !== 'topic').length}{graph.counts ? ` of ${graph.counts.matching_records}` : ''} records{graph.truncated ? ' · Limited view' : ''}</span>
      <GraphHelp constellation={isConstellation} truncated={graph.truncated} omitted={layout.omitted} warnings={graph.warnings}/>
      </div>
      <div className="cm-toolbar-row cm-toolbar-secondary">
        {toolbarContent}
        {!showList && selectedId && <button className="cm-tool cm-neighborhood" aria-pressed={isolate} onClick={() => {manualNavigate(); setIsolate(value => !value); if (!isolate) fit(visibleIds); else fit();}}>Connections</button>}
      </div>
    </div>
    <aside className={`cm-preview-dock ${previewNode || previewGroup ? 'has-preview' : ''}`} aria-label="Record preview">
      {previewNode || previewGroup ? <>
        <div className="cm-dock-top">{previewNode?.type === 'document' ? <FileTypeIcon metadata={previewNode} size={22}/> : <Icon name={previewNode ? 'memory' : 'knowledge'} size={20}/>}<div><small>{previewNode ? previewNode.type === 'document' ? fileType(previewNode).extension.toUpperCase() : previewNode.kind || 'Memory' : 'Group'}</small><strong>{previewNode?.label || previewGroup?.title || previewGroup?.label}</strong></div>
          {previewNode && <button className="cm-preview-open" aria-label="Open previewed record" title="Open record" onClick={() => selectNode(previewNode)}><Icon name="arrow" size={16}/></button>}
        </div>
        <p>{previewText}</p>
        {previewNode && <small className="cm-dock-links">{previewLinks ? `${previewLinks} linked ${previewLinks === 1 ? 'record' : 'records'}` : 'No recorded connections'}</small>}
      </> : <div className="cm-dock-empty"><Icon name="source" size={20}/><span><strong>Record preview</strong><small>Point to a memory or file</small></span></div>}
    </aside></div>
    {showList ? <div className="cm-list">{graph.nodes.map(node => <article key={node.id} className={node.id === selectedId ? 'selected' : ''}>
      <button onClick={() => onSelect(node)}>{node.type === 'document' ? <FileTypeIcon metadata={node} size={27} /> : <i className="cm-memory-dot" />}<span><strong>{node.label}</strong><small>{node.preview || node.type}</small></span></button>
    </article>)}</div> : <div className={`cm-canvas ${dragging ? 'dragging' : ''} ${motion.stopped ? 'motion-paused' : ''}`} ref={canvasRef} style={{backgroundImage: 'none'}}>
      {!nodes.length && !groups.length ? <div className="cm-empty">No records in this view.</div> : <svg ref={svgRef} className="cm-svg" viewBox={`0 0 ${size.width} ${size.height}`} aria-label="Interactive library map. Scroll to zoom. Shift plus scroll or drag to pan. Pinch to zoom. Tab selects groups and records."
        onClickCapture={event => {if (event.detail > 0 && suppressClick.current) {event.preventDefault(); event.stopPropagation(); suppressClick.current = false;}}}
        onPointerDownCapture={event => {
          if (event.button !== 0) return;
          if (event.pointerType !== 'touch' && (event.target as Element).closest('.cm-node, .cm-edit-group, .cm-resize')) return;
          manualNavigate(); setFocusAnchor(null); setCursorPoint(null);
          const bounds = event.currentTarget.getBoundingClientRect();
          if (!pointers.current.size) suppressClick.current = false;
          pointers.current.set(event.pointerId, {x: event.clientX - bounds.left, y: event.clientY - bounds.top});
          if (pointers.current.size > 1) suppressClick.current = true;
          if (event.pointerType !== 'touch') event.currentTarget.setPointerCapture(event.pointerId);
          beginGesture(); setDragging(true);
        }}
        onPointerLeave={() => setCursorPoint(null)}
        onPointerMove={event => {
          if (event.pointerType !== 'touch' && !pointers.current.size && !resize.current) {
            const bounds = event.currentTarget.getBoundingClientRect();
            setCursorPoint({x: event.clientX - bounds.left, y: event.clientY - bounds.top});
          }
          if (resize.current?.pointer === event.pointerId) {
            const value = resize.current;
            const next = clampResizeSize({width: value.size.width + (event.clientX - value.start.x) / viewRef.current.scale, height: value.size.height + (event.clientY - value.start.y) / viewRef.current.scale}, value.minimum);
            latestResize.current = next;
            if (resizeLedger.current.update(value.id, value.token, next)) setLocalSizes(resizeLedger.current.snapshot());
            return;
          }
          if (!pointers.current.has(event.pointerId) || !gesture.current) return;
          const bounds = event.currentTarget.getBoundingClientRect();
          pointers.current.set(event.pointerId, {x: event.clientX - bounds.left, y: event.clientY - bounds.top});
          const current = pointerCenter(), start = gesture.current;
          if (Math.hypot(current.center.x - start.center.x, current.center.y - start.center.y) > 6 || pointers.current.size > 1) suppressClick.current = true;
          if (!suppressClick.current) return;
          if (!event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.setPointerCapture(event.pointerId);
          const scaled = zoomAt(start.view, start.distance > 0 ? current.distance / start.distance : 1, start.center, Math.max(.000001, fitScale.current * .4));
          moveCamera({...scaled, x: scaled.x + current.center.x - start.center.x, y: scaled.y + current.center.y - start.center.y}, false);
        }}
        onPointerUp={event => {
          if (resize.current?.pointer === event.pointerId) {const value = resize.current; resize.current = null; if (latestResize.current) commitResize(value.id, latestResize.current, value.token); latestResize.current = null; setDragging(false);}
          pointers.current.delete(event.pointerId); beginGesture();
          if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
        }}
        onPointerCancel={() => {if (resize.current) {const value = resize.current; if (resizeLedger.current.settle(value.id, value.token)) setLocalSizes(resizeLedger.current.snapshot());} resize.current = null; latestResize.current = null; pointers.current.clear(); beginGesture();}}
      >
        <GraphBackdrop view={view} overviewScale={overviewScale} width={size.width} height={size.height}/>
        <g className="cm-world" style={overviewType} transform={`translate(${view.x} ${view.y}) scale(${view.scale})`}>
          {layout.sections?.map(section => <g key={section.id} className="cm-section" transform={`translate(${section.x} ${section.y})`}>
            <text x={0} y={(section.headingHeight || 62) * .68}>{section.label}</text><path d={`M ${Math.min(section.width * .5, (section.label.length * 6 + 20) / overviewScale)} ${(section.headingHeight || 62) * .58} H ${section.width}`} />
          </g>)}
          {groups.map(group => {
            const meta = groupMeta.get(group.id);
            const color = COLORS[group.colorIndex % COLORS.length];
            const members = group.nodeIds.map(id => byId.get(id)).filter((node): node is GraphNode => Boolean(node));
            const files = members.filter(node => node.type === 'document'), memories = members.filter(node => node.type === 'memory');
            const label = meta?.title || meta?.label || 'Library';
            const hoveredCard = cursorGroup?.id === group.id || hoveredGroup === group.id;
            const inset = group.contentPadding;
            const titleSize = Math.min((group.width - inset * 2) / 10, 14 / overviewScale, 22 / view.scale);
            const metaSize = Math.min(9 / overviewScale, 11 / view.scale);
            const iconSize = Math.min(28 / overviewScale, 38 / view.scale, group.headerHeight * .55);
            const style = {...groupStyle(color), "--cm-group-title-size": `${titleSize}px`, "--cm-group-meta-size": `${metaSize}px`, "--cm-group-icon-size": `${iconSize}px`, "--cm-group-icon-gap": `${Math.min(12 / overviewScale, 18 / view.scale)}px`} as CSSProperties;
            return <g key={group.id} className={`cm-group ${group.nodeIds.includes(selectedId || '') ? 'selected' : ''} ${hoveredCard ? 'hovered' : ''}`} style={style}
              onMouseEnter={() => {if (!previewTarget.node) setPreviewTarget({group: group.id});}}>
              <g className="cm-group-surface" role="button" tabIndex={0} aria-label={`${label}, ${memories.length} memories, ${files.length} files. Zoom into group.`}
                onClick={() => focusGroup(group)} onDoubleClick={() => onGroupSelect?.(group.id)}
                onFocus={() => {setHoveredGroup(group.id); setPreviewTarget({group: group.id}); const first = nodes.find(node => group.nodeIds.includes(node.id)); if (first) setKeyboardNode(first.id);}} onBlur={() => setHoveredGroup(null)}
                onKeyDown={event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault(); focusGroup(group);const first=nodes.find(node=>group.nodeIds.includes(node.id));if(first){setKeyboardNode(first.id);requestAnimationFrame(()=>{const element=Array.from(svgRef.current?.querySelectorAll<SVGGElement>('[data-record-id]')||[]).find(item=>item.dataset.recordId===first.id);element?.focus();});}} }}>
                {isConstellation ? <>
                  <ellipse className="cm-constellation-aura" cx={group.x + group.width / 2} cy={group.y + group.headerHeight + (group.height - group.headerHeight) / 2} rx={group.width / 2} ry={(group.height - group.headerHeight) / 2}/>
                  <rect className="cm-constellation-hit" x={group.x} y={group.y} width={group.width} height={group.height} rx={24}/>
                </> : <>
                  <rect x={group.x} y={group.y} width={group.width} height={group.height} rx={24} className="cm-group-background" />
                  <rect x={group.x + 1} y={group.y + 1} width={group.width - 2} height={7} rx={3.5} className="cm-group-accent" />
                </>}
                <foreignObject x={group.x + inset} y={group.y + inset} width={group.width - inset * 2} height={Math.max(group.headerHeight - inset * 1.5, titleSize * 1.6 + metaSize * 1.25 + 8)} pointerEvents="none">
                  <div className="cm-group-copy cm-group-identity">
                    <span className="cm-group-symbol">{meta?.icon && /[^a-z-]/i.test(meta.icon) ? meta.icon : <Icon name={GROUP_ICONS[meta?.icon || ''] || meta?.icon || ({decision:'decision',fact:'knowledge',files:'source',preference:'preferences',procedure:'workflow',plan:'project'}[label.toLowerCase()] || 'knowledge')} size={iconSize} />}</span>
                    <div><h3>{label}</h3><span className="cm-group-counts">{memories.length} memories <span className="cm-count-divider">/</span> {files.length} files</span></div>
                  </div>
                </foreignObject>
                <text className="cm-group-arrow" x={group.x + group.width - 48} y={group.y + 60}>↗</text>
                {!isConstellation && <path className="cm-group-divider" d={`M ${group.x + inset} ${group.y + group.headerHeight - inset * .25} H ${group.x + group.width - inset}`} />}
              </g>
              {meta?.image && <image href={meta.image} x={group.x + group.width - 157} y={group.y + group.headerHeight - 105} width={80} height={80} preserveAspectRatio="xMidYMid slice" className="cm-custom-image" pointerEvents="none" />}
              {onEditGroup && <g className="cm-edit-group" role="button" tabIndex={0} aria-label={`Edit ${label}`} transform={`translate(${group.x + group.width - 46} ${group.y + 94})`}
                onClick={() => onEditGroup(group.id)} onKeyDown={event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault(); onEditGroup(group.id);} }}>
                <rect x={-16} y={-16} width={32} height={32} rx={9} /><text textAnchor="middle" y={7}>···</text>
              </g>}
              {onGroupResize && <g className="cm-resize" role="button" tabIndex={0} aria-label={`Resize ${label}. Arrow keys resize.`} transform={`translate(${group.x + group.width - 25} ${group.y + group.height - 25})`}
                onPointerDown={event => {
                  event.stopPropagation(); manualNavigate(); suppressClick.current = true;
                  const token = resizeLedger.current.begin(group.id, {width: group.width, height: group.height});
                  resize.current = {pointer: event.pointerId, token, id: group.id, start: {x: event.clientX, y: event.clientY}, size: {width: group.width, height: group.height}, minimum: group.minimumSize};
                  latestResize.current = resize.current.size;
                  svgRef.current?.setPointerCapture(event.pointerId); setDragging(true);
                }}
                onClick={event => event.stopPropagation()}
                onKeyDown={event => {
                  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
                  event.preventDefault(); event.stopPropagation(); manualNavigate();
                  const next = clampResizeSize({width: group.width + (event.key === 'ArrowRight' ? 40 : event.key === 'ArrowLeft' ? -40 : 0), height: group.height + (event.key === 'ArrowDown' ? 40 : event.key === 'ArrowUp' ? -40 : 0)}, group.minimumSize);
                  commitResize(group.id, next);
                }}><rect x={-18} y={-18} width={36} height={36} rx={9} /><path d="M -7 8 L 8 -7 M 0 8 L 8 0" /></g>}
            </g>;
          })}
          <g className="cm-connections">
            {shownEdges.map(edge => {
              const from = positions.get(edge.source)!, to = positions.get(edge.target)!;
              const highlighted = awake.has(edge.source) && awake.has(edge.target);
              return <path key={edge.id} d={sourceCurve(from, to, .24, Infinity)} className={`cm-edge ${edge.kind} ${highlighted ? 'active' : ''}`} vectorEffect="non-scaling-stroke" style={{opacity: highlighted ? .56 : hovered ? .07 : .16}}><title>{edge.label}{edge.citation ? ` · ${edge.citation}` : ''}</title></path>;
            })}
          </g>
          <ConnectionEffects edges={shownEdges} positions={positions} hovered={hovered} hoverType={hoverNode?.type} activity={active} cameraScale={view.scale} stopped={motion.stopped} reduced={motion.reduced} />
          {nodes.map(node => {
            const point = positions.get(node.id)!, group = groupByNode.get(node.id);
            const selected = node.id === selectedId, attention = node.id === hovered, wake = awake.has(node.id), live = active.nodes[node.id];
            const color = node.type === 'document' ? fileType(node).color : COLORS[(group?.colorIndex || 0) % COLORS.length];
            const recordScale = group?.recordScale || 1;
            const base = organic.get(node.id) || point;
            const distance = Math.hypot(base.x - lens.point.x, base.y - lens.point.y) * view.scale;
            const focus = graphFocusPresentation({relativeZoom, screenDistance: distance, maxScreenDistance: detailDistance,
              lensAmount: 0, lensRadius: 200});
            const presentation = recordPresentation(focus.localZoom, view.scale * recordScale * .85 * focus.magnification);
            const cardOpacity = focus.detailReveal;
            const card = cardOpacity > .5;
            return <g key={node.id} transform={`translate(${point.x} ${point.y})`} className={`cm-node ${node.type} ${selected ? 'selected' : ''} ${wake ? 'awake' : ''} ${attention ? 'hovered' : ''} ${card ? 'with-card' : ''} compact-detail ${live ? `live-${live.kind}` : ''}`} style={{...groupStyle(color), "--record-title": `${presentation.titleSize}px`, "--record-copy": `${presentation.copySize}px`} as CSSProperties}
              data-group-id={group?.id} data-lens-group={cursorGroup?.id || ""} data-detail={card ? "title" : "glyph"} data-magnification={focus.magnification.toFixed(3)} data-detail-reveal={cardOpacity.toFixed(3)} data-record-scale={recordScale}
              role="button" data-record-id={node.id} tabIndex={node.id === (keyboardNode && byId.has(keyboardNode) ? keyboardNode : nodes[0]?.id) ? 0 : -1} aria-label={`${node.type}: ${node.label}`} aria-pressed={selected}
              onClick={() => selectNode(node)} onMouseEnter={() => inspectNode(node.id)} onMouseLeave={() => setHovered(null)}
              onFocus={() => {setKeyboardNode(node.id);manualNavigate(); inspectNode(node.id); setCursorPoint(null); setFocusAnchor(point); const target = revealPoint(viewRef.current, size, point, Math.max(45, 128 * recordScale * viewRef.current.scale)); if (target !== viewRef.current) moveCamera(target);}}
              onBlur={() => setHovered(null)} onContextMenu={event => {if (onEditRecord) {event.preventDefault(); onEditRecord(node.id);} }}
              onKeyDown={event => {if (['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key)) {event.preventDefault(); const candidates = nodes.filter(item => groupByNode.get(item.id)?.id === group?.id); const next = nextScopedRecord(candidates.map(item=>item.id),node.id,event.key); const target = candidates.find(item=>item.id===next); if (target) {setKeyboardNode(target.id); const element = Array.from(svgRef.current?.querySelectorAll<SVGGElement>('[data-record-id]') || []).find(item => item.dataset.recordId === target.id); element?.focus();}} if (event.key === 'Enter' || event.key === ' ') {event.preventDefault(); selectNode(node);} if (event.key === 'F2' && onEditRecord) {event.preventDefault(); onEditRecord(node.id);} }}>
              <title>{node.label}{node.sensitive ? ' · Sensitive record' : ''}</title>
              <g className="cm-node-scale" transform={`scale(${recordScale})`}><g className="cm-cursor-lens" style={{transform: `scale(${focus.magnification})`}}><g className="cm-node-arrival">
                <rect className="cm-hit-area" x={-110} y={-104} width={220} height={208} rx={16} />
                <g className="cm-detail-layer" transform="scale(.85)" opacity={cardOpacity} pointerEvents="none">
                  <rect className="cm-record-surface" x={-RECORD_CARD.width / 2} y={-RECORD_CARD.height / 2} width={RECORD_CARD.width} height={RECORD_CARD.height} rx={15} />
                  <foreignObject x={-94} y={16} width={188} height={80} pointerEvents="none"><div className="cm-record-copy"><strong>{node.label}</strong></div></foreignObject>
                </g>
                <g className="cm-record-identity" pointerEvents="none" transform={`translate(0 ${-39.1 * cardOpacity})`}>
                  <circle className="cm-node-halo" r={84} style={{opacity: .07 * (1 - cardOpacity)}}/>
                  <GraphRecordGlyph node={node} size={136 - 47.6 * cardOpacity}/>
                </g>
                {onEditRecord && attention && card && <g className="cm-record-edit" role="button" aria-label={`Edit ${node.label}`} transform="translate(71 -49)" onClick={event => {event.stopPropagation(); onEditRecord(node.id);} }><rect x={-12} y={-12} width={24} height={24} rx={6} /><text y={4} textAnchor="middle">···</text></g>}
                <rect className="cm-focus-ring" x={card ? -116 : -90} y={card ? -RECORD_CARD.height / 2 - 8 : -90} width={card ? 232 : 180} height={card ? RECORD_CARD.height + 16 : 180} rx={card ? 20 : 26} />
              </g></g></g>
            </g>;
          })}
        </g>
      </svg>}
      <div className="cm-controls" aria-label="Map controls">
        <button aria-label="Zoom in" onClick={() => zoom(1.3)}><Icon name="plus" size={16} /></button><button aria-label="Zoom out" onClick={() => zoom(1 / 1.3)}><Icon name="minus" size={16} /></button><span>{Math.round(view.scale / fitScale.current * 100)}%</span>
        <button aria-label="Fit all groups" title="Fit all groups" onClick={overview}><Icon name="fit" size={16} /></button>
      </div>
      <div className="cm-legend"><span><i className="memory" />Memory</span><span><i className="file" />File</span><span><i className="source" />Citation</span>{edges.some(edge => edge.kind === 'topic') && <span><i className="topic" />Topic</span>}</div>
    </div>}
  </section>;
}

export function groupTitleLines(title: string, limit: number): string[] {
  return previewLines(title, limit, 2);
}
export function previewLines(text: string, width: number, count = 3): string[] {
  const words = text.replace(/\s+/g, ' ').trim().split(' '), result: string[] = [];
  let line = '';
  for (const word of words) {
    if (line && line.length + word.length + 1 > width) {
      result.push(line); line = word;
      if (result.length === count) return [...result.slice(0, count - 1), clipLabel(`${result[count - 1]} ${line}`, width)];
    } else line = line ? `${line} ${word}` : word;
  }
  if (line) result.push(line);
  return result.slice(0, count).map(value => clipLabel(value, width));
}
