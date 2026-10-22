import {isVisualRegion,type VisualRegion} from './visualRegions';
import {isOriginalFileReference,originalFileReference,originalFileCitation,type OriginalFileReference} from './originalFiles';
import {isOriginalPageReference,originalPageReference,type OriginalPageReference} from './pageImages';
import type {EvidenceRef,RetrievalPaths,RetrievalPath,RetrievalPathStep} from './libraryApi';
export interface SourcePassage extends EvidenceRef {title?:string;quote:string;chunk_id:string;citation:string;ordinal?:number;anchor?:boolean;version?:number;is_latest?:boolean}
export interface EvidenceMemory {memory_id:string;citation:string;text:string;kind:string;verification:string;provenance?:{sources:SourcePassage[]}}
export interface Assessment {status?:string;candidate_citations?:string[];missing_query_terms?:string[];[key:string]:unknown}
export interface SourceFamily {family_id:string;document_ids:string[];citations:string[];duplicate_excerpt_count:number;independent_corroboration:string}
export interface FileLead {source_reference?:OriginalFileReference;original_citation?:string;document_id:string;version:number;content_hash:string;title:string|null;source_filename:string|null;media_type:string|null;author?:string|null;document_date?:string|null;has_stored_chunks:boolean;matched_queries:number[];matched_fields:string[];source_metadata_warnings?:string[]}
export interface FileDiscovery {executed:boolean;reason?:string;candidates:FileLead[];queries?:string[];matching_documents?:number;omitted_documents?:number;documents_with_omitted_metadata?:number}
export interface OmittedReference {citation?:string;stream:string;rank:number;omission_reason?:string;navigation_labels?:{title?:string;text_prefix?:string;section?:string;page?:number|string};source_references?:{citation?:string}[]}
export interface DiscoveryLead {citation?:string;record_id?:string;memory_id?:string;derived_text?:string;navigation_labels?:{title?:string;text_prefix?:string};source_references?:{citation?:string}[]}
export interface EvidencePacket {visual_region_discovery?:{scope:string;candidate_limit_reached?:boolean;remaining?:number};visual_regions?:VisualRegion[];executed_requirements?:string[];loaded_pages?:number;memory_discovery?:{candidates:DiscoveryLead[];omitted_candidates?:number};enrichment_discovery?:{candidates:DiscoveryLead[]};source_leads?:DiscoveryLead[];warnings?:string[];continuation?:{next_cursor:string|null;scope?:string};omitted_references?:OmittedReference[];projection?:{omitted_counts?:Record<string,number>;omitted_reference_count?:number;[key:string]:unknown};retrieval_paths?:RetrievalPaths;source_discovery?:FileDiscovery;source_families?:SourceFamily[];query:string;relevant_memories:EvidenceMemory[];supporting_passages:SourcePassage[];missing_information:{question:string;field:string;page?:number}[];requirement_checks:{query:string;assessment_page?:number;selected_citations?:string[];retrieval_assessment:Assessment;warnings:string[];next_step:string}[];retrieval_assessment:Assessment;usage_guidance:string[];retrieval?:{warnings:string[]}}
export type CollectionReference = {memory_id:string;document_id?:never;chunk_id?:never}|{document_id:string;chunk_id?:string;memory_id?:never;version?:never;original_content_hash?:never;page?:never}|(OriginalPageReference&{memory_id?:never;chunk_id?:never})|(OriginalFileReference&{memory_id?:never;chunk_id?:never;page?:never});
export interface CollectionReview {citation:string;status:'unreviewed'|'reviewed'|'needs_context';note:string}
export interface Collection {schemaVersion?:1|2|3;notes?:string;reviews?:CollectionReview[];id:string;revision:number;name:string;purpose?:string;requirements:string[];references:CollectionReference[];voice_examples:CollectionReference[];unresolved:string[];sensitive?:boolean}
export function referenceKey(ref:CollectionReference){return isOriginalPageReference(ref)?`page:${ref.document_id}@${ref.version}:${ref.original_content_hash}:${ref.page}`:isOriginalFileReference(ref)?`file:${ref.document_id}@${ref.version}:${ref.original_content_hash}`:ref.memory_id ? `memory:${ref.memory_id}` : `document:${ref.document_id}:${ref.chunk_id||''}`;}
export function uniqueReferences(refs:CollectionReference[]){return [...new Map(refs.map(ref=>[referenceKey(ref),isOriginalPageReference(ref)?originalPageReference(ref):isOriginalFileReference(ref)?originalFileReference(ref):ref])).values()];}
export function normalizeRequirements(text:string){return text.split('\n').map(value=>value.trim()).filter(Boolean).slice(0,4).map(value=>value.slice(0,240));}
export function assessmentLabel(assessment?:Assessment){return assessment?.status === 'no_candidates' ? 'No candidates returned' : assessment?.status === 'weak_matches' ? 'Weak matches · review required' : 'Candidates require review';}
export function downloadText(content:string,format:'markdown'|'json',name:string){const url=URL.createObjectURL(new Blob([content],{type:format==='json'?'application/json':'text/markdown;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download=`${name.replace(/[^a-z0-9_-]/gi,'-')||'collection'}.${format==='json'?'json':'md'}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
export function citationReference(citation:string):CollectionReference|null {const file=/^\[file:([^\s\[\]@:]+)@([1-9]\d*):([0-9a-f]{64})\]$/.exec(citation);if(file){const ref={document_id:file[1],version:Number(file[2]),original_content_hash:file[3]};return isOriginalFileReference(ref)?ref:null;}const page=/^\[page:([^\s\[\]@:]+)@([1-9]\d*):([0-9a-f]{64}):([1-9]\d*)\]$/.exec(citation);if(page){const ref={document_id:page[1],version:Number(page[2]),original_content_hash:page[3],page:Number(page[4])};return isOriginalPageReference(ref)?ref:null;}const memory=/^\[mem:([^\]]+)\]$/.exec(citation);if(memory)return {memory_id:memory[1]};const source=/^\[doc:([^#\]]+)#([^\]]+)\]$/.exec(citation);return source?{document_id:source[1],chunk_id:source[2]}:null;}

export function collectionBody(draft:Collection,question:string,requirements:string,includeSensitive:boolean,idempotencyKey:string){return {name:draft.name.trim(),purpose:question.trim().slice(0,2000),requirements:normalizeRequirements(requirements),references:uniqueReferences(draft.references).slice(0,50),voice_examples:uniqueReferences(draft.voice_examples).slice(0,20),notes:draft.notes||'',reviews:reviewsForReferences([...draft.references,...draft.voice_examples],draft.reviews||[]),unresolved:draft.unresolved.map(value=>value.trim()).filter(Boolean).slice(0,20).map(value=>value.slice(0,240)),include_sensitive:includeSensitive,idempotency_key:idempotencyKey,...(draft.id?{expected_revision:draft.revision}:{})};}

export function referenceCitation(ref:CollectionReference):string|null{return isOriginalPageReference(ref)?`[page:${ref.document_id}@${ref.version}:${ref.original_content_hash}:${ref.page}]`:isOriginalFileReference(ref)?originalFileCitation(ref):ref.memory_id?`[mem:${ref.memory_id}]`:ref.document_id&&ref.chunk_id?`[doc:${ref.document_id}#${ref.chunk_id}]`:null;}
export function reviewsForReferences(refs:CollectionReference[],reviews:CollectionReview[]):CollectionReview[]{const selected=new Set(refs.map(referenceCitation).filter(Boolean));return [...new Map(reviews.filter(review=>selected.has(review.citation)).map(review=>[review.citation,review])).values()];}
export function updateCollectionReview(reviews:CollectionReview[],citation:string,patch:Partial<Pick<CollectionReview,'status'|'note'>>):CollectionReview[]{const existing=reviews.find(review=>review.citation===citation)||{citation,status:'unreviewed' as const,note:''};const next={...existing,...patch};return reviews.some(review=>review.citation===citation)?reviews.map(review=>review.citation===citation?next:review):[...reviews,next];}

/** Route navigation must use an exact citation, never an authored label or URI. */
export function retrievalPathReference(citation:string):CollectionReference|null {
 const reference=citationReference(citation);
 return reference&&reference.document_id?reference:null;
}

/** Classify actual step support; aggregate hints and review labels cannot upgrade it. */
export function routeStepSupport(step:RetrievalPathStep):'authored'|'inferred'|'unspecified' {
 if(step.support==='inferred'||step.assertion_status==='inferred'||step.assertion_status==='uncertain')return 'inferred';
 return step.support==='authored'?'authored':'unspecified';
}
export function routeProvenanceLabel(path:RetrievalPath):string {
 if(!path.steps.length)return 'Source attachment · structural';
 const supports=new Set(path.steps.map(routeStepSupport));
 if(supports.has('unspecified'))return 'Relationship route · support partly unspecified';
 if(supports.size>1)return 'Relationship route · mixed authored and inferred';
 return supports.has('authored')?'Relationship route · authored':'Relationship route · inferred';
}

/** Normalize optional bounded fields for rendering; never manufacture source text. */
export function findPacket(packet:EvidencePacket):EvidencePacket {
 const passages=packet.supporting_passages||[];
 return {...packet,visual_regions:(packet.visual_regions||[]).filter(isVisualRegion),query:packet.query||'',relevant_memories:(packet.relevant_memories||[]).map(memory=>({...memory,kind:memory.kind||'memory',verification:memory.verification||'not_determined',...(memory.provenance?{provenance:{...memory.provenance,sources:(memory.provenance.sources||[]).map(source=>({...passages.find(passage=>passage.citation===source.citation),...source}))}}:{})})),supporting_passages:passages,requirement_checks:packet.requirement_checks||[],missing_information:packet.missing_information||[],usage_guidance:packet.usage_guidance||[],retrieval_assessment:packet.retrieval_assessment||{},...(packet.source_discovery?{source_discovery:{...packet.source_discovery,candidates:(packet.source_discovery.candidates||[]).map(file=>({...file,matched_queries:file.matched_queries||[],matched_fields:file.matched_fields||[]}))}}:{})};
}
export function appendFindPage(previous:EvidencePacket,page:EvidencePacket):EvidencePacket {
 const unique=<T,>(items:T[],key:(item:T)=>string)=>[...new Map(items.map(item=>[key(item),item])).values()];
 const passages=unique([...previous.supporting_passages,...page.supporting_passages],item=>item.citation);
 const memories=unique([...previous.relevant_memories,...page.relevant_memories],item=>item.citation);
 const represented=new Set([...passages,...memories].map(item=>item.citation));
 const requirements=new Map(previous.requirement_checks.map(check=>[check.query,check]));
 for(const incoming of page.requirement_checks){const check={...incoming,assessment_page:(previous.loaded_pages||1)+1};const earlier=requirements.get(check.query);requirements.set(check.query,earlier?{...earlier,...check,selected_citations:[...new Set([...(earlier.selected_citations||[]),...(check.selected_citations||[])])],warnings:[...new Set([...(earlier.warnings||[]),...(check.warnings||[])])],retrieval_assessment:{...earlier.retrieval_assessment,...check.retrieval_assessment,candidate_citations:[...new Set([...(earlier.retrieval_assessment.candidate_citations||[]),...(check.retrieval_assessment.candidate_citations||[])])]}}:check);}
 return {...previous,...page,executed_requirements:previous.executed_requirements,visual_regions:unique([...(previous.visual_regions||[]),...(page.visual_regions||[])],item=>item.citation),loaded_pages:(previous.loaded_pages||1)+1,supporting_passages:passages,relevant_memories:memories,
 warnings:[...new Set([...(previous.warnings||[]),...(page.warnings||[])])],
 usage_guidance:[...new Set([...previous.usage_guidance,...page.usage_guidance])],
 missing_information:unique([...previous.missing_information.map(gap=>({...gap,page:gap.page||1})),...page.missing_information.map(gap=>({...gap,page:(previous.loaded_pages||1)+1}))],item=>JSON.stringify(item)),
 source_families:unique([...(previous.source_families||[]),...(page.source_families||[])],item=>JSON.stringify(item)),
 source_leads:unique([...(previous.source_leads||[]),...(page.source_leads||[])],item=>item.record_id||item.citation||JSON.stringify(item)),
 retrieval:{...previous.retrieval,...page.retrieval,warnings:[...new Set([...(previous.retrieval?.warnings||[]),...(page.retrieval?.warnings||[])])]},
 // Projection counters describe only the latest page, never accumulated global absence.
 projection:page.projection,
 memory_discovery:{...page.memory_discovery,candidates:unique([...(previous.memory_discovery?.candidates||[]),...(page.memory_discovery?.candidates||[])],item=>item.citation||item.memory_id||JSON.stringify(item))},
 enrichment_discovery:{candidates:unique([...(previous.enrichment_discovery?.candidates||[]),...(page.enrichment_discovery?.candidates||[])],item=>item.record_id||item.citation||JSON.stringify(item))},
 omitted_references:unique([...(previous.omitted_references||[]),...(page.omitted_references||[])],item=>item.citation||`${item.stream}:${item.rank}`).filter(item=>!represented.has(item.citation||'')),
 requirement_checks:[...requirements.values()],
 ...(page.source_discovery||previous.source_discovery?{source_discovery:{...(page.source_discovery||previous.source_discovery)!,candidates:unique([...(previous.source_discovery?.candidates||[]),...(page.source_discovery?.candidates||[])],item=>item.original_citation||`${item.document_id}@${item.version}:${item.content_hash}`)}}:{}),
 ...(page.retrieval_paths||previous.retrieval_paths?{retrieval_paths:{...(page.retrieval_paths||previous.retrieval_paths)!,paths:unique([...(previous.retrieval_paths?.paths||[]),...(page.retrieval_paths?.paths||[])],item=>JSON.stringify(item))}}:{})};
}

/** A continuation assessment covers its page, never all accumulated candidates. */
export function findResultsHeading(packet:EvidencePacket){
 if(packet.supporting_passages.length||packet.relevant_memories.length)return 'Sources for your question';
 if(packet.visual_regions?.length)return 'Images and sources to inspect';
 if(packet.source_discovery?.candidates.length)return 'Files to inspect';
 if(packet.omitted_references?.length||packet.retrieval_paths?.paths.length)return 'Sources to inspect';
 return 'No matching excerpts returned';
}

/** Copy only exact bodies displayed by Ask, never search labels or agent annotations. */
export function packetContextReferences(packet:EvidencePacket):CollectionReference[] {
 const passages=packet.supporting_passages.filter(passage=>passage.quote.trim()&&passage.document_id&&passage.chunk_id).map(passage=>({document_id:passage.document_id,chunk_id:passage.chunk_id}));
 const memories=packet.relevant_memories.filter(memory=>memory.text.trim()&&memory.memory_id).map(memory=>({memory_id:memory.memory_id}));
 return uniqueReferences([...passages,...memories]);
}
export function passageLocation(passage:SourcePassage):string {
 const section=passage.location?.section||'';
 const namedVersion=passage.version&&new RegExp(`\\b(?:version|v)\\s*${passage.version}\\b`,'i').test(`${section} ${passage.title||''}`);
 return [passage.location?.page?`Page ${passage.location.page}`:'',section===passage.title?'':section,passage.version&&!namedVersion?`Version ${passage.version}`:'',passage.is_latest===false?'Historical':''].filter(Boolean).join(' · ');
}

/** Recognize a question about source status, not uses such as "real-time". */
function asksAboutAuthenticity(query:string):boolean {
 const wording=query.toLowerCase().replace(/\breal[\s-]+time\b/g,'realtime');
 const status='(?:real|genuine|authentic|fiction(?:al)?|fictitious|invented|fabricated|fake|made[\\s-]+up|simulated|synthetic|demo(?:nstration)?|mock|sample)';
 // An attribute mentioned inside another question ("Who owns the fictional
 // project?") must not change that question into a fictionality check.
 return /\b(?:authenticity|fictionality)\b|\b(?:actually|really)\s+exist\b/i.test(wording)
  || new RegExp(`^(?:is|are|was|were|isn['’]t|aren['’]t|wasn['’]t|weren['’]t)\\b.+\\b${status}\\b(?:[\\s?!.,]*(?:or\\b.*)?)?$`,'i').test(wording)
  || new RegExp(`\\b(?:whether|if)\\b.+\\b${status}\\b(?:[\\s?!.,]*(?:or\\b.*)?)?$`,'i').test(wording)
  || new RegExp(`^(?:is|are|was|were|does|do)\\b.+\\b${status}\\s+(?:data|datasets?|records?|sources?|documents?|projects?|companies|company|people|persons?|examples?|scenarios?|reports?|events?|case\\s+stud(?:y|ies))\\b`,'i').test(wording);
}

/** Source wording that can address authenticity in either direction. No verdict. */
function hasAuthenticityWording(text:string):boolean {
 return /\b(?:fictional|fictitious|invented|fabricated|fake|genuine|authentic|made[\s-]+up)\b/i.test(text)
  || /\b(?:synthetic|simulated|mock|sample|demo(?:nstration)?|training|test)\s+(?:data(?:sets?)?|records?|sources?|documents?|projects?|examples?|scenarios?|reports?|events?|case\s+stud(?:y|ies))\b/i.test(text)
  || /\b(?:data(?:sets?)?|records?|examples?|scenarios?)\s+(?:(?:is|are|was|were)\s+)?(?:(?:not|entirely|all|artificially)\s+)*(?:synthetic|simulated|generated|mock)\b/i.test(text)
  || /\b(?:for|as)\s+(?:(?:a|an)\s+)?(?:demo(?:nstration)?|training|testing)\b/i.test(text)
  || /\breal(?:[\s-]+world)?\s+(?:data(?:sets?)?|records?|sources?|documents?|projects?|companies|company|people|persons?|events?)\b/i.test(text)
  || /\b(?:is|are|was|were)\s+(?:(?:not|entirely|all)\s+)*(?:real|genuine|authentic)\b/i.test(text);
}

// Mirrors memory/lexical.py for display matching only. Never rewrite source text.
function normalizedDisplayWord(word:string):string {
 let folded=word.toLowerCase().replaceAll('ß','ss').replaceAll('ς','σ');
 if(!/^\p{L}+$/u.test(word))return folded;
 folded=folded.normalize('NFKD').replace(/\p{M}/gu,'');
 if(!/^[a-z]+$/.test(folded)||folded.length<=3||new Set(['news','series','species','https','headquarters']).has(folded)||/(?:ss|us|is|ics)$/.test(folded))return folded;
 if(folded.endsWith('ies')&&folded.length>4)return folded.slice(0,-3)+'y';
 if(/(?:sses|xes|zes|ches|shes)$/.test(folded))return folded.slice(0,-2);
 return folded.endsWith('s')?folded.slice(0,-1):folded;
}

// Mirrors memory/quantity_focus.py. This count-word proximity signal neither
// infers missing quantities nor establishes that a source claim is true.
const quantityQuestionEnd=new Set('can could may might must should shall will would do does did is are was were have has had each per at in on for from to of with and or a an the you your we our i my it they their bring take borrow submit buy receive hold contain get'.split(' '));
const quantityNumbers=new Set('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million dozen'.split(' '));
const quantityCurrency=new Set('usd eur gbp jpy cad aud chf cny inr krw rub dollar euro yen rupee won rouble yuan penny pence cent'.split(' '));
const quantityUnitAttribute=/^\s+(?:(?:may|can|must|will|would|usually|typically|normally)\s+)*(?:costs?|weighs?|measures?|lasts?\b|(?:is|are)\s+(?:priced|valued)|(?:has|have)\s+(?:a\s+)?(?:weight|price|length|height|width|volume|duration))\b/i;
const quantityGapBlocked=new Set([...quantityQuestionEnd,...'by before after than that which who whose these those this its then now not never no bought sold received brought took holds contains checks weighs checked weighed return returns allowed available cost costs weigh kilogram gram kg g milligram mg litre liter ml meter metre cm mm km mile inch foot feet pound lb ounce oz hour minute second day week month year january february march april june july august september october november december jan feb mar apr jun jul aug sep oct nov dec'.split(' ')]);
const quantityWords=(text:string)=>[...text.matchAll(/\d+(?:[.,]\d+)*|[\p{L}]+(?:['’][\p{L}]+)?/gu)];
function quantityTarget(query:string):string|undefined {
 const prefix=/\b(?:how\s+many|number\s+of)\s+/i.exec(query);
 if(!prefix)return undefined;
 const words:string[]=[];
 for(const word of quantityWords(query.slice(prefix.index+prefix[0].length))){
  const raw=word[0].toLowerCase();
  if(quantityQuestionEnd.has(raw)||words.length===4)break;
  const normalized=normalizedDisplayWord(raw);
  words.push(normalized);
  if(raw.endsWith('s')&&normalized!==raw)return normalized;
 }
 return words.at(-1);
}
function quantityMatchScore(text:string,query:string):number {
 const target=quantityTarget(query);
 if(!target)return 0;
 const identifiers=(value:string)=>new Set([...value.matchAll(/\b(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9]+(?:-[a-z0-9]+)*\b/gi)].map(match=>match[0].toLowerCase()));
 const queryIds=identifiers(query),sourceIds=identifiers(text);
 if(queryIds.size&&sourceIds.size&&![...queryIds].some(id=>sourceIds.has(id)))return 0;
 if(/\b(?:not|never|cannot)\b|n['’]t\b|\bno\b(?!\s+(?:more|fewer|less)\s+than\b)/i.test(text))return 0;
 const tokens=quantityWords(text);
 for(let index=0;index<tokens.length;index++){
  if(normalizedDisplayWord(tokens[index][0])!==target)continue;
  // Reject per-unit attributes, without suppressing counts beside separate prices.
  if(quantityUnitAttribute.test(text.slice(tokens[index].index!+tokens[index][0].length)))continue;
  for(let quantityIndex=Math.max(0,index-3);quantityIndex<index;quantityIndex++){
   const quantity=tokens[quantityIndex],raw=quantity[0].toLowerCase(),numeric=/^\d+(?:[.,]\d+)*$/.test(raw);
   if(!numeric&&!quantityNumbers.has(raw))continue;
   if(quantityIndex>0&&quantityCurrency.has(tokens[quantityIndex-1][0].toLowerCase()))continue;
   const before=text.slice(0,quantity.index!),after=text.slice(quantity.index!+quantity[0].length);
   if(numeric&&(/[\w:/.$£€¥₹₩₽-]$/.test(before)||/^[:/.-]\w/.test(after)))continue;
   const gap=tokens.slice(quantityIndex+1,index);
   if(gap.some(word=>quantityGapBlocked.has(word[0].toLowerCase())||quantityCurrency.has(word[0].toLowerCase())||(normalizedDisplayWord(word[0])!==word[0].toLowerCase()&&!quantityNumbers.has(word[0].toLowerCase()))||/(?:ed|ing)$/i.test(word[0])))continue;
   if(tokens.slice(quantityIndex,index).some((left,offset)=>!/^[\s-]+$/.test(text.slice(left.index!+left[0].length,tokens[quantityIndex+offset+1].index!))))continue;
   return 1;
  }
 }
 return 0;
}

/** A conservative source window; matching words do not establish an answer. */
export function passageExcerpt(quote:string,query:string):{text:string;leading:boolean;trailing:boolean;clipped:boolean;focusStart?:number;focusEnd?:number} {
 const full={text:quote,leading:false,trailing:false,clipped:false};
 const quantity=quantityTarget(query);
 if(quote.length<=140&&!quantity)return full;
 const stop=new Set('the and for that this with from what when where which who whom whose why how was were are does did has have had can could would should about into than then there their they them these those your you our its any all more find show tell give please looking need know'.split(' '));
 const terms=new Set((query.toLowerCase().match(/[\p{L}\p{N}]{3,}/gu)||[]).filter(term=>!stop.has(term)&&(!quantity||!['many','number'].includes(term))).map(normalizedDisplayWord));
 if(!terms.size)return full;
 const authenticity=asksAboutAuthenticity(query);
 const paragraphs:{start:number;end:number}[]=[];
 let paragraphStart=0;
 for(const separator of quote.matchAll(/\r?\n[ \t]*(?:\r?\n)+/g)){
  paragraphs.push({start:paragraphStart,end:separator.index!});
  paragraphStart=separator.index!+separator[0].length;
 }
 paragraphs.push({start:paragraphStart,end:quote.length});
 // Rank a short, unpunctuated initial title line as structure, not an answer.
 const initial=quote.slice(0,paragraphs[0].end).trim();
 const plainTitleEnd=paragraphs.length>1&&initial.length>0&&initial.length<=120&&!initial.includes('\n')&&!/[.!?;:]["”’')\]]*$/.test(initial)?paragraphs[0].end:0;
 const units:{start:number;end:number;score:number;matches:string[];structural:boolean;statusMatch:boolean;quantityMatch:number}[]=[];
 // Keep complete sentences and paragraphs; soft line wraps must not split meaning.
 // Offsets always refer to the raw source, including its original whitespace.
 const boundaries=[...new Set([...quote.matchAll(/[.!?]["”’')\]]*(?=\s|$)|\r?\n[ \t]*(?:\r?\n)+/gu),...quote.matchAll(/^#{1,6}[^\n]*(?:\n|$)/gm)].map(match=>match.index!+match[0].length))].sort((a,b)=>a-b);
 if(boundaries.at(-1)!==quote.length)boundaries.push(quote.length);
 let start=0;
 for(const end of boundaries){
  const text=quote.slice(start,end);
  if(text.trim()){
   const words=new Set((text.toLowerCase().match(/[\p{L}\p{N}]{3,}/gu)||[]).map(normalizedDisplayWord));
   const matches=[...terms].filter(term=>words.has(term));
   const structural=!!(plainTitleEnd&&start<plainTitleEnd)||/^\s*#{1,6}\s/.test(text)||/^\s*(?:date|author|title|version)\s*:[^\n]*\s*$/i.test(text);
   units.push({start,end,score:0,matches,structural,statusMatch:authenticity&&hasAuthenticityWording(text),quantityMatch:quantity?quantityMatchScore(text,query):0});
  }
  start=end;
 }
 // Repeated subject names identify the document; rarer question terms identify
 // the sentence to read. Heading matches alone must not displace source prose.
 const frequency=new Map([...terms].map(term=>[term,units.filter(unit=>unit.matches.includes(term)).length]));
 const headingTerms=new Set(units.filter(unit=>unit.structural).flatMap(unit=>unit.matches));
 const contentTerms=new Set([...terms].filter(term=>!headingTerms.has(term)));
 for(const unit of units){
  const overlap=unit.matches.reduce((score,term)=>score+1/(frequency.get(term)||1),0);
  // For explicit authenticity questions, source-status wording matters more
  // than repeated project names. This changes presentation only: it neither
  // verifies a claim nor filters sources, and negation remains in the sentence.
  unit.score=unit.structural?0:overlap+(unit.statusMatch?terms.size+1:0)+(unit.quantityMatch?terms.size+1:0);
 }
 const best=units.reduce((selected,unit,index)=>unit.score>(units[selected]?.score||0)?index:selected,-1);
 if(best<0)return full;
 let focusFrom=units[best].start,focusTo=units[best].end;
 while(focusFrom<focusTo&&/\s/.test(quote[focusFrom]))focusFrom++;
 while(focusTo>focusFrom&&/\s/.test(quote[focusTo-1]))focusTo--;
 const paragraph=paragraphs.find(item=>item.start<=focusFrom&&focusFrom<item.end)!;
 let first=best,last=best;
 while(first>0&&units[first-1].end>paragraph.start&&!units[first-1].structural)first--;
 while(last+1<units.length&&units[last+1].start<paragraph.end&&!units[last+1].structural)last++;
 // Preserve all qualifications in ordinary paragraphs. Longer paragraphs use
 // a neighboring-sentence preview with no selective emphasis; full text remains
 // available through Read in context. This is a soft, whole-sentence bound.
 const longParagraph=units[last].end-units[first].start>800;
 if(longParagraph){
  first=Math.max(0,best-1);last=Math.min(units.length-1,best+1);
  while(first<best&&units[first].structural)first++;
  while(last>best&&units[last].structural)last--;
 }
 let from=units[first].start,to=units[last].end;
 while(from<to&&/\s/.test(quote[from]))from++;
 while(to>from&&/\s/.test(quote[to-1]))to--;
 const leading=!!quote.slice(0,from).trim(),trailing=!!quote.slice(to).trim();
 const result={text:quote.slice(from,to),leading,trailing,clipped:leading||trailing};
 const strongMatch=quantity?!!units[best].quantityMatch:units[best].statusMatch||(contentTerms.size>0&&units[best].matches.filter(term=>contentTerms.has(term)).length>=Math.min(2,contentTerms.size));
 // Conservatively remove emphasis; do not claim to resolve contradictions.
 const qualification=/\b(?:no|not|never|without|only|however|although|unless|except|unverified|unconfirmed|unknown|untested|pending)\b|n['’]t\b/i;
 const surroundingQualification=units.some((unit,index)=>index>=first&&index<=last&&index!==best&&qualification.test(quote.slice(unit.start,unit.end)));
 if(longParagraph||!strongMatch||surroundingQualification)return result;
 return {...result,focusStart:focusFrom-from,focusEnd:focusTo-from};
}
