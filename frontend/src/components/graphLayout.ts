export type MapLayout = 'board' | 'constellation' | 'columns';
export type GroupingMode = 'subjects' | 'kind' | 'source';
export type GroupSize = {width: number; height: number};
export type BoardGroupOverride = {title?: string; description?: string; icon?: string | null; image?: string | null; width?: number; height?: number; record_ids?: string[]; kind?: string};
export type GraphBoardSettings = {
  version?: number; revision?: number; follow_agent?: boolean;
  groups?: Record<string, BoardGroupOverride>;
  records?: Record<string, {icon?: string | null; image?: string | null}>;
  headerHeight?: number;
  minimumGroupWidth?: number;
  overviewSpacing?: number;
  recordScale?: number;
  layout?: MapLayout;
  grouping?: GroupingMode;
  groupSizes?: Record<string, GroupSize>;
};
export type LayoutInputNode = {
  id: string; type: 'memory' | 'document' | 'topic'; group_id?: string;
  record_id?: string; kind?: string; source_path?: string; media_type?: string;
};
export type LayoutInputEdge = {id: string; source: string; target: string; kind: 'source' | 'topic'};
export type LayoutInputGroup = {id: string; label?: string; record_ids?: string[]; color_index?: number; section_id?: string; section_label?: string; icon?: string; kind?: string; width?: number; height?: number};
export type Point = {x: number; y: number};
export type GroupBounds = {id: string; x: number; y: number; width: number; height: number; colorIndex: number; nodeIds: string[]; headerHeight: number; recordScale: number; contentPadding: number; minimumSize: GroupSize};
export type SectionBounds = {id: string; label: string; x: number; y: number; width: number; height: number; headingHeight?: number};
export type LayoutResult = {sections?: SectionBounds[]; positions: Map<string, Point>; groups: GroupBounds[]; width: number; height: number; omitted: number};

// Cards, drift, and their preview fan all fit within these reserved world slots.
export const RECORD_SLOT = {width: 286, height: 232};
export const RECORD_CARD = {width: 216, height: 148};
const PADDING = 32;
const HEADER = 232;
const GROUP_GAP = 46;
const SECTION_GAP = 62;

function seededUnit(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index++) hash = Math.imul(hash ^ value.charCodeAt(index), 16777619);
  return (hash >>> 0) / 4294967295;
}

export function minimumGroupSize(count: number, columns = Math.min(3, Math.max(1, Math.ceil(Math.sqrt(count))))) {
  return {width: Math.max(540, columns * RECORD_SLOT.width + PADDING * 2), height: HEADER + Math.ceil(count / columns) * RECORD_SLOT.height + PADDING};
}

/** Alternate organizations change placement only. Source grouping uses recorded
 * file paths and citation neighbors; no synthetic relationship is introduced. */
export function organizeGraph<N extends LayoutInputNode, G extends LayoutInputGroup>(nodes: readonly N[], edges: readonly LayoutInputEdge[], groups: readonly G[], mode: GroupingMode = 'subjects'): {nodes: (N & {group_id?: string})[]; groups: LayoutInputGroup[]} {
  if (mode === 'subjects') return {nodes: [...nodes], groups: [...groups]};
  const byId = new Map(nodes.map(node => [node.id, node]));
  const sourceKey = (node: LayoutInputNode) => {
    const path = node.source_path?.replace(/\\/g, '/');
    return path?.includes('/') ? path.slice(0, path.lastIndexOf('/')).split('/').filter(Boolean).slice(-2).join('/') : 'Saved files';
  };
  const membership = new Map<string, LayoutInputGroup>();
  const assigned = nodes.map(node => {
    let label: string;
    if (mode === 'kind') label = node.type === 'document' ? 'Files' : node.type === 'topic' ? 'Topics' : node.kind ? `${node.kind[0].toUpperCase()}${node.kind.slice(1)}` : 'Memories';
    else if (node.type === 'document') label = sourceKey(node);
    else {
      const files = edges.filter(edge => edge.kind === 'source' && (edge.source === node.id || edge.target === node.id))
        .map(edge => byId.get(edge.source === node.id ? edge.target : edge.source)).filter((other): other is N => other?.type === 'document')
        .sort((a, b) => a.id.localeCompare(b.id));
      label = files.length ? sourceKey(files[0]) : 'Without a source file';
    }
    const group_id = `${mode}:${label}`;
    if (!membership.has(group_id)) membership.set(group_id, {id: group_id, label, record_ids: [], color_index: membership.size});
    membership.get(group_id)!.record_ids!.push(node.id);
    return {...node, group_id};
  });
  return {nodes: assigned, groups: [...membership.values()]};
}

/** Deterministic shelf packing and card slots preserve every isolated record,
 * leave real room for details, and never overlap resized groups. */
export function layoutGraph(nodes: readonly LayoutInputNode[], edges: readonly LayoutInputEdge[], groups: readonly LayoutInputGroup[] = [], aspectRatio = 1.6, settings: GraphBoardSettings = {}): LayoutResult {
  const unique = [...new Map(nodes.filter(node => node.id).map(node => [node.id, node])).values()].sort((a, b) => a.id.localeCompare(b.id));
  const visible = unique.slice(0, 200);
  if (!visible.length && !groups.some(group => (group.kind === 'custom' || group.id.startsWith('group:manual:')))) return {positions: new Map(), groups: [], width: 800, height: 600, omitted: 0};
  const assignment = new Map<string, string>();
  for (const group of [...groups].sort((a, b) => a.id.localeCompare(b.id))) for (const id of group.record_ids || []) if (!assignment.has(id)) assignment.set(id, group.id);
  const buckets = new Map<string, LayoutInputNode[]>();
  for (const node of visible) {
    const key = node.group_id || assignment.get(node.id) || assignment.get(node.record_id || '') || 'group:ungrouped';
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key)!.push(node);
  }
  for (const group of groups) if ((group.kind === 'custom' || group.id.startsWith('group:manual:')) && !buckets.has(group.id)) buckets.set(group.id, []);
  const degree = new Map<string, number>();
  const ids = new Set(visible.map(node => node.id));
  for (const edge of edges) if (edge.source !== edge.target && ids.has(edge.source) && ids.has(edge.target)) {
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  }
  const sectionOrder = ['section:personal', 'section:work', 'section:knowledge', 'section:topics'];
  const meta = new Map(groups.map(group => [group.id, group]));
  const sectionId = (id: string) => meta.get(id)?.section_id || 'section:library';
  const rank = (id: string) => {const i = sectionOrder.indexOf(sectionId(id)); return i < 0 ? 4 : i;};
  const headerHeight = Math.max(HEADER, settings.headerHeight || 0);
  const groupGap = settings.minimumGroupWidth ? settings.overviewSpacing || Math.max(20, headerHeight * .1) : GROUP_GAP;
  const clusters = [...buckets].sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b)).map(([id, members]) => {
    members.sort((a, b) => (degree.get(b.id) || 0) - (degree.get(a.id) || 0) || a.id.localeCompare(b.id));
    const saved = settings.groupSizes?.[id] || settings.groups?.[id] || meta.get(id);
    const localHeader = saved?.width ? Math.max(HEADER, Math.min(headerHeight, saved.width * .55)) : headerHeight;
    const contentPadding = settings.minimumGroupWidth ? Math.max(20, Math.min(settings.overviewSpacing || localHeader * .1, (saved?.width || settings.minimumGroupWidth) * .065)) : PADDING;
    const defaultColumns = Math.min(settings.layout === 'columns' ? 2 : 6, Math.max(1, Math.ceil(Math.sqrt(members.length))));
    const overviewWidth = saved?.width ? 0 : settings.minimumGroupWidth || 0;
    const recordScale = Math.max(1, Math.min(settings.recordScale || 1, saved?.width ? (Math.max(540, saved.width) - contentPadding * 2) / RECORD_SLOT.width : Infinity));
    const slotWidth = RECORD_SLOT.width * recordScale, slotHeight = RECORD_SLOT.height * recordScale;
    const columns = overviewWidth ? Math.max(1, Math.min(Math.max(1, members.length), settings.layout === 'columns' ? 2 : 7, Math.floor((overviewWidth - contentPadding * 2) / slotWidth))) : saved?.width ? Math.max(1, Math.min(Math.max(1, members.length), 12, Math.floor((Math.max(540, saved.width) - contentPadding * 2) / slotWidth))) : defaultColumns;
    const rowSizes: number[] = [];
    let remaining = members.length;
    while (remaining > 0) {const capacity = settings.layout !== 'columns' && columns > 1 && rowSizes.length % 2 === 1 ? columns - 1 : columns; const count = Math.min(capacity, remaining); rowSizes.push(count); remaining -= count;}
    const minimumSize = {width: Math.max(540, columns * slotWidth + contentPadding * 2), height: localHeader + Math.max(1, rowSizes.length) * slotHeight + contentPadding};
    return {id, members, columns, rowSizes, minimumSize, recordScale, headerHeight: localHeader, contentPadding, width: Math.max(minimumSize.width, saved?.width || overviewWidth), height: Math.max(minimumSize.height, saved?.height || 0)};
  });
  const ratio = Number.isFinite(aspectRatio) ? Math.max(.4, Math.min(3, aspectRatio)) : 1.6;
  const positions = new Map<string, Point>();
  const bounds: GroupBounds[] = [];
  const sections: SectionBounds[] = [];
  const sectioned = groups.some(group => group.section_id);
  const sectionIds = [...new Set(clusters.map(cluster => sectioned ? sectionId(cluster.id) : 'section:library'))];
  const headingHeight = sectioned ? Math.max(SECTION_GAP, headerHeight * .2) : 0;
  type Cluster = typeof clusters[number];
  type Block = {id: string; width: number; height: number; placements: {cluster: Cluster; x: number; y: number}[]};
  const makeBlock = (id: string, members: Cluster[], columns: number): Block => {
    const cellWidth = Math.max(...members.map(cluster => cluster.width));
    const heights = Array.from({length: columns}, () => headingHeight);
    const placements = members.map(cluster => {
      const column = heights.indexOf(Math.min(...heights));
      const placement = {cluster, x: column * (cellWidth + groupGap), y: heights[column]};
      heights[column] += cluster.height + groupGap;
      return placement;
    });
    return {id, placements, width: columns * (cellWidth + groupGap) - groupGap, height: Math.max(...heights) - groupGap};
  };
  const choices = sectionIds.map(id => {
    const members = clusters.filter(cluster => !sectioned || sectionId(cluster.id) === id);
    const meanWidth = members.reduce((sum, cluster) => sum + cluster.width, 0) / members.length;
    const meanHeight = members.reduce((sum, cluster) => sum + cluster.height, 0) / members.length;
    let candidates: number[];
    if (!sectioned && members.length <= 4) candidates = [ratio < .8 ? 1 : settings.layout === 'columns' ? Math.min(3, members.length) : Math.max(1, Math.round(Math.sqrt(members.length * ratio * .8)))];
    else {
      const center = Math.max(1, Math.round(Math.sqrt(members.length * meanHeight / meanWidth * (sectioned ? 1.4 : ratio))));
      candidates = [...new Set([1, center - 1, center, center + 1, center + 2].filter(column => column >= 1 && column <= Math.min(members.length, 9)))];
      if (settings.layout === 'columns') candidates = [Math.min(3, members.length)];
    }
    return candidates.map(columns => makeBlock(id, members, columns));
  });
  // A bounded search compares the *final* viewport extent. A shelf cannot grow
  // arbitrarily wide just because every section happens to fit beside it.
  let variants: Block[][] = [[]];
  for (const options of choices) variants = variants.flatMap(previous => options.map(block => [...previous, block])).slice(0, 512);
  let best: {score: number; width: number; height: number; blocks: {block: Block; x: number; y: number}[]} | undefined;
  for (const variant of variants) {
    const targets = new Set<number>();
    for (let first = 0; first < variant.length; first++) {
      let width = 0;
      for (let last = first; last < variant.length; last++) {width += variant[last].width + (last > first ? groupGap : 0); targets.add(width);}
    }
    const area = variant.reduce((sum, block) => sum + block.width * block.height, 0);
    for (const factor of [.8, 1, 1.2]) targets.add(Math.sqrt(area * ratio) * factor);
    for (const target of targets) {
      let x = PADDING, y = PADDING, shelfHeight = 0, width = 0, height = 0;
      const placed = variant.map(block => {
        if (x > PADDING && x + block.width > target + PADDING + .001) {y += shelfHeight + groupGap; x = PADDING; shelfHeight = 0;}
        const placement = {block, x, y};
        width = Math.max(width, x + block.width + PADDING); height = Math.max(height, y + block.height + PADDING);
        x += block.width + groupGap; shelfHeight = Math.max(shelfHeight, block.height);
        return placement;
      });
      const empty = 1 - area / (width * height);
      const score = Math.max(width / ratio, height) * (1 + empty * .08);
      if (!best || score < best.score) best = {score, width, height, blocks: placed};
    }
  }
  for (const {block, x: blockX, y: blockY} of best!.blocks) {
    for (const {cluster, x: localX, y: localY} of block.placements) {
      const x = blockX + localX, y = blockY + localY;
      const width = cluster.width, height = cluster.height, {headerHeight, contentPadding} = cluster, bodyHeight = height - headerHeight - contentPadding;
      const rows = Math.max(1, cluster.rowSizes.length);
      cluster.members.forEach((node, nodeIndex) => {
        let row = 0, before = 0;
        while (before + cluster.rowSizes[row] <= nodeIndex) {before += cluster.rowSizes[row]; row++;}
        const col = nodeIndex - before, rowCount = cluster.rowSizes[row], cell = (width - contentPadding * 2) / cluster.columns;
        const stagger = settings.layout !== 'columns' ? (cluster.columns - rowCount) * cell / 2 : 0;
        // Seeded offsets retain the complete reserved card rectangle.
        const organic = settings.layout !== 'columns';
        const jitterX = organic ? (seededUnit(`${node.id}:x`) - .5) * 30 * cluster.recordScale : 0;
        const jitterY = organic ? (seededUnit(`${node.id}:y`) - .5) * 32 * cluster.recordScale : 0;
        positions.set(node.id, {x: x + contentPadding + cell * (col + .5) + stagger + jitterX, y: y + headerHeight + bodyHeight / rows * (row + .5) + jitterY});
      });
      bounds.push({id: cluster.id, x, y, width, height, headerHeight, recordScale: cluster.recordScale, contentPadding, colorIndex: meta.get(cluster.id)?.color_index ?? bounds.length, nodeIds: cluster.members.map(node => node.id), minimumSize: {width: 540, height: headerHeight + RECORD_SLOT.height * cluster.recordScale + contentPadding}});
    }
    if (sectioned) sections.push({id: block.id, label: groups.find(group => group.section_id === block.id)?.section_label || block.id.replace('section:', ''), x: blockX, y: blockY, width: block.width, height: block.height, headingHeight});
  }
  return {positions: new Map([...positions].sort(([a], [b]) => a.localeCompare(b))), groups: bounds, sections, width: best!.width, height: best!.height, omitted: unique.length - visible.length};
}
export function neighborsFor(id: string | null | undefined, edges: readonly LayoutInputEdge[]): Set<string> {
  if (!id) return new Set();
  const neighbors = new Set([id]);
  for (const edge of edges) {if (edge.source === id) neighbors.add(edge.target); if (edge.target === id) neighbors.add(edge.source);}
  return neighbors;
}

export type SharedTopicLink = LayoutInputEdge & {label:string; topic_ids:string[]};
/** Presentation links follow a real two-edge path through an existing saved
 * topic hub. They describe shared metadata, never evidence or claim support. */
export function sharedTopicLinks(selectedId:string|null|undefined, nodes:readonly (LayoutInputNode & {label?:string})[], edges:readonly LayoutInputEdge[]): SharedTopicLink[] {
  const byId=new Map(nodes.map(node=>[node.id,node]));
  if(!selectedId||!byId.has(selectedId)||byId.get(selectedId)?.type==='topic')return [];
  const topics=new Map<string,Set<string>>();
  for(const edge of edges){
    if(edge.kind!=='topic')continue;
    const source=byId.get(edge.source),target=byId.get(edge.target);
    if(!source||!target)continue;
    const topic=source.type==='topic'?source:target.type==='topic'?target:null;
    const record=source.type!=='topic'?source:target.type!=='topic'?target:null;
    if(!topic||!record)continue;
    if(!topics.has(topic.id))topics.set(topic.id,new Set());
    topics.get(topic.id)!.add(record.id);
  }
  const shared=new Map<string,string[]>();
  for(const [topicId,records] of topics)if(records.has(selectedId))for(const recordId of records){
    if(recordId===selectedId)continue;
    if(!shared.has(recordId))shared.set(recordId,[]);
    shared.get(recordId)!.push(topicId);
  }
  return [...shared].sort(([a],[b])=>a.localeCompare(b)).map(([target,topicIds])=>{
    topicIds.sort();
    return {id:`shared:${selectedId}:${target}`,source:selectedId,target,kind:'topic',topic_ids:topicIds,
      label:`Shared topic: ${topicIds.map(id=>byId.get(id)?.label||id).join(', ')}`};
  });
}

export type GroupSourceLink = {source:string;target:string;count:number};
/** Aggregate only actual source edges between groups; internal citations remain
 * inside their group and shared topics are never promoted to source evidence. */
export function groupSourceLinks(groups:readonly GroupBounds[],edges:readonly LayoutInputEdge[]):GroupSourceLink[]{
  const membership=new Map(groups.flatMap(group=>group.nodeIds.map(id=>[id,group.id] as const)));
  const links=new Map<string,GroupSourceLink>();
  for(const edge of edges){if(edge.kind!=='source')continue;const a=membership.get(edge.source),b=membership.get(edge.target);if(!a||!b||a===b)continue;const [source,target]=[a,b].sort();const key=JSON.stringify([source,target]);const existing=links.get(key);if(existing)existing.count++;else links.set(key,{source,target,count:1});}
  return [...links.values()].sort((a,b)=>a.source.localeCompare(b.source)||a.target.localeCompare(b.target));
}


/** Overview calibration solves width and header together, then packs against
 * the actual viewport. Saved group sizes remain authoritative inputs. */
export function layoutOverview(nodes: readonly LayoutInputNode[], edges: readonly LayoutInputEdge[], groups: readonly LayoutInputGroup[], size: {width: number; height: number}, settings: GraphBoardSettings = {}): LayoutResult {
  const usableWidth = Math.max(1, size.width - 42), usableHeight = Math.max(1, size.height - 86), aspect = usableWidth / usableHeight;
  const count = Math.max(1, new Set([...nodes.map(node => node.group_id || 'group:ungrouped'), ...groups.filter(group => group.id.startsWith('group:manual:')).map(group => group.id)]).size);
  const desiredWidth = Math.min(240, Math.max(130, Math.sqrt(usableWidth * usableHeight / count) * 1.25));
  const desiredHeader = 46;
  let best: {score: number; result: LayoutResult} | undefined;
  for (const headerHeight of [280, 360, 460, 600, 740]) for (const recordScale of [2, 3, 4, 5, 6]) for (const proportion of [1.6, 2.2, 3, 4]) {
    const minimumGroupWidth = Math.max(1112, headerHeight * proportion);
    const first = layoutGraph(nodes, edges, groups, aspect, {...settings, headerHeight, minimumGroupWidth, recordScale});
    if (!first.groups.length) return first;
    const firstScale = Math.min(1, usableWidth / first.width, usableHeight / first.height);
    // One bounded second pass allocates near-eleven-pixel spacing at fit.
    // Camera zoom never participates in layout or saved dimensions.
    const result = layoutGraph(nodes, edges, groups, aspect, {...settings, headerHeight, minimumGroupWidth, recordScale, overviewSpacing: 11 / firstScale});
    const scale = Math.min(1, usableWidth / result.width, usableHeight / result.height);
    const standard = result.groups.filter(group => !(settings.groupSizes?.[group.id]?.width || settings.groups?.[group.id]?.width || groups.find(meta => meta.id === group.id)?.width));
    const widths = (standard.length ? standard : result.groups).map(group => group.width * scale);
    const minimumWidth = Math.min(...widths), header = headerHeight * scale;
    const penalty = Math.max(0, desiredWidth - minimumWidth) / desiredWidth * 4 + Math.max(0, desiredHeader - header) / desiredHeader * 6;
    const excess = Math.max(0, header - desiredHeader * 1.05) / desiredHeader * 2;
    const bodyResolution = Math.max(0, .14 - scale * recordScale) / .14 * 3;
    const score = penalty + excess + bodyResolution;
    if (!best || score < best.score) best = {score, result};
  }
  return best!.result;
}
