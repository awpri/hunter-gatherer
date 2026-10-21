import {beforeEach,describe,expect,it,vi} from 'vitest';
import type {ReactElement} from 'react';
// @ts-expect-error Vite raw source import for the mount-boundary regression.
import app from '../App.tsx?raw';
const hooks=vi.hoisted(()=>({index:0,slots:[] as any[],effects:[] as (()=>unknown)[]}));
vi.mock('react',async()=>({...await vi.importActual<typeof import('react')>('react'),
 useState:(initial:any)=>{const i=hooks.index++;if(!(i in hooks.slots))hooks.slots[i]=typeof initial==='function'?initial():initial;return [hooks.slots[i],(value:any)=>{hooks.slots[i]=typeof value==='function'?value(hooks.slots[i]):value;}];},
 useRef:(initial:any)=>{const i=hooks.index++;return hooks.slots[i]??(hooks.slots[i]={current:initial});},
 useEffect:(effect:()=>unknown,deps:any[])=>{const i=hooks.index++;const previous=hooks.slots[i];if(!previous||deps.some((value,index)=>value!==previous[index])){hooks.slots[i]=deps;hooks.effects.push(effect);}},
}));
vi.mock('../lib/libraryApi',()=>({libraryApi:{request:vi.fn()},errorMessage:(error:Error)=>error.message,isAbort:()=>false}));
import {libraryApi} from '../lib/libraryApi';
import {EvidenceWorkspace} from './EvidenceWorkspace';
const nodeText=(node:any):string=>typeof node==='string'||typeof node==='number'?String(node):Array.isArray(node)?node.map(nodeText).join(''):node?.props?nodeText(node.props.children):'';
function elements(node:unknown):ReactElement<any>[] {if(Array.isArray(node))return node.flatMap(elements);if(!node||typeof node!=='object'||!('props' in node))return [];const element=node as ReactElement<any>;return [element,...elements(element.props.children)];}
const base:{embedded?:boolean;searchDraft?:string;active:boolean;mode:'evidence'|'collections';seed:{query:string;token:number};includePrivate:boolean;select:ReturnType<typeof vi.fn>;notify:ReturnType<typeof vi.fn>;selectedOriginalPage?:import('./EvidenceWorkspace').OriginalPageSelection|null}={active:true,mode:'evidence' as 'evidence'|'collections',seed:{query:'',token:0},includePrivate:false,select:vi.fn(),notify:vi.fn()};
function render(props=base){hooks.index=0;const tree=EvidenceWorkspace(props);hooks.effects.splice(0).forEach(effect=>effect());return elements(tree);}
function field(nodes:ReactElement<any>[],index:number){return nodes.filter(node=>node.type==='textarea')[index];}
beforeEach(()=>{hooks.index=0;hooks.slots=[];hooks.effects=[];vi.mocked(libraryApi.request).mockReset();base.selectedOriginalPage=undefined;base.select.mockReset();base.notify.mockReset();vi.stubGlobal('crypto',{randomUUID:()=>'retry-identity'});});
describe('Evidence workspace navigation ownership',()=>{
 it('retains question, requirements, notes and candidates across hidden views and Collections without gathering again',async()=>{
  vi.mocked(libraryApi.request).mockImplementation(async(path)=>path==='collections'?{collections:[],next_cursor:null}:{relevant_memories:[{memory_id:'candidate-1',citation:'[mem:candidate-1]',text:'Candidate source wording',kind:'fact',verification:'self_reported'}],supporting_passages:[],requirement_checks:[],missing_information:[],usage_guidance:[],unresolved:[],retrieval_assessment:{status:'weak'}} as any);
  let nodes=render();field(nodes,0).props.onChange({target:{value:'Project role'}});field(nodes,1).props.onChange({target:{value:'Documented outcome'}});field(nodes,2).props.onChange({target:{value:'Review in progress'}});
  nodes=render();nodes.find(node=>node.type==='form')!.props.onSubmit({preventDefault:vi.fn()});await Promise.resolve();await Promise.resolve();
  nodes=render();nodes.find(node=>node.type==='button'&&nodeText(node)==='Save')!.props.onClick();
  const hidden=render({...base,active:false});expect(field(hidden,0).props.value).toBe('Project role');expect(hidden.some(node=>node.props['aria-label']==='Evidence results')).toBe(true);
  nodes=render({...base,mode:'collections'});await Promise.resolve();nodes=render({...base,mode:'collections'});
  expect(nodes.some(node=>node.props.children==='Candidate source wording')).toBe(true);expect(nodes.some(node=>node.type==='button'&&nodeText(node)==='Selected')).toBe(true);expect(field(nodes,1).props.value).toBe('Documented outcome');expect(nodes.some(node=>node.type==='textarea'&&node.props.value==='Review in progress')).toBe(true);expect(nodes.some(node=>node.props['aria-label']==='Evidence results')).toBe(true);
  render(base);expect(vi.mocked(libraryApi.request).mock.calls.filter(call=>call[0]==='find')).toHaveLength(1);
 });
 it('does no fetch or gather on an inactive initial mount',()=>{render({...base,active:false,mode:'collections'});expect(libraryApi.request).not.toHaveBeenCalled();});
 it('mounts the owner outside a navigation key and resets it at the privacy boundary',()=>{
  expect(app).not.toContain('key={`${view}:${tab}`}');expect(app).toContain('hidden={!(view===');expect(app).toContain('<EvidenceWorkspace embedded={');expect(app).toContain('<LibraryWorkspace key={String(includePrivate)}');
 });
});


describe('Original-page collection draft ownership',()=>{
 const reference={document_id:'scan-1',version:3,original_content_hash:'a'.repeat(64),page:2};
 function pageSelection(token=1){return {reference,title:'Image-only scan',token};}
 function button(nodes:ReactElement<any>[],label:string){return nodes.find(node=>node.type==='button'&&nodeText(node)===label)!;}
 async function settle(props=base){render(props);for(let tick=0;tick<8;tick++)await Promise.resolve();return render(props);}
 it('adds only a selected page to the shared draft, deduplicates it, and saves with the existing controls',async()=>{
  vi.mocked(libraryApi.request).mockImplementation(async path=>path==='collections'?{collections:[],next_cursor:null}:{} as any);
  let nodes=render();field(nodes,0).props.onChange({target:{value:'Compare scanned drawing'}});
  base.selectedOriginalPage=pageSelection();render();nodes=render();expect(button(nodes,'Image-only scan · Original page 2 · Version 3 →')).toBeDefined();
  expect(vi.mocked(libraryApi.request).mock.calls.every(call=>call[2]===undefined)).toBe(true);
  base.selectedOriginalPage=pageSelection(2);render();nodes=render();expect(nodes.filter(node=>node.type==='button'&&node.props['aria-label']==='Remove evidence reference')).toHaveLength(1);
  nodes.find(node=>node.type==='input'&&node.props.maxLength===160)!.props.onChange({target:{value:'Scan evidence'}});nodes=render();
  vi.mocked(libraryApi.request).mockImplementation(async (_path,_query,body)=>body?{...body,id:'collection-1',revision:1}:{} as any);
  await button(nodes,'Create collection').props.onClick();const body=vi.mocked(libraryApi.request).mock.calls.find(call=>call[2]!==undefined)![2] as any;
  expect(body.references).toEqual([reference]);expect(body.purpose).toBe('Compare scanned drawing');expect(body.idempotency_key).toBe('retry-identity');expect(body).not.toHaveProperty('image');
  nodes=render();button(nodes,'Image-only scan · Original page 2 · Version 3 →').props.onClick();expect(base.select).toHaveBeenCalledWith({type:'document',id:'scan-1',page:2,originalPage:reference});
 });
 it('restores saved page/version/hash coordinates after a fresh mount and fetches only a matching catalog title',async()=>{
  const collection={schemaVersion:2,id:'saved',revision:7,name:'Saved scan',requirements:[],references:[reference],voice_examples:[],unresolved:[],reviews:[]};
  vi.mocked(libraryApi.request).mockImplementation(async path=>path==='collections'?{collections:[collection],next_cursor:null}:path==='collections/saved'?collection:{document_id:'scan-1',version:3,content_hash:reference.original_content_hash,title:'Historical scan'} as any);
  let nodes=await settle({...base,mode:'collections'});nodes.find(node=>node.type==='button'&&node.props.className==='record-row ')!.props.onClick();nodes=await settle({...base,mode:'collections'});nodes=await settle({...base,mode:'collections'});
  button(nodes,'Historical scan · Original page 2 · Version 3 →').props.onClick();expect(base.select).toHaveBeenCalledWith({type:'document',id:'scan-1',page:2,originalPage:reference});expect(button(nodes,'Save revision')).toBeDefined();
 });
 it('keeps voice selection and actor review notes at the page citation and removes them with the last reference',async()=>{
  vi.mocked(libraryApi.request).mockResolvedValue({} as any);base.selectedOriginalPage=pageSelection();render();let nodes=render();button(nodes,'Writing example').props.onClick();nodes=render();
  expect(nodes.filter(node=>node.type==='button'&&nodeText(node)==='Image-only scan · Original page 2 · Version 3 →')).toHaveLength(2);
  const review=nodes.find(node=>node.type==='textarea'&&node.props.maxLength===500)!;review.props.onChange({target:{value:'My observation, no OCR quote'}});nodes=render();expect(nodes.some(node=>node.type==='textarea'&&node.props.value==='My observation, no OCR quote')).toBe(true);
  nodes.find(node=>node.type==='button'&&node.props['aria-label']==='Remove evidence reference')!.props.onClick();nodes=render();expect(nodes.some(node=>node.type==='textarea'&&node.props.value==='My observation, no OCR quote')).toBe(true);
  nodes.find(node=>node.type==='button'&&node.props['aria-label']==='Remove writing example')!.props.onClick();nodes=render();expect(nodes.some(node=>node.type==='textarea'&&node.props.value==='My observation, no OCR quote')).toBe(false);
 });
});


describe('Optional file discovery handoff',()=>{
 const file={document_id:'scan-file',version:2,content_hash:'a'.repeat(64),title:'Community archive',source_filename:'community.pdf',media_type:'application/pdf',has_stored_chunks:false,matched_queries:[1],matched_fields:['title']};
 const packet={query:'community',relevant_memories:[],supporting_passages:[],requirement_checks:[],missing_information:[{field:'evidence',question:'No relevant records or passages were found.'}],usage_guidance:[],retrieval_assessment:{status:'no_candidates'},source_discovery:{executed:true,candidates:[file],matching_documents:3,omitted_documents:2}};
 const text=(node:any):string=>typeof node==='string'||typeof node==='number'?String(node):Array.isArray(node)?node.map(text).join(''):node?.props?text(node.props.children):'';
 async function search(files=true,result:any=packet){
  vi.mocked(libraryApi.request).mockResolvedValue(result);let nodes=render();
  field(nodes,0).props.onChange({target:{value:'community'}});nodes=render();
  void files;
  nodes=render();nodes.find(node=>node.type==='form')!.props.onSubmit({preventDefault:vi.fn()});
  for(let i=0;i<5;i++)await Promise.resolve();return render();
 }
 it('uses unified Find without unsupported file-discovery or selection options',async()=>{
  await search(false,{...packet,source_discovery:undefined});expect(vi.mocked(libraryApi.request).mock.calls[0][2]).not.toHaveProperty('source_discovery');
  await search();expect(vi.mocked(libraryApi.request).mock.calls.at(-1)![0]).toBe('find');expect(vi.mocked(libraryApi.request).mock.calls.at(-1)![2]).toEqual({query:'community',requirements:[],include_sensitive:false,max_tokens:4096,contextual_requirements:true});
 });
 it('opens an unextracted file at its exact version without inventing a passage or page',async()=>{
  const nodes=await search();expect(nodes.some(node=>node.type==='h3'&&text(node)==='Files to inspect')).toBe(true);
  expect(nodes.some(node=>text(node)==='Text hasn’t been extracted. Open the file to read it or extract its text.')).toBe(true);
  expect(nodes.some(node=>node.type==='p'&&text(node)==='No relevant records or passages were found.')).toBe(false);
  expect(nodes.some(node=>node.type==='p'&&text(node).startsWith('2 more file matches.'))).toBe(true);
  nodes.find(node=>node.type==='button'&&node.props.className==='file-title')!.props.onClick();
  expect(base.select).toHaveBeenCalledWith({type:'document',id:'scan-file',originalFile:{document_id:'scan-file',version:2,original_content_hash:'a'.repeat(64)}});
  expect(nodes.some(node=>node.type==='button'&&text(node)==='Save')).toBe(false);
  expect(vi.mocked(libraryApi.request).mock.calls).toHaveLength(1);
 });
 it('saves a catalog-only file without turning its identity into a quotation',async()=>{
  let nodes=await search();nodes.find(node=>node.type==='button'&&text(node)==='Save file')!.props.onClick();nodes=render();
  nodes.find(node=>node.type==='input'&&node.props.maxLength===160)!.props.onChange({target:{value:'Design originals'}});nodes=render();
  await nodes.find(node=>node.type==='button'&&text(node)==='Create collection')!.props.onClick();
  const call=vi.mocked(libraryApi.request).mock.calls.find(call=>call[0]==='collections'&&call[3]==='POST')!;
  expect(call[2]).toMatchObject({name:'Design originals',references:[{document_id:'scan-file',version:2,original_content_hash:'a'.repeat(64)}]});
  expect(call[2]).not.toHaveProperty('quote');
 });
 it('distinguishes no metadata match from a search that could not run',async()=>{
  let nodes=await search(true,{...packet,source_discovery:{executed:true,candidates:[],matching_documents:0}});
  expect(nodes.some(node=>node.props.role==='status'&&text(node).startsWith('No file details matched.'))).toBe(true);
  nodes=await search(true,{...packet,source_discovery:{executed:false,reason:'no_searchable_metadata_terms',candidates:[]}});
  expect(nodes.some(node=>node.props.role==='status'&&text(node).startsWith('Use a distinctive title'))).toBe(true);
 });
 it('retains the option and results across navigation without repeating retrieval',async()=>{
  await search();const nodes=render({...base,active:false});

  expect(nodes.some(node=>node.props['aria-label']==='File discovery')).toBe(true);
  render();expect(vi.mocked(libraryApi.request).mock.calls).toHaveLength(1);
 });
});

describe('Relationship route inspection',()=>{
 const text=(node:any):string=>typeof node==='string'||typeof node==='number'?String(node):Array.isArray(node)?node.map(text).join(''):node?.props?text(node.props.children):'';
 const route={reason:'typed_relationship',seed:{record_id:'seed',label:'<img src=x onerror=alert(1)>',match:'name_or_alias'},steps:[{record_id:'edge',predicate:'uses',support:'inferred',assertion_status:'uncertain',review_status:'unreviewed',source_citations:['[doc:decision#anchor]'],relation_validity:{valid_from:'2032-01-01'}}],target:{record_id:'target',label:'Relay',citation:'[doc:relay#details]'}};
 const packet={query:'Cedar',relevant_memories:[],supporting_passages:[],requirement_checks:[],missing_information:[],usage_guidance:[],retrieval_assessment:{status:'no_candidates'},retrieval_paths:{status:'bounded_navigation_hints',paths:[route],truncated:true}};
 async function gather(result:any){vi.mocked(libraryApi.request).mockResolvedValue(result);let nodes=render();field(nodes,0).props.onChange({target:{value:'Cedar'}});nodes=render();nodes.find(node=>node.type==='form')!.props.onSubmit({preventDefault:vi.fn()});for(let i=0;i<8;i++)await Promise.resolve();return render();}
 it('uses a keyboard-activatable native button and opens exact sources without changing the collection draft',async()=>{
  let nodes=await gather(packet);const toggle=nodes.find(node=>node.type==='button'&&text(node)==='Why this source')!;
  expect(toggle.props.type).toBe('button');expect(toggle.props['aria-expanded']).toBe(false);expect(toggle.props.onKeyDown).toBeUndefined();
  expect(nodes.find(node=>node.props.id===toggle.props['aria-controls'])?.props.hidden).toBe(true);
  // Native buttons dispatch click with detail=0 for keyboard activation.
  toggle.props.onClick({detail:0});nodes=render();expect(nodes.find(node=>node.type==='button'&&text(node)==='Why this source')!.props['aria-expanded']).toBe(true);
  expect(nodes.find(node=>node.props.id===toggle.props['aria-controls'])?.props.hidden).toBe(false);
  nodes.find(node=>node.type==='button'&&text(node).startsWith('Read route source'))!.props.onClick();expect(base.select).toHaveBeenCalledWith({type:'document',id:'decision',chunk:'anchor'});
  nodes.find(node=>node.type==='button'&&text(node)==='Relay')!.props.onClick();expect(base.select).toHaveBeenLastCalledWith({type:'document',id:'relay',chunk:'details'});
  expect(field(nodes,0).props.value).toBe('Cedar');expect(nodes.some(node=>node.type==='h4'&&text(node)==='Evidence · 0')).toBe(true);
  expect(text(nodes)).toContain('not verified claim support');expect(text(nodes)).toContain('Relationship valid from 2032-01-01');expect(text(nodes)).toContain('More routes may exist');
  expect(nodes.some(node=>node.type==='strong'&&node.props.children===route.seed.label)).toBe(true);expect(nodes.some(node=>node.props.dangerouslySetInnerHTML)).toBe(false);expect(nodes.some(node=>node.type==='img')).toBe(false);
 });
 it('keeps the normal workspace unchanged when no routes are supplied',async()=>{const {retrieval_paths:_,...ordinary}=packet;const nodes=await gather(ordinary);expect(nodes.some(node=>text(node)==='Why this source')).toBe(false);expect(nodes.some(node=>node.props['aria-label']==='Related source routes')).toBe(false);});
});

describe('Truthful route provenance and declared field witnesses',()=>{
 const text=(node:any):string=>typeof node==='string'||typeof node==='number'?String(node):Array.isArray(node)?node.map(text).join(''):node?.props?text(node.props.children):'';
 const step={record_id:'edge',predicate:'uses',support:'authored',assertion_status:'asserted',review_status:'unreviewed',source_citations:['[doc:decision#anchor]']};
 const route={reason:'typed_relationship',seed:{record_id:'seed',label:'Survey',match:'name_or_alias',source_citations:['[doc:registry#name]']},steps:[step],target:{record_id:'target',label:'Relay',citation:'[doc:relay#detail]',identity_citations:['[doc:spec#identity]'],matched_fields:['interface']}};
 async function gatherRoute(path:any){vi.mocked(libraryApi.request).mockResolvedValue({query:'Survey',relevant_memories:[],supporting_passages:[],requirement_checks:[],missing_information:[],usage_guidance:[],retrieval_assessment:{status:'weak_matches'},retrieval_paths:{paths:[path]}});let nodes=render();field(nodes,0).props.onChange({target:{value:'Survey'}});nodes=render();nodes.find(node=>node.type==='form')!.props.onSubmit({preventDefault:vi.fn()});for(let i=0;i<8;i++)await Promise.resolve();nodes=render();nodes.find(node=>node.type==='button'&&text(node)==='Why this source')!.props.onClick({detail:0});return render();}
 it('labels zero-step source attachment as structural even when an aggregate hint says authored',async()=>{
  const nodes=await gatherRoute({...route,steps:[],support:'authored',reason:'entity_source'});
  expect(text(nodes)).toContain('Source attachment · structural');expect(text(nodes)).not.toContain('Relationship route · authored');expect(text(nodes)).not.toContain('Authored relationship route');
 });
 it('keeps authored and unreviewed separate from verification and opens only returned witness citations',async()=>{
  const nodes=await gatherRoute({...route,applicability:{status:'match',checks:[{field:'attributes.interface',status:'match',reason:'exact_typed_value',requested:'C8',actual:'C8'}],missing_fields:[],review_status:'unreviewed'}});
  expect(text(nodes)).toContain('Relationship route · authored');expect(text(nodes)).toContain('Review: unreviewed');expect(text(nodes)).toContain('Declared field comparison · match');expect(text(nodes)).toContain('does not verify compatibility');
  const openWitness=(label:string)=>nodes.find(node=>node.type==='button'&&text(node).startsWith(label))!.props.onClick();
  openWitness('Read seed identity sources');expect(base.select).toHaveBeenLastCalledWith({type:'document',id:'registry',chunk:'name'});
  openWitness('Read target identity sources');expect(base.select).toHaveBeenLastCalledWith({type:'document',id:'spec',chunk:'identity'});
  openWitness('Read field source');expect(base.select).toHaveBeenLastCalledWith({type:'document',id:'relay',chunk:'detail'});
  expect(text(nodes)).toContain('Declared field witnesses: interface');expect(text(nodes)).toContain('requested "C8" · declared "C8"');expect(field(nodes,0).props.value).toBe('Survey');
  expect(nodes.some(node=>node.props.children==='Verified'||node.props.children==='Compatible')).toBe(false);expect(nodes.some(node=>node.type==='h4'&&text(node)==='Evidence · 0')).toBe(true);
 });
 it('retains uncertain/rejected edge metadata and unknown missing fields without upgrading support',async()=>{
  const unknown={status:'unknown',checks:[{field:'valid_to',status:'unknown',reason:'missing_authored_field',requested_as_of:'2032-01-01'}],missing_fields:['valid_to'],review_status:'rejected',assertion_status:'uncertain'};
  const nodes=await gatherRoute({...route,support:'authored',steps:[{...step,assertion_status:'uncertain',review_status:'rejected',applicability:unknown}],applicability:unknown});
  expect(text(nodes)).toContain('Relationship route · inferred');expect(text(nodes)).toContain('Review: rejected');expect(text(nodes)).toContain('Declared field comparison · unknown');expect(text(nodes)).toContain('Missing authored fields: valid_to');expect(text(nodes)).toContain('requested as of 2032-01-01');expect(text(nodes)).not.toContain('Relationship route · authored');
 });
 it('identifies mixed routes and preserves exact mismatches rather than inferring compatibility',async()=>{
  const mismatch={status:'mismatch',checks:[{field:'attributes.interface',status:'mismatch',reason:'different_typed_value',requested:'C8',actual:'C9'},{field:'attributes.voltage',status:'unknown',reason:'missing_authored_field',requested:5}],missing_fields:['attributes.voltage']};
  const nodes=await gatherRoute({...route,steps:[step,{...step,record_id:'edge-2',support:'inferred',assertion_status:'uncertain',review_status:'disputed'}],applicability:mismatch});
  expect(text(nodes)).toContain('Relationship route · mixed authored and inferred');expect(text(nodes)).toContain('Review: disputed');expect(text(nodes)).toContain('Declared field comparison · mismatch');expect(text(nodes)).toContain('requested "C8" · declared "C9"');expect(text(nodes)).toContain('attributes.voltage · unknown');
 });
 it('does not invent support or field witnesses when the response omits them',async()=>{
  const nodes=await gatherRoute({...route,steps:[{record_id:'unclassified',predicate:'related_to',source_citations:[]}],seed:{record_id:'seed',label:'Survey',match:'passage'},target:{record_id:'target',label:'Relay',citation:'[doc:relay#detail]'}});
  expect(text(nodes)).toContain('Relationship route · support partly unspecified');expect(text(nodes)).toContain('Support unspecified');expect(text(nodes)).not.toContain('Declared field comparison');expect(text(nodes)).not.toContain('Target identity sources');expect(text(nodes)).not.toContain('Declared field witnesses');
 });
});

describe('Bounded Find continuation',()=>{
 const first={query:'project',relevant_memories:[],supporting_passages:[{document_id:'doc-a',chunk_id:'chunk-a',citation:'[doc:doc-a#chunk-a]',quote:'Exact first wording',title:'First source'}],retrieval_assessment:{status:'weak_matches'},requirement_checks:[],missing_information:[],usage_guidance:[],omitted_references:[{citation:'[doc:doc-b#chunk-b]',stream:'supporting_passages',rank:2,omission_reason:'weak_query_body_match',navigation_labels:{title:'Weak source'}}],projection:{omitted_counts:{supporting_passages:1},omitted_reference_count:1},continuation:{next_cursor:'a'.repeat(32),scope:'Admitted matches only'}};
 const text=(node:any):string=>typeof node==='string'||typeof node==='number'?String(node):Array.isArray(node)?node.map(text).join(''):node?.props?text(node.props.children):'';
 async function search(){vi.mocked(libraryApi.request).mockResolvedValueOnce(first);let nodes=render();field(nodes,0).props.onChange({target:{value:'project'}});field(nodes,1).props.onChange({target:{value:'Outcome'}});nodes=render();nodes.find(node=>node.type==='form')!.props.onSubmit({preventDefault:vi.fn()});for(let i=0;i<5;i++)await Promise.resolve();return render();}
 it('shows omitted weak leads as navigation and preserves the question and first page through exact source reads and continuation',async()=>{
  let nodes=await search();expect(nodes.some(node=>node.type==='p'&&text(node)==='Open to read the text · weak query body match')).toBe(true);
  nodes.find(node=>node.type==='button'&&text(node)==='Weak source')!.props.onClick();expect(base.select).toHaveBeenCalledWith({type:'document',id:'doc-b',chunk:'chunk-b'});
  nodes=render();expect(field(nodes,0).props.value).toBe('project');expect(field(nodes,1).props.value).toBe('Outcome');
  vi.mocked(libraryApi.request).mockResolvedValueOnce({...first,supporting_passages:[...first.supporting_passages,{document_id:'doc-b',chunk_id:'chunk-b',citation:'[doc:doc-b#chunk-b]',quote:'Exact second wording'}],omitted_references:[],projection:{omitted_counts:{}},continuation:{next_cursor:null},retrieval_assessment:{status:'weak_matches',missing_query_terms:['first']},missing_information:[{field:'detail',question:'Inspect detail'}],requirement_checks:[{query:'Outcome',retrieval_assessment:{status:'weak_matches',missing_query_terms:['outcome']},warnings:[],next_step:'Open'}]});
  await nodes.find(node=>node.type==='button'&&text(node)==='Load more results')!.props.onClick();
  expect(vi.mocked(libraryApi.request).mock.calls.at(-1)![2]).toEqual({cursor:'a'.repeat(32),include_sensitive:false,max_tokens:4096,contextual_requirements:true});
  nodes=render();expect(nodes.some(node=>node.props.className==='result-count'&&text(node)==='2 results')).toBe(true);expect(nodes.some(node=>node.type==='p'&&text(node).includes('2 pages loaded · Latest page: Weak matches · review required'))).toBe(true);expect(nodes.some(node=>node.type==='p'&&text(node).includes('Page 2 candidate matching · terms not matched on this page: first'))).toBe(true);expect(nodes.some(node=>node.type==='p'&&text(node).includes('Page 2 candidate matching · terms not matched on this page: outcome'))).toBe(true);expect(nodes.some(node=>node.type==='p'&&text(node).includes('Page 2: Inspect detail This note does not assess all loaded sources.'))).toBe(true);expect(nodes.filter(node=>node.type==='blockquote')).toHaveLength(2);expect(nodes.some(node=>node.type==='button'&&text(node)==='Load more results')).toBe(false);expect(nodes.some(node=>node.type==='h3'&&text(node)==='More sources to inspect')).toBe(false);
 });
 it('keeps the first page and cursor when a continuation fails and allows retry',async()=>{
  let nodes=await search();vi.mocked(libraryApi.request).mockRejectedValueOnce(new Error('Find cursor is stale; restart Find.'));
  await nodes.find(node=>node.type==='button'&&text(node)==='Load more results')!.props.onClick();nodes=render();
  expect(nodes.some(node=>node.type==='blockquote'&&text(node)==='Exact first wording')).toBe(true);expect(nodes.some(node=>node.type==='button'&&text(node)==='Load more results')).toBe(true);expect(field(nodes,0).props.value).toBe('project');
 });
});

describe('Superseded Gather responses',()=>{
 it('ignores a response from an aborted search even when its transport resolves after the newer search',async()=>{
  let resolveOld!:(packet:any)=>void;let resolveNew!:(packet:any)=>void;
  vi.mocked(libraryApi.request).mockImplementation((_path,_query,body:any)=>new Promise(resolve=>{if(body.query==='old query')resolveOld=resolve;else resolveNew=resolve;}));
  let nodes=render();field(nodes,0).props.onChange({target:{value:'old query'}});nodes=render();nodes.find(node=>node.type==='form')!.props.onSubmit({preventDefault:vi.fn()});
  const next={...base,seed:{query:'new query',token:1}};render(next);
  const packet=(wording:string)=>({query:wording,relevant_memories:[],supporting_passages:[{citation:`[doc:d#${wording}]`,document_id:'d',chunk_id:wording,quote:wording}],requirement_checks:[],missing_information:[],usage_guidance:[],retrieval_assessment:{}});
  resolveNew(packet('current'));for(let i=0;i<5;i++)await Promise.resolve();resolveOld(packet('obsolete'));for(let i=0;i<5;i++)await Promise.resolve();nodes=render(next);
  expect(nodes.some(node=>node.type==='blockquote'&&nodeText(node)==='current')).toBe(true);expect(nodes.some(node=>node.type==='blockquote'&&nodeText(node)==='obsolete')).toBe(false);expect(field(nodes,0).props.value).toBe('new query');
 });
});

describe('Executed search identity',()=>{
 const text=(node:any):string=>typeof node==='string'||typeof node==='number'?String(node):Array.isArray(node)?node.map(text).join(''):node?.props?text(node.props.children):'';
 it('blocks continuation and save after edits and allows restoring the executed query',async()=>{
  vi.mocked(libraryApi.request).mockResolvedValue({query:'First question',relevant_memories:[],supporting_passages:[],continuation:{next_cursor:'cursor'},retrieval_assessment:{status:'no_candidates'}} as any);
  let nodes=render();field(nodes,0).props.onChange({target:{value:'First question'}});nodes=render();nodes.find(node=>node.type==='form')!.props.onSubmit({preventDefault:vi.fn()});for(let i=0;i<8;i++)await Promise.resolve();nodes=render();
  field(nodes,0).props.onChange({target:{value:'Second question'}});nodes=render();
  const more=nodes.find(node=>node.type==='button'&&text(node)==='Load more results')!;expect(more.props.disabled).toBe(true);await more.props.onClick();expect(libraryApi.request).toHaveBeenCalledTimes(1);expect(text(nodes)).toContain('Search changes haven’t been applied yet.');
  nodes.find(node=>node.type==='input'&&node.props.maxLength===160)!.props.onChange({target:{value:'Saved investigation'}});nodes=render();const save=nodes.find(node=>node.type==='button'&&text(node)==='Create collection')!;expect(save.props.disabled).toBe(true);await save.props.onClick();expect(libraryApi.request).toHaveBeenCalledTimes(1);
  nodes.find(node=>node.type==='button'&&text(node)==='Undo changes')!.props.onClick();nodes=render();expect(field(nodes,0).props.value).toBe('First question');expect(nodes.find(node=>node.type==='button'&&text(node)==='Load more results')!.props.disabled).toBe(false);
 });
 it('opens exact image selection without making an annotation into a quote',async()=>{
  const region={record_id:'11111111-1111-4111-8111-111111111111',citation:'[region:11111111-1111-4111-8111-111111111111]',description:'Blue diagram',verification:'unverified',source_reference:{document_id:'source',version:1,original_content_hash:'a'.repeat(64),page:2,bbox:[0.1,0.2,0.6,0.8]}};
  vi.mocked(libraryApi.request).mockResolvedValue({query:'diagram',visual_regions:[region],relevant_memories:[],supporting_passages:[],retrieval_assessment:{status:'no_candidates'}} as any);
  let nodes=render();field(nodes,0).props.onChange({target:{value:'diagram'}});nodes=render();nodes.find(node=>node.type==='form')!.props.onSubmit({preventDefault:vi.fn()});for(let i=0;i<8;i++)await Promise.resolve();nodes=render();
  expect(text(nodes)).toContain('AI description');expect(nodes.some(node=>node.type==='blockquote')).toBe(false);nodes.find(node=>node.type==='button'&&text(node).startsWith('Open original image'))!.props.onClick();expect(base.select).toHaveBeenCalledWith({type:'document',id:'source',region});
 });
});


describe('Quote-first Ask and direct cited-context handoff',()=>{
 const text=(node:any):string=>typeof node==='string'||typeof node==='number'?String(node):Array.isArray(node)?node.map(text).join(''):node?.props?text(node.props.children):'';
 const exact={document_id:'design',chunk_id:'page-two',citation:'[doc:design#page-two]',title:'Design notes',quote:'The original bag was blue.',location:{page:2,section:'Design'},version:1};
 const packet={query:'What did the bag look like?',relevant_memories:[{memory_id:'m',citation:'[mem:m]',text:'A saved memory of the bag',kind:'fact',verification:'self_reported'}],supporting_passages:[exact],enrichment_discovery:{candidates:[{record_id:'derived',derived_text:'An agent guesses it was red.',source_references:[{citation:'[doc:other#lead]'}]}]},requirement_checks:[],missing_information:[],usage_guidance:[],retrieval_assessment:{status:'review_required'}};
 async function search(result:any=packet){vi.mocked(libraryApi.request).mockResolvedValueOnce(result);let nodes=render();field(nodes,0).props.onChange({target:{value:packet.query}});nodes=render();nodes.find(node=>node.type==='form')!.props.onSubmit({preventDefault:vi.fn()});for(let i=0;i<8;i++)await Promise.resolve();return render();}
 const button=(nodes:ReactElement<any>[],label:string)=>nodes.find(node=>node.type==='button'&&text(node)===label)!;
 it('starts with a normal question and optional additional searches and collections, preserving exact sources before derived leads',async()=>{
  let nodes=render();expect(nodes.find(node=>node.type==='h2')!.props.children).toBe('Ask your library');
  expect(nodes.find(node=>node.props.id==='ask-search-options')!.props.hidden).toBe(true);
  expect(nodes.find(node=>node.props.id==='ask-collection-editor')!.props.hidden).toBe(true);
  expect(text(nodes)).toContain('it is not a condition every source must meet');
  nodes=await search();const quotation=nodes.findIndex(node=>node.type==='blockquote');const derived=nodes.findIndex(node=>node.type==='p'&&text(node).includes('An agent guesses'));
  expect(quotation).toBeGreaterThan(-1);expect(derived).toBeGreaterThan(quotation);expect(nodes.filter(node=>node.type==='blockquote').map(text)).toEqual([exact.quote]);
  expect(text(nodes)).toContain('Page 2 · Design · Version 1');expect(button(nodes,'Copy quotes + citations').props.disabled).toBe(false);
 });
 it('marks a clipped display excerpt while keeping the original passage accessible and the copy request unchanged',async()=>{
  const full='General project bookkeeping. '.repeat(30)+'The bag was not red. The original bag design was blue with white lettering. The design was only a proposal at that point. '+'Later unrelated archive notes. '.repeat(30);
  const result={...packet,supporting_passages:[{...exact,quote:full}],relevant_memories:[]};
  const writeText=vi.fn().mockResolvedValue(undefined);vi.stubGlobal('navigator',{clipboard:{writeText}});
  let nodes=await search(result);const quote=nodes.find(node=>node.type==='blockquote')!;expect(text(quote)).toMatch(/^… .* …$/s);expect(text(quote)).toContain('The bag was not red.');
  const disclosure=nodes.find(node=>node.type==='details'&&node.props.className==='quote-full-passage')!;expect(text(disclosure)).toContain('Read in context');expect(text(disclosure)).toContain(full);
  vi.mocked(libraryApi.request).mockResolvedValueOnce({content:'Server validated full source context',truncated:false,unresolved:[]});await button(nodes,'Copy quotes + citations').props.onClick();for(let i=0;i<8;i++)await Promise.resolve();
  expect(vi.mocked(libraryApi.request).mock.calls.find(call=>call[0]==='find/context')![2]).toMatchObject({references:[{document_id:'design',chunk_id:'page-two'}]});expect(writeText).toHaveBeenCalledWith('Server validated full source context');
 });
 it('uses the unified search as the embedded editor and keeps optional additional searches without a question accordion',async()=>{
  let nodes=render();field(nodes,1).props.onChange({target:{value:'Where was the bag distributed?'}});
  nodes=await search();expect(nodes.some(node=>node.type==='details'&&node.props.className?.startsWith('ask-editor'))).toBe(false);
  expect(field(nodes,0).props.value).toBe(packet.query);expect(field(nodes,1).props.value).toBe('Where was the bag distributed?');
  nodes=render({...base,embedded:true});expect(nodes.some(node=>node.props.id==='library-question')).toBe(false);
  expect(nodes.find(node=>node.props.id==='ask-search-options')!.props.hidden).toBe(true);
  nodes.find(node=>node.type==='button'&&node.props['aria-label']==='Search options')!.props.onClick();nodes=render({...base,embedded:true});
  expect(nodes.find(node=>node.props.id==='ask-search-options')!.props.hidden).toBe(false);
  nodes.find(node=>node.props.id==='library-additional-questions')!.props.onChange({target:{value:'What changed?'}});nodes=render({...base,embedded:true});
  expect(text(nodes)).toContain('Search changes haven’t been applied yet.');button(nodes,'Undo changes').props.onClick();nodes=render({...base,embedded:true});
  expect(nodes.find(node=>node.props.id==='library-additional-questions')!.props.value).toBe('Where was the bag distributed?');
  expect(vi.mocked(libraryApi.request).mock.calls.filter(call=>call[0]==='find')).toHaveLength(1);
 });
 it('marks unsubmitted header edits and blocks stale copy, continuation, and collection save',async()=>{
  let nodes=await search({...packet,continuation:{next_cursor:'next-page'}});
  nodes=render({...base,embedded:true,searchDraft:'A different question'});
  expect(text(nodes)).toContain('Press Enter to search this question.');expect(button(nodes,'Copy quotes + citations').props.disabled).toBe(true);
  await button(nodes,'Copy quotes + citations').props.onClick();expect(vi.mocked(libraryApi.request).mock.calls.some(call=>call[0]==='find/context')).toBe(false);
  expect(button(nodes,'Load more results').props.disabled).toBe(true);expect(button(nodes,'Create collection').props.disabled).toBe(true);
  nodes=render({...base,embedded:true,searchDraft:packet.query});expect(button(nodes,'Copy quotes + citations').props.disabled).toBe(false);
 });
 it('opens a passage through its source heading, keeps raw citations out of the card and puts collection selection in the toolbar',async()=>{
  let nodes=await search();const quote=nodes.find(node=>node.type==='article'&&node.props.className==='evidence-card quote-card')!;
  const inside=elements(quote);inside.find(node=>node.type==='button'&&node.props.className==='quote-source')!.props.onClick();
  expect(base.select).toHaveBeenCalledWith({type:'document',id:'design',chunk:'page-two'});expect(inside.some(node=>node.type==='code')).toBe(false);
  button(inside,'Save').props.onClick();nodes=render();expect(nodes.some(node=>node.props['aria-label']==='Selected sources')).toBe(true);
  expect(nodes.find(node=>node.props.id==='ask-collection-editor')!.props.hidden).toBe(true);button(nodes,'Save collection').props.onClick();nodes=render();
  expect(nodes.find(node=>node.props.id==='ask-collection-editor')!.props.hidden).toBe(false);expect(button(nodes,'Copy 1 selected + citations')).toBeDefined();
 });
 it('deduplicates represented saved-memory leads and labels unopened related records without exposing UUID-only cards',async()=>{
  const nodes=await search({...packet,enrichment_discovery:undefined,memory_discovery:{candidates:[{memory_id:'m',citation:'[mem:m]'},{memory_id:'unopened',citation:'[mem:unopened]'}]}});
  const related=nodes.find(node=>node.props['aria-label']==='Linked discovery leads')!;const inside=elements(related);
  expect(inside.filter(node=>node.type==='article')).toHaveLength(1);expect(text(related)).not.toContain('[mem:');
  button(inside,'Related saved record').props.onClick();expect(base.select).toHaveBeenCalledWith({type:'memory',id:'unopened'});
 });
 it('copies freshly validated exact references without saving a collection or including discovery annotations',async()=>{
  const writeText=vi.fn().mockResolvedValue(undefined);vi.stubGlobal('navigator',{clipboard:{writeText}});
  let nodes=await search();vi.mocked(libraryApi.request).mockResolvedValueOnce({content:'Validated cited context [doc:design#page-two]',truncated:false,unresolved:[]});
  await button(nodes,'Copy quotes + citations').props.onClick();for(let i=0;i<8;i++)await Promise.resolve();nodes=render();
  const call=vi.mocked(libraryApi.request).mock.calls.find(call=>call[0]==='find/context')!;
  expect(call[2]).toEqual({query:packet.query,requirements:[],references:[{document_id:'design',chunk_id:'page-two'},{memory_id:'m'}],include_sensitive:false,max_tokens:4096});
  expect(JSON.stringify(call[2])).not.toContain('guesses');expect(writeText).toHaveBeenCalledWith('Validated cited context [doc:design#page-two]');
  expect(vi.mocked(libraryApi.request).mock.calls.some(call=>call[0]==='collections')).toBe(false);expect(text(nodes)).toContain('with citations.');
 });
 it('offers a selectable fallback and reports bounded output when clipboard access fails',async()=>{
  vi.stubGlobal('navigator',{clipboard:{writeText:vi.fn().mockRejectedValue(new Error('Blocked'))}});
  let nodes=await search();vi.mocked(libraryApi.request).mockResolvedValueOnce({content:'Bounded fresh context',truncated:true,unresolved:[],omitted_references:2});
  await button(nodes,'Copy quotes + citations').props.onClick();for(let i=0;i<8;i++)await Promise.resolve();nodes=render();
  expect(text(nodes)).toContain('Context reached its size limit');expect(text(nodes)).toContain('2 references did not fit');const fallback=nodes.find(node=>node.type==='textarea'&&node.props.readOnly)!;expect(fallback.props.value).toBe('Bounded fresh context');
  const select=vi.fn();fallback.props.onFocus({currentTarget:{select}});expect(select).toHaveBeenCalledOnce();
 });
 it('keeps unavailable sources out of copied text and clears old context after a question edit',async()=>{
  const writeText=vi.fn().mockResolvedValue(undefined);vi.stubGlobal('navigator',{clipboard:{writeText}});
  let nodes=await search();vi.mocked(libraryApi.request).mockResolvedValueOnce({content:'Selected sources are unavailable.',truncated:false,unresolved:['Unavailable references']});
  await button(nodes,'Copy quotes + citations').props.onClick();for(let i=0;i<8;i++)await Promise.resolve();nodes=render();
  expect(writeText).toHaveBeenCalledWith('Selected sources are unavailable.');expect(text(nodes)).toContain('Some references are unavailable');
  field(nodes,0).props.onChange({target:{value:'A different question'}});nodes=render();expect(button(nodes,'Copy quotes + citations').props.disabled).toBe(true);expect(nodes.some(node=>node.props.className==='copy-context-status field-note')).toBe(false);
 });
 it('does not silently drop sources over the handoff limit and lets the user copy selected loaded references',async()=>{
  const writeText=vi.fn().mockResolvedValue(undefined);vi.stubGlobal('navigator',{clipboard:{writeText}});
  let nodes=await search({...packet,relevant_memories:[],supporting_passages:Array.from({length:51},(_,i)=>({...exact,chunk_id:`c${i}`,citation:`[doc:design#c${i}]`}))});
  await button(nodes,'Copy quotes + citations').props.onClick();expect(vi.mocked(libraryApi.request).mock.calls.filter(call=>call[0]==='find/context')).toHaveLength(0);
  nodes=render();expect(text(nodes)).toContain('More than 50 sources are loaded');button(nodes,'Save').props.onClick();nodes=render();
  vi.mocked(libraryApi.request).mockResolvedValueOnce({content:'Selected quote',truncated:false,unresolved:[]});
  await button(nodes,'Copy 1 selected + citations').props.onClick();for(let i=0;i<8;i++)await Promise.resolve();
  expect(vi.mocked(libraryApi.request).mock.calls.find(call=>call[0]==='find/context')![2]).toMatchObject({references:[{document_id:'design',chunk_id:'c0'}]});
 });
});
