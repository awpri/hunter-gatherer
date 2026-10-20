import {beforeEach,describe,expect,it,vi} from 'vitest';
import type {ReactElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
// The project has no DOM test runtime. Exercise effects and cleanup explicitly,
// and render the resulting real React element tree for content/escaping checks.
const hooks=vi.hoisted(()=>({index:0,slots:[] as any[],effects:[] as (()=>void)[],cleanups:[] as (()=>void)[]}));
vi.mock('react',async()=>({...await vi.importActual<typeof import('react')>('react'),
 useState:(initial:any)=>{const i=hooks.index++;if(!(i in hooks.slots))hooks.slots[i]=typeof initial==='function'?initial():initial;return [hooks.slots[i],(value:any)=>{hooks.slots[i]=typeof value==='function'?value(hooks.slots[i]):value;}];},
 useRef:(initial:any)=>{const i=hooks.index++;return hooks.slots[i]??(hooks.slots[i]={current:initial});},
 useEffect:(effect:()=>unknown,deps:any[])=>{const i=hooks.index++;const previous=hooks.slots[i];if(!previous||deps.some((value,index)=>value!==previous[index])){hooks.slots[i]=deps;hooks.effects.push(()=>{hooks.cleanups[i]?.();const cleanup=effect();hooks.cleanups[i]=typeof cleanup==='function'?cleanup as ()=>void:()=>{};});}},
}));
vi.mock('../lib/libraryApi',()=>({libraryApi:{request:vi.fn()},errorMessage:(error:Error)=>error.message,isAbort:(error:Error)=>error.name==='AbortError'}));
import {libraryApi} from '../lib/libraryApi';
import {TableCellInspector} from './TableCellInspector';
import {packet,cell} from './tableCellFixtures';
const props={document:{document_id:'doc-1',title:'Fixture',version:3,media_type:'application/pdf',created_at:0,is_latest:false},chunkId:'chunk-1',includePrivate:false,onOriginalPage:undefined as ((page?:number)=>void)|undefined};
function elements(node:unknown):ReactElement<any>[]{if(Array.isArray(node))return node.flatMap(elements);if(!node||typeof node!=='object'||!('props' in node))return [];const element=node as ReactElement<any>;return [element,...elements(element.props.children)];}
function render(){hooks.index=0;const tree=TableCellInspector(props);hooks.effects.splice(0).forEach(effect=>effect());return {tree,nodes:elements(tree),html:renderToStaticMarkup(tree)};}
function action(text:string){return render().nodes.find(node=>node.type==='button'&&node.props.children===text)!.props;}
async function inspect(){await action('Inspect table').onClick();return render();}
beforeEach(()=>{hooks.cleanups.forEach(cleanup=>cleanup?.());hooks.index=0;hooks.slots=[];hooks.effects=[];hooks.cleanups=[];props.document={document_id:'doc-1',title:'Fixture',version:3,media_type:'application/pdf',created_at:0,is_latest:false};props.chunkId='chunk-1';props.includePrivate=false;props.onOriginalPage=undefined;vi.mocked(libraryApi.request).mockReset();vi.mocked(libraryApi.request).mockImplementation(async(_path,query)=>packet(Number(query?.table_cell_offset)) as any);});
describe('Explicit saved table-cell inspection',()=>{
 it('makes no request until inspection and uses exact chunk with bounded private scope',async()=>{
  expect(render().html).toContain('Inspect table');expect(libraryApi.request).not.toHaveBeenCalled();props.includePrivate=true;
  const view=await inspect();expect(libraryApi.request).toHaveBeenCalledWith('chunks/chunk-1/window',{include_sensitive:true,before:0,after:0,max_tokens:4096,table_cell_offset:0,table_cell_limit:32},undefined,undefined,expect.any(AbortSignal));
  expect(view.html).toContain('Model predictions from version 3');expect(view.html).not.toContain('EXACT SOURCE QUOTE');expect(view.html).toContain('include_sensitive=true');expect(view.html).toContain('Download original');expect(view.html).not.toContain('#page=');
 });
 it('follows server cursors, restores exact prior offsets and starts at first cells',async()=>{
  await inspect();await action('Next cells').onClick();expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[1]?.table_cell_offset).toBe(32);
  await action('Next cells').onClick();expect(action('Next cells').disabled).toBe(true);
  await action('Previous cells').onClick();expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[1]?.table_cell_offset).toBe(32);
  await action('First cells').onClick();expect(action('Previous cells').disabled).toBe(true);expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[1]?.table_cell_offset).toBe(0);
 });
 it('retries a blocked cursor at a larger budget without inventing forward progress',async()=>{
  vi.mocked(libraryApi.request).mockResolvedValue(packet(0,[],{budget_blocked:true,next_cell_offset:0}) as any);
  expect((await inspect()).html).toContain('blocked remaining cells at offset 0');expect(action('Next cells').disabled).toBe(true);
  await action('Retry with larger budget').onClick();expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[1]?.max_tokens).toBe(8192);expect(vi.mocked(libraryApi.request).mock.calls.at(-1)?.[1]?.table_cell_offset).toBe(0);
 });
 it('shows raw overlaps, invalid spans, empty text and oversized omissions with no clean badge',async()=>{
  vi.mocked(libraryApi.request).mockResolvedValue(packet(0,[cell(1,{text:'<script>bad</script>',row_span:-1,column_header:true}),cell(2,{text:''})],{observations:['overlapping_cells','inconsistent_spans'],omitted_cells:[{ordinal:3,reason:'cell_size_limit'}]}) as any);
  const {html}=await inspect();expect(html).toContain('overlapping cells');expect(html).toContain('Spans -1');expect(html).toContain('column header true');expect(html).toContain('Empty model text');expect(html).toContain('Cell 3 omitted');expect(html).toContain('&lt;script&gt;');expect(html).not.toContain('<script>');expect(html).not.toContain('<table');
 });
 it('distinguishes empty, legacy, corrupt, metadata omission and missing summaries',async()=>{
  for(const [result,message] of [[packet(0,[],{total_cell_count:0,next_cell_offset:null}),'contains no cells'],[{chunks:[],table_structure_omitted:{reason:'not_saved'}},'No saved table cells'],[{chunks:[],table_structure_omitted:{reason:'invalid_metadata'}},'invalid metadata'],[packet(0,[],{status:'omitted',omission_reason:'snapshot_bytes_exceeded'}),'metadata was omitted'],[{chunks:[]},'No table-cell summary'],[packet(0,[],{version:8}),'inconsistent or malformed']] as const){
   vi.mocked(libraryApi.request).mockResolvedValue(result as any);expect((await inspect()).html).toContain(message);
  }
 });
 it('keeps zero observations explicitly unverified',async()=>{expect((await inspect()).html).toContain('does not establish correct extraction');});
 it.each(['privacy','chunk','version','document'])('aborts %s scope changes and ignores late private results before and after effects',async(change)=>{
  props.includePrivate=true;let resolve!:(value:any)=>void;vi.mocked(libraryApi.request).mockImplementation(()=>new Promise(done=>{resolve=done;}) as any);
  const request=action('Inspect table').onClick();const signal=vi.mocked(libraryApi.request).mock.calls.at(-1)![4]!;
  if(change==='privacy')props.includePrivate=false;if(change==='chunk')props.chunkId='other-chunk';if(change==='version')props.document={...props.document,version:4};if(change==='document')props.document={...props.document,document_id:'other-doc'};
  const view=render();expect(signal.aborted).toBe(true);expect(view.html).not.toContain('Raw 1');resolve(packet());await request;expect(render().html).not.toContain('Raw 1');
 });
 it('hides already loaded private cells synchronously when privacy changes',async()=>{
  props.includePrivate=true;await inspect();props.includePrivate=false;hooks.index=0;
  const beforeEffects=TableCellInspector(props);expect(renderToStaticMarkup(beforeEffects)).not.toContain('Raw 1');
 });
 it('cancels on unmount and ignores failed or superseded requests',async()=>{
  let reject!:(value:any)=>void;vi.mocked(libraryApi.request).mockImplementation(()=>new Promise((_done,fail)=>{reject=fail;}) as any);
  const request=action('Inspect table').onClick(),signal=vi.mocked(libraryApi.request).mock.calls.at(-1)![4]!;hooks.cleanups.forEach(cleanup=>cleanup?.());expect(signal.aborted).toBe(true);reject(new Error('Private failure'));await request;expect(render().html).not.toContain('Private failure');
 });
 it('ignores superseded pages whose transport completes out of order',async()=>{
  const resolvers:((value:any)=>void)[]=[];vi.mocked(libraryApi.request).mockImplementation(()=>new Promise(done=>resolvers.push(done)) as any);
  const first=action('Inspect table').onClick(),firstSignal=vi.mocked(libraryApi.request).mock.calls.at(-1)![4]!;
  const second=action('Inspect table').onClick();expect(firstSignal.aborted).toBe(true);
  resolvers[1](packet(0,[cell(1,{text:'CURRENT RESULT'})]));await second;
  resolvers[0](packet(0,[cell(1,{text:'STALE PRIVATE RESULT'})]));await first;
  expect(render().html).toContain('CURRENT RESULT');expect(render().html).not.toContain('STALE PRIVATE RESULT');
 });
 it('reports a read failure and permits retry',async()=>{
  vi.mocked(libraryApi.request).mockRejectedValue(new Error('Unavailable'));expect((await inspect()).html).toContain('Unavailable');expect(action('Inspect table').disabled).toBe(false);
 });
 it('uses the page-free fallback when the exact anchor page is unknown',async()=>{
  const raw=packet();raw.chunks[0].location={};vi.mocked(libraryApi.request).mockResolvedValue(raw as any);expect((await inspect()).html).toContain('original?include_sensitive=false');expect(render().html).not.toContain('#page=');
 });
});


describe('Inline original-page table action',()=>{
 it('opens the exact table anchor page through its callback without downloading or fetching pixels',async()=>{
  const open=vi.fn();props.onOriginalPage=open;const view=await inspect();const requests=vi.mocked(libraryApi.request).mock.calls.length;
  const button=view.nodes.find(node=>node.type==='button'&&Array.isArray(node.props.children)&&node.props.children[0]==='Original page')!;button.props.onClick();
  expect(open).toHaveBeenCalledWith(7);expect(view.html).toContain('Page 7');expect(view.nodes.some(node=>node.type==='a')).toBe(false);expect(view.html).not.toContain('#page=');expect(libraryApi.request).toHaveBeenCalledTimes(requests);
 });
 it('does not trust a page from another source version or an invalid page number',async()=>{
  const open=vi.fn();props.onOriginalPage=open;
  for(const change of [{version:4},{location:{page:0}}]){const raw=packet();Object.assign(raw.chunks[0],change);vi.mocked(libraryApi.request).mockResolvedValue(raw as any);const view=await inspect();view.nodes.find(node=>node.type==='button'&&Array.isArray(node.props.children)&&node.props.children[0]==='Original page')!.props.onClick();expect(open).toHaveBeenLastCalledWith(undefined);expect(view.html).not.toContain('Page 7');}
 });
});
