import {beforeEach,describe,expect,it,vi} from 'vitest';
import type {ReactElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {imagePacket} from '../lib/pageImageFixtures';
const hooks=vi.hoisted(()=>({index:0,slots:[] as any[],effects:[] as (()=>void)[],cleanups:[] as (()=>void)[]}));
vi.mock('react',async()=>({...await vi.importActual<typeof import('react')>('react'),
 useState:(initial:any)=>{const i=hooks.index++;if(!(i in hooks.slots))hooks.slots[i]=typeof initial==='function'?initial():initial;return [hooks.slots[i],(value:any)=>{hooks.slots[i]=typeof value==='function'?value(hooks.slots[i]):value;}];},
 useRef:(initial:any)=>{const i=hooks.index++;return hooks.slots[i]??(hooks.slots[i]={current:initial});},
 useEffect:(effect:()=>unknown,deps:any[])=>{const i=hooks.index++;const previous=hooks.slots[i];if(!previous||deps.some((value,index)=>value!==previous[index])){hooks.slots[i]=deps;hooks.effects.push(()=>{hooks.cleanups[i]?.();const cleanup=effect();hooks.cleanups[i]=typeof cleanup==='function'?cleanup as ()=>void:()=>{};});}},
}));
vi.mock('../lib/libraryApi',()=>({libraryApi:{request:vi.fn(),contextVersion:0},errorMessage:(error:Error)=>error.message,isAbort:(error:Error)=>error.name==='AbortError'}));
import {libraryApi} from '../lib/libraryApi';
import {VisualRegionReader} from './VisualRegionReader';
import type {VisualRegion} from '../lib/visualRegions';
const identity='00000000-0000-4000-8000-000000000001';
function region():VisualRegion{return {record_id:identity,citation:`[region:${identity}]`,description:'A <drawn> bag',verification:'unverified',source_reference:{...imagePacket(2).source_reference,bbox:[.1,.2,.7,.8]}};}
const props={region:region(),includePrivate:false,select:vi.fn(),close:vi.fn(),back:vi.fn()};
function packet(view='region'){return {...imagePacket(2),citation:props.region.citation,region_reference:props.region.source_reference,view};}
function elements(node:unknown):ReactElement<any>[]{if(Array.isArray(node))return node.flatMap(elements);if(!node||typeof node!=='object'||!('props' in node))return [];const element=node as ReactElement<any>;return [element,...elements(element.props.children)];}
function render(effects=true){hooks.index=0;const tree=VisualRegionReader(props);if(effects)hooks.effects.splice(0).forEach(effect=>effect());return {nodes:elements(tree),html:renderToStaticMarkup(tree)};}
function action(label:string){return render().nodes.find(node=>node.type==='button'&&(node.props.children===label||node.props['aria-label']===label))!.props;}
async function settle(){for(let i=0;i<8;i++)await Promise.resolve();return render();}
beforeEach(()=>{hooks.cleanups.forEach(fn=>fn?.());hooks.index=0;hooks.slots=[];hooks.effects=[];hooks.cleanups=[];props.region=region();props.includePrivate=false;props.select.mockReset();props.close.mockReset();props.back.mockReset();(libraryApi as any).contextVersion=0;vi.mocked(libraryApi.request).mockReset();vi.mocked(libraryApi.request).mockImplementation(async(_path,query)=>packet(String(query?.view)) as any);});
describe('Exact image region reader',()=>{
 it('opens exact crop, escapes unverified description, and switches to its full page',async()=>{
  render();let view=await settle();expect(view.nodes.some(node=>node.type==='img')).toBe(true);expect(view.html).toContain('A &lt;drawn&gt; bag');expect(view.html).toContain('Agent-authored description · unverified');
  expect(libraryApi.request).toHaveBeenCalledWith(`regions/${identity}/image`,{include_sensitive:false,view:'region'},undefined,undefined,expect.any(AbortSignal));
  action('Full page').onClick();expect(render().nodes.some(node=>node.type==='img')).toBe(false);view=await settle();expect(view.nodes.find(node=>node.type==='img')!.props.alt).toBe('Original PDF page 2');
  action('Open source reader').onClick();expect(props.select).toHaveBeenCalledWith({type:'document',id:'doc-1',page:2,originalPage:imagePacket(2).source_reference});
  action('Close image source').onClick();expect(props.close).toHaveBeenCalled();action('← Back').onClick();expect(props.back).toHaveBeenCalled();
 });
 it.each(['privacy','context','region'])('hides old pixels synchronously on %s changes and ignores late requests',async change=>{
  props.includePrivate=true;render();await settle();let resolve!:(value:any)=>void;vi.mocked(libraryApi.request).mockImplementation(()=>new Promise(done=>{resolve=done;}) as any);
  action('Full page').onClick();render();const late=resolve,old=packet('page'),signal=vi.mocked(libraryApi.request).mock.calls.at(-1)![4]!;
  if(change==='privacy')props.includePrivate=false;if(change==='context')(libraryApi as any).contextVersion++;
  if(change==='region')props.region={...props.region,record_id:'00000000-0000-4000-8000-000000000002',citation:'[region:00000000-0000-4000-8000-000000000002]'};
  expect(render(false).nodes.some(node=>node.type==='img')).toBe(false);hooks.effects.splice(0).forEach(effect=>effect());expect(signal.aborted).toBe(true);late(old);await settle();expect(render().nodes.some(node=>node.type==='img')).toBe(false);
 });
 it('rejects mismatched source identity and retries without retaining stale pixels',async()=>{
  vi.mocked(libraryApi.request).mockResolvedValue({...packet(),region_reference:{...props.region.source_reference,bbox:[0,0,1,1]}});
  render();let view=await settle();expect(view.html).toContain('no longer matches');expect(view.nodes.some(node=>node.type==='img')).toBe(false);
  vi.mocked(libraryApi.request).mockResolvedValue(packet());action('Retry image').onClick();render();view=await settle();expect(view.nodes.some(node=>node.type==='img')).toBe(true);
 });
 it('ignores stale image decode errors after a view change and aborts on unmount',async()=>{
  render();const old=(await settle()).nodes.find(node=>node.type==='img')!;action('Full page').onClick();render();await settle();old.props.onError();expect(render().nodes.some(node=>node.type==='img')).toBe(true);
  const signal=vi.mocked(libraryApi.request).mock.calls.at(-1)![4]!;hooks.cleanups.forEach(fn=>fn?.());expect(signal.aborted).toBe(true);
 });
});
