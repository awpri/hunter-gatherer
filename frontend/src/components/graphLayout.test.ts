import { describe, expect, it } from "vitest";
import { layoutGraph, layoutOverview, neighborsFor, sharedTopicLinks, groupSourceLinks, type LayoutInputNode, RECORD_CARD, minimumGroupSize, organizeGraph } from "./graphLayout";

describe("connection map layout", () => {
  const nodes: LayoutInputNode[] = [{ id: "memory:b", type: "memory" }, { id: "memory:a", type: "memory" }, { id: "topic:work", type: "topic" }, { id: "document:source", type: "document" }];
  const edges = [{ id: "category", source: "memory:a", target: "topic:work", kind: "topic" as const }, { id: "citation", source: "memory:a", target: "document:source", kind: "source" as const }];
  it("is stable across API ordering and keeps every isolated node", () => {
    const original = layoutGraph(nodes, edges);
    const reordered = layoutGraph([...nodes].reverse(), [...edges].reverse());
    expect([...original.positions]).toEqual([...reordered.positions]);
    expect(original.positions.size).toBe(4);
    expect(original.positions.has("memory:b")).toBe(true);
    for (const point of original.positions.values()) {
      expect(Number.isFinite(point.x) && Number.isFinite(point.y)).toBe(true);
      expect(point.x).toBeGreaterThan(0);
      expect(point.x).toBeLessThan(original.width);
      expect(point.y).toBeGreaterThan(0);
      expect(point.y).toBeLessThan(original.height);
    }
  });
  it("does not create extra nodes for broken links or duplicates", () => {
    const result = layoutGraph([...nodes, nodes[0]], [...edges, { id: "broken", source: "memory:a", target: "missing", kind: "topic" }, { id: "self", source: "memory:a", target: "memory:a", kind: "source" }]);
    expect(result.positions.size).toBe(4);
    expect(result.positions.has("missing")).toBe(false);
  });
  it("returns safe dimensions for empty input and bounds large graphs", () => {
    expect(layoutGraph([], []).positions.size).toBe(0);
    const result = layoutGraph(Array.from({ length: 301 }, (_, index) => ({ id: `memory:${index}`, type: "memory" })), []);
    expect(result.positions.size).toBe(200);
    expect(result.omitted).toBe(101);
    for (const point of result.positions.values()) expect(Number.isFinite(point.x) && Number.isFinite(point.y)).toBe(true);
  });
  it('stacks groups on narrow canvases while using two columns on desktop',()=>{
    const nodes=Array.from({length:12},(_,i)=>({id:`memory:${i}`,type:'memory' as const,group_id:`group:${Math.floor(i/3)}`}));
    const phone=layoutGraph(nodes,[],[],.5),desktop=layoutGraph(nodes,[],[],1.6);
    expect(new Set(phone.groups.map(group=>group.x)).size).toBe(1);
    expect(new Set(desktop.groups.map(group=>group.x)).size).toBe(2);
    expect(phone.positions.size).toBe(12);
    for(const point of phone.positions.values())expect(Number.isFinite(point.x)&&Number.isFinite(point.y)).toBe(true);
  });
  it('reserves a header above records and packs overview groups to the viewport shape',()=>{
    const nodes=Array.from({length:63},(_,i)=>({id:`memory:${String(i).padStart(3,'0')}`,type:'memory' as const,group_id:`group:${Math.floor(i/3)}`}));
    for(const aspect of [1,1.8]){
      const result=layoutGraph(nodes,[],[],aspect);
      expect(result.groups).toHaveLength(21);
      for(const group of result.groups)for(const id of group.nodeIds){
        expect(result.positions.get(id)!.y-group.y).toBeGreaterThan(80);
      }
      expect(result.width/result.height).toBeGreaterThanOrEqual(aspect*.75);
    }
  });
  it("focuses only directly recorded links, never shared-hub inference", () => {
    const linked = [...edges, { id: "other", source: "memory:b", target: "topic:work", kind: "topic" as const }];
    expect([...neighborsFor("memory:a", linked)].sort()).toEqual(["document:source", "memory:a", "topic:work"]);
    expect(neighborsFor(null, linked).size).toBe(0);
  });
  it("separates automatic groups while keeping all 150 isolated records", () => {
    const grouped = Array.from({length:150}, (_,i) => ({id:`memory:${String(i).padStart(3,'0')}`,type:'memory' as const,group_id:`group:${Math.floor(i/30)}`}));
    const groups = Array.from({length:5},(_,i)=>({id:`group:${i}`,label:`Group ${i}`,color_index:i,record_ids:grouped.filter(node=>node.group_id===`group:${i}`).map(node=>node.id)}));
    const layout = layoutGraph(grouped,[],groups);
    expect(layout.positions.size).toBe(150);
    expect(layout.groups).toHaveLength(5);
    expect([...layout.positions]).toEqual([...layoutGraph([...grouped].reverse(),[],[...groups].reverse()).positions]);
    for (const group of layout.groups) {
      for (const id of group.nodeIds) {
        const point = layout.positions.get(id)!;
        expect(Number.isFinite(point.x)&&Number.isFinite(point.y)).toBe(true);
        expect(point.x).toBeGreaterThan(group.x);
        expect(point.x).toBeLessThan(group.x+group.width);
        expect(point.y).toBeGreaterThan(group.y);
        expect(point.y).toBeLessThan(group.y+group.height);
      }
      for(const other of layout.groups.filter(other=>other.id!==group.id)) {
        expect(group.x+group.width<other.x||other.x+other.width<group.x||group.y+group.height<other.y||other.y+other.height<group.y).toBe(true);
      }
    }
  });

});

describe('shared topic presentation',()=>{
  it('follows only recorded topic paths and never treats shared categories as sources',()=>{
    const nodes=[{id:'a',type:'memory' as const},{id:'b',type:'memory' as const},{id:'c',type:'document' as const},{id:'d',type:'memory' as const},{id:'topic:t',type:'topic' as const,label:'Research'},{id:'topic:u',type:'topic' as const,label:'Writing'}];
    const edges=[{id:'1',source:'a',target:'topic:t',kind:'topic' as const},{id:'2',source:'topic:t',target:'b',kind:'topic' as const},{id:'3',source:'c',target:'topic:t',kind:'topic' as const},{id:'4',source:'a',target:'topic:u',kind:'topic' as const},{id:'5',source:'b',target:'topic:u',kind:'topic' as const},{id:'6',source:'a',target:'d',kind:'source' as const},{id:'7',source:'missing',target:'topic:t',kind:'topic' as const}];
    const links=sharedTopicLinks('a',nodes,edges);
    expect(links.map(link=>link.target)).toEqual(['b','c']);
    expect(links[0].label).toBe('Shared topic: Research, Writing');
    expect(links.every(link=>link.kind==='topic')).toBe(true);
    expect(links).toEqual(sharedTopicLinks('a',[...nodes].reverse(),[...edges].reverse()));
    expect([...neighborsFor('a',links)].sort()).toEqual(['a','b','c']);
    expect(sharedTopicLinks('topic:t',nodes,edges)).toEqual([]);
    expect(sharedTopicLinks('d',nodes,edges)).toEqual([]);
  });
});

describe('overview source connections',()=>{
  it('counts only real cross-group citations between visible records',()=>{
    const nodes=[{id:'a',type:'memory' as const,group_id:'A'},{id:'b',type:'document' as const,group_id:'B'},{id:'c',type:'memory' as const,group_id:'A'}];
    const groups=layoutGraph(nodes,[]).groups;
    const edges=[{id:'1',source:'a',target:'b',kind:'source' as const},{id:'2',source:'b',target:'c',kind:'source' as const},{id:'3',source:'a',target:'c',kind:'source' as const},{id:'4',source:'a',target:'b',kind:'topic' as const},{id:'5',source:'hidden',target:'b',kind:'source' as const}];
    expect(groupSourceLinks(groups,edges)).toEqual([{source:'A',target:'B',count:2}]);
    expect(groupSourceLinks(groups.slice(0,1),edges)).toEqual([]);
    expect(groupSourceLinks(groups,[...edges].reverse())).toEqual(groupSourceLinks(groups,edges));
  });
});


describe('spatial sections',()=>{
 it('packs disjoint section areas in canonical order and preserves their membership',()=>{
  const sectionIds=['section:personal','section:work','section:knowledge','section:topics'];
  const groups=sectionIds.flatMap((section_id,i)=>Array.from({length:i===2?1:3},(_,j)=>({id:`group:${i}:${j}`,section_id,section_label:section_id.slice(8)})));
  const nodes=groups.flatMap(g=>Array.from({length:3},(_,i)=>({id:`${g.id}:node:${i}`,type:'memory' as const,group_id:g.id})));
  for(const aspect of [1,1.8]){const graph=layoutGraph(nodes,[],groups,aspect);expect(graph.sections?.map(s=>s.id)).toEqual(sectionIds);
   for(const section of graph.sections!){const members=graph.groups.filter(g=>groups.find(meta=>meta.id===g.id)?.section_id===section.id);for(const g of members){expect(g.x).toBeGreaterThanOrEqual(section.x);expect(g.x+g.width).toBeLessThanOrEqual(section.x+section.width+.001);expect(g.y).toBeGreaterThan(section.y);expect(g.y+g.height).toBeLessThanOrEqual(section.y+section.height+.001);}}
   for(let i=0;i<graph.sections!.length;i++)for(let j=i+1;j<graph.sections!.length;j++){const a=graph.sections![i],b=graph.sections![j];expect(a.x+a.width<b.x||b.x+b.width<a.x||a.y+a.height<b.y||b.y+b.height<a.y).toBe(true);}
  }
 });
});

describe('readable card slots and user group sizes',()=>{
 const nodes=Array.from({length:90},(_,i)=>({id:`memory:${String(i).padStart(3,'0')}`,type:'memory' as const,group_id:`group:${Math.floor(i/30)}`}));
 it('reserves room for full cards, drift, and preview fans in every layout',()=>{
  for(const kind of ['board','constellation','columns'] as const){const graph=layoutGraph(nodes,[],[],1.6,{layout:kind});
   for(const group of graph.groups){const points=group.nodeIds.map(id=>graph.positions.get(id)!);
    for(const p of points){expect(p.x-RECORD_CARD.width/2-22).toBeGreaterThan(group.x);expect(p.x+RECORD_CARD.width/2+22).toBeLessThan(group.x+group.width);expect(p.y-RECORD_CARD.height/2-22).toBeGreaterThan(group.y+214);expect(p.y+RECORD_CARD.height/2+22).toBeLessThan(group.y+group.height);}
    for(let i=0;i<points.length;i++)for(let j=i+1;j<points.length;j++){const a=points[i],b=points[j];expect(Math.abs(a.x-b.x)>RECORD_CARD.width+22||Math.abs(a.y-b.y)>RECORD_CARD.height+22).toBe(true);}
   }
  }
 });
 it('clamps unsafe saved sizes and repacks enlarged groups without overlap',()=>{
  const graph=layoutGraph(nodes,[],[],1.6,{groups:{'group:0':{width:2200,height:1500},'group:1':{width:4,height:3}}});
  const a=graph.groups.find(group=>group.id==='group:0')!,b=graph.groups.find(group=>group.id==='group:1')!;expect(a.width).toBe(2200);expect(a.height).toBeGreaterThanOrEqual(1500);expect(b.width).toBeGreaterThanOrEqual(minimumGroupSize(30,1).width);
  for(let i=0;i<graph.groups.length;i++)for(let j=i+1;j<graph.groups.length;j++){const a=graph.groups[i],b=graph.groups[j];expect(a.x+a.width<b.x||b.x+b.width<a.x||a.y+a.height<b.y||b.y+b.height<a.y).toBe(true);}
 });
 it('renders empty manual groups before records are added',()=>{
  const graph=layoutGraph([],[],[{id:'group:custom',label:'New space',kind:'custom'}]);expect(graph.groups).toHaveLength(1);expect(graph.positions.size).toBe(0);expect(graph.width).toBeGreaterThan(500);
 });
 it('reorganizes by real source files without introducing inferred evidence',()=>{
  const nodes=[{id:'a',type:'memory' as const},{id:'b',type:'memory' as const},{id:'file',type:'document' as const,source_path:'/docs/Research/report.pdf'}];
  const edges=[{id:'citation',source:'a',target:'file',kind:'source' as const},{id:'topic',source:'b',target:'file',kind:'topic' as const}];
  const organized=organizeGraph(nodes,edges,[],'source');expect(organized.nodes[0].group_id).toBe(organized.nodes[2].group_id);expect(organized.nodes[1].group_id).not.toBe(organized.nodes[2].group_id);expect(edges).toHaveLength(2);
 });
});

describe('responsive group resizing',()=>{
 it('reflows dense card slots when widened or narrowed',()=>{
  const nodes=Array.from({length:30},(_,i)=>({id:`node:${i}`,type:'memory' as const,group_id:'A'}));
  const narrow=layoutGraph(nodes,[],[],1.6,{groupSizes:{A:{width:600,height:400}}}),wide=layoutGraph(nodes,[],[],1.6,{groupSizes:{A:{width:2800,height:400}}});
  expect(narrow.groups[0].width).toBe(600);expect(wide.groups[0].width).toBe(2800);expect(narrow.groups[0].height).toBeGreaterThan(wide.groups[0].height);
  for(const graph of [narrow,wide]){const points=[...graph.positions.values()];for(let i=0;i<points.length;i++)for(let j=i+1;j<points.length;j++)expect(Math.abs(points[i].x-points[j].x)>RECORD_CARD.width+22||Math.abs(points[i].y-points[j].y)>RECORD_CARD.height+22).toBe(true);}
 });
});

// Shape-only fixtures model uneven section membership without library content.
describe('viewport overview planning', () => {
  const counts = [4, 5, 3, 6, 9, 1, 14, 11, 10, 13, 1, 4, 1, 1, 2, 1, 2, 7];
  function fixture(count: number, uneven = false) {
    const groups = Array.from({length: count}, (_, index) => ({id: `group:${index}`, section_id: `section:${uneven ? index < 3 ? 0 : index < 5 ? 1 : index < 8 ? 2 : 3 : index % 4}`}));
    const nodes: LayoutInputNode[] = groups.flatMap((group, index) => Array.from({length: counts[index % counts.length]}, (_, record) => ({id: `${group.id}:record:${record}`, type: 'document', group_id: group.id})));
    return {groups, nodes};
  }
  for (const count of [4, 8, 18, 30]) it(`keeps ${count} groups readable with calibrated spacing and no card collisions`, () => {
    const {groups, nodes} = fixture(count);
    const result = layoutOverview(nodes, [], groups, {width: 1440, height: 900});
    const scale = Math.min(1, 1398 / result.width, 814 / result.height);
    for (const group of result.groups) {
      expect(group.width * scale).toBeGreaterThan(150);
      expect(group.contentPadding * scale).toBeGreaterThan(8);
      expect(group.contentPadding * scale).toBeLessThan(14);
      for (const id of group.nodeIds) {
        const point = result.positions.get(id)!;
        expect(point.x - RECORD_CARD.width / 2 - 11).toBeGreaterThan(group.x);
        expect(point.x + RECORD_CARD.width / 2 + 11).toBeLessThan(group.x + group.width);
        expect(point.y - RECORD_CARD.height / 2 - 11).toBeGreaterThan(group.y + group.headerHeight);
        expect(point.y + RECORD_CARD.height / 2 + 11).toBeLessThan(group.y + group.height);
      }
      for (const other of result.groups.filter(other => other.id !== group.id)) expect(group.x + group.width < other.x || other.x + other.width < group.x || group.y + group.height < other.y || other.y + other.height < group.y).toBe(true);
    }
  });
  it('balances the dominant ten-group section and preserves narrow saved sizes', () => {
    const {groups, nodes} = fixture(18, true);
    const size = {width: 1190, height: 650};
    const result = layoutOverview(nodes, [], groups, size);
    const scale = Math.min(1, 1148 / result.width, 564 / result.height);
    expect(Math.min(...result.groups.map(group => group.width * scale))).toBeGreaterThan(130);
    expect(result.width / result.height).toBeGreaterThan(1);
    expect([...result.positions]).toEqual([...layoutOverview([...nodes].reverse(), [], [...groups].reverse(), size).positions]);
    const saved = layoutOverview(nodes, [], groups, size, {groups: {[groups[0].id]: {width: 628}}}).groups.find(group => group.id === groups[0].id)!;
    expect(saved.width).toBe(628);
    expect(saved.headerHeight).toBeLessThanOrEqual(628 * .55);
    expect(saved.contentPadding).toBeLessThanOrEqual(628 * .065);
  });
});

 describe('organic overview record geometry', () => {
  it('keeps scaled glyphs substantial and full cards collision-free for the real library shape', () => {
    const counts = [4,5,3,6,9,1,14,11,10,13,1,4,1,1,2,1,2,7];
    const groups = counts.map((_, i) => ({id: `group:${i}`, section_id: `section:${i < 3 ? 0 : i < 5 ? 1 : i < 8 ? 2 : 3}`}));
    const nodes = groups.flatMap((group, i) => Array.from({length: counts[i]}, (_, j) => ({id: `${group.id}:${j}`, type: 'memory' as const, group_id: group.id})));
    const graph = layoutOverview(nodes, [], groups, {width:1190,height:650});
    const fit = Math.min(1,1148 / graph.width,564 / graph.height);
    for (const group of graph.groups) {
      expect(100 * group.recordScale * fit).toBeGreaterThan(10);
      expect(group.headerHeight * fit).toBeGreaterThan(38);
      expect(group.headerHeight * fit).toBeLessThan(58);
      const points = group.nodeIds.map(id => graph.positions.get(id)!);
      for (let i = 0; i < points.length; i++) for (let j = i + 1; j < points.length; j++) {
        expect(Math.abs(points[i].x - points[j].x) > (RECORD_CARD.width + 22) * group.recordScale || Math.abs(points[i].y - points[j].y) > (RECORD_CARD.height + 22) * group.recordScale).toBe(true);
      }
    }
    expect(new Set([...graph.positions.values()].map(point => point.y)).size).toBeGreaterThan(30);
  });
 });
