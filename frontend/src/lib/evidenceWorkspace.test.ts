import {findPacket,appendFindPage,findResultsHeading,packetContextReferences,passageExcerpt} from './evidenceWorkspace';
import {describe,it,expect} from 'vitest';
import {assessmentLabel,citationReference,retrievalPathReference,collectionBody,normalizeRequirements,referenceCitation,reviewsForReferences,updateCollectionReview,uniqueReferences,type Collection} from './evidenceWorkspace';
describe('Evidence workspace boundaries',()=>{
 it('bounds independent requirements without inventing coverage',()=>{expect(normalizeRequirements(' Role?\n\nOutcome?\nDate?\nSource?\nIgnored')).toEqual(['Role?','Outcome?','Date?','Source?']);expect(normalizeRequirements('x'.repeat(300))[0]).toHaveLength(240);expect(assessmentLabel({status:'review_required'})).toBe('Candidates require review');expect(assessmentLabel({status:'weak_matches'})).toBe('Weak matches · review required');expect(assessmentLabel({status:'no_candidates'})).toBe('No candidates returned');});
 it('accepts exact citation coordinates only',()=>{expect(citationReference('[mem:abc]')).toEqual({memory_id:'abc'});expect(citationReference('[doc:source#chunk]')).toEqual({document_id:'source',chunk_id:'chunk'});expect(citationReference('possibly [mem:abc]')).toBeNull();expect(citationReference('[doc:source]')).toBeNull();});
 it('keeps distinct source passages and deduplicates the same reference',()=>{expect(uniqueReferences([{memory_id:'a'},{memory_id:'a'},{document_id:'a',chunk_id:'b'},{document_id:'a',chunk_id:'c'}])).toHaveLength(3);});
 it('writes only permitted revisioned reference fields, with privacy and retry identity',()=>{const draft:Collection={id:'collection',revision:3,name:' Portfolio ',purpose:'old',requirements:[],references:[{memory_id:'a'}],voice_examples:[{document_id:'d',chunk_id:'c'}],unresolved:['','  Role? '],sensitive:true};expect(collectionBody(draft,' Question ','Role?\nOutcome?',true,'retry')).toEqual({name:'Portfolio',purpose:'Question',requirements:['Role?','Outcome?'],references:[{memory_id:'a'}],voice_examples:[{document_id:'d',chunk_id:'c'}],notes:'',reviews:[],unresolved:['Role?'],include_sensitive:true,idempotency_key:'retry',expected_revision:3});expect(collectionBody({...draft,id:''},'','',false,'new')).not.toHaveProperty('expected_revision');});
});

describe('User-authored collection review metadata',()=>{
 it('roundtrips notes and exact memory/source review coordinates without changing status meaning',()=>{const draft:Collection={id:'c',revision:4,name:'Application',requirements:[],references:[{memory_id:'m'}],voice_examples:[{document_id:'d',chunk_id:'p'}],unresolved:[],notes:'My interpretation, not a verified fact.',reviews:[{citation:'[mem:m]',status:'reviewed',note:'I read this.'},{citation:'[doc:d#p]',status:'needs_context',note:'Check the preceding page.'}]};const body=collectionBody(draft,'Purpose','',false,'retry');expect(body.notes).toBe(draft.notes);expect(body.reviews).toEqual(draft.reviews);const restored={...draft,...JSON.parse(JSON.stringify(body))};expect(collectionBody(restored,'Purpose','',false,'retry')).toEqual(body);});
 it('removes stale reviews only after a citation leaves both evidence and voice references',()=>{const source={document_id:'d',chunk_id:'p'};const reviews=[{citation:'[doc:d#p]',status:'needs_context' as const,note:'Read context'},{citation:'[mem:removed]',status:'reviewed' as const,note:'Old reference'}];expect(reviewsForReferences([source],reviews)).toEqual([reviews[0]]);expect(reviewsForReferences([],reviews)).toEqual([]);expect(referenceCitation({memory_id:'m'})).toBe('[mem:m]');expect(referenceCitation(source)).toBe('[doc:d#p]');expect(referenceCitation({document_id:'d'})).toBeNull();});
 it('edits one review without mutating another or replacing its note',()=>{const first=updateCollectionReview([],'[mem:m]',{note:'My own assessment'});expect(first).toEqual([{citation:'[mem:m]',status:'unreviewed',note:'My own assessment'}]);const next=updateCollectionReview(first,'[mem:m]',{status:'reviewed'});expect(next[0].note).toBe('My own assessment');expect(first[0].status).toBe('unreviewed');expect(updateCollectionReview(next,'[doc:d#p]',{status:'needs_context'})).toHaveLength(2);});
});


describe('Original-page collection coordinates',()=>{
 const page={document_id:'source',version:3,original_content_hash:'a'.repeat(64),page:2};
 const citation=`[page:source@3:${page.original_content_hash}:2]`;
 it('roundtrips strict page citations and keeps versions, hashes and pages distinct',()=>{
  expect(citationReference(citation)).toEqual(page);expect(referenceCitation(page)).toBe(citation);
  expect(uniqueReferences([page,{...page},{...page,version:4},{...page,page:3},{...page,original_content_hash:'b'.repeat(64)},{document_id:'source',chunk_id:'chunk'}])).toHaveLength(5);
  for(const invalid of [citation.replace('@3:','@03:'),citation.replace(':2]',':0]'),citation.replace(':2]',':1000001]'),citation.replace('@3:','@true:'),citation.replace('@3:','@9007199254740992:'),citation.replace(page.original_content_hash,'A'.repeat(64)),citation+' trailing'])expect(citationReference(invalid)).toBeNull();
 });
 it('persists references without pixels, keeps page review coordinates and revision retry fields',()=>{
  const draft:Collection={id:'c',revision:2,name:'Scan',requirements:[],references:[{...page,image:{data:'not stored'}} as any],voice_examples:[page],unresolved:[],notes:'My observation of the scan',reviews:[{citation,status:'needs_context',note:'Read adjacent page'}]};
  const body=collectionBody(draft,'Purpose','',false,'retry');expect(body.references).toEqual([page]);expect(body.voice_examples).toEqual([page]);expect(body.reviews).toEqual(draft.reviews);expect(body.expected_revision).toBe(2);expect(body.idempotency_key).toBe('retry');expect(JSON.stringify(body)).not.toContain('not stored');
  expect(collectionBody({...draft,...JSON.parse(JSON.stringify(body))},'Purpose','',false,'retry')).toEqual(body);
  expect(reviewsForReferences([],draft.reviews!)).toEqual([]);
 });
});

describe('Relationship route navigation',()=>{
 it('accepts exact source references without turning labels or memory claims into reader links',()=>{
  expect(retrievalPathReference('[doc:historical#chunk-2]')).toEqual({document_id:'historical',chunk_id:'chunk-2'});
  expect(retrievalPathReference('[mem:claim]')).toBeNull();
  expect(retrievalPathReference('<img src=x onerror=alert(1)>')).toBeNull();
  expect(retrievalPathReference('https://example.test/source')).toBeNull();
 });
});

describe('Find page diagnostic retention',()=>{
 it('merges exact diagnostics and requirement references while keeping omission counts scoped to the latest page',()=>{
  const base=findPacket({query:'q',relevant_memories:[],supporting_passages:[],requirement_checks:[],missing_information:[],usage_guidance:[],retrieval_assessment:{}});
  const family={family_id:'f',document_ids:['d'],citations:['[doc:d#c]'],duplicate_excerpt_count:0,independent_corroboration:'not_determined'};
  const gap={question:'Inspect outcome',field:'outcome'};
  const first={...base,warnings:['first warning'],retrieval:{warnings:['retrieval warning']},usage_guidance:['review first'],missing_information:[gap],source_families:[family],source_leads:[{citation:'[doc:d#c]'}],projection:{omitted_reference_count:7},requirement_checks:[{query:'outcome',selected_citations:['[doc:d#c]'],retrieval_assessment:{candidate_citations:['[mem:a]']},warnings:['requirement warning'],next_step:'Open'}]};
  const second={...base,warnings:['second warning'],projection:{omitted_reference_count:2},requirement_checks:[{query:'outcome',selected_citations:['[doc:d#b]'],retrieval_assessment:{candidate_citations:['[mem:b]']},warnings:[],next_step:'Open'}]};
  const merged=appendFindPage(first,second);
  expect(merged.warnings).toEqual(['first warning','second warning']);expect(merged.retrieval?.warnings).toEqual(['retrieval warning']);expect(merged.usage_guidance).toEqual(['review first']);expect(merged.missing_information).toEqual([{...gap,page:1}]);expect(merged.source_families).toEqual([family]);expect(merged.source_leads).toEqual([{citation:'[doc:d#c]'}]);expect(merged.requirement_checks[0].selected_citations).toEqual(['[doc:d#c]','[doc:d#b]']);expect(merged.requirement_checks[0].retrieval_assessment.candidate_citations).toEqual(['[mem:a]','[mem:b]']);expect(merged.requirement_checks[0].warnings).toEqual(['requirement warning']);expect(merged.projection?.omitted_reference_count).toBe(2);
 });
});

describe('Accumulated Find assessment scope',()=>{
 it('keeps an empty initial-page label and scopes later weak assessment separately',()=>{
  const empty=findPacket({query:'q',relevant_memories:[],supporting_passages:[],requirement_checks:[],missing_information:[],usage_guidance:[],retrieval_assessment:{status:'no_candidates'}});
  expect(findResultsHeading(empty)).toBe('No matching excerpts returned');
  const first={...empty,supporting_passages:[{document_id:'d',chunk_id:'c',citation:'[doc:d#c]',quote:'Exact wording'}],retrieval_assessment:{status:'review_required'}};
  const combined=appendFindPage(first,{...empty,retrieval_assessment:{status:'weak_matches'}});
  expect(combined.loaded_pages).toBe(2);expect(findResultsHeading(combined)).toBe('Sources for your question');expect(combined.retrieval_assessment.status).toBe('weak_matches');
 });
});


describe('Exact loaded context references',()=>{
 it('deduplicates exact visible text bodies and excludes omissions and derived annotations',()=>{
  const packet=findPacket({query:'bag',supporting_passages:[{document_id:'d',chunk_id:'c',citation:'[doc:d#c]',quote:'Original wording'},{document_id:'d',chunk_id:'c',citation:'[doc:d#c]',quote:'Original wording'},{document_id:'d',chunk_id:'blank',citation:'[doc:d#blank]',quote:' '}],relevant_memories:[{memory_id:'m',citation:'[mem:m]',text:'Memory wording',kind:'fact',verification:'unverified'}],source_leads:[{derived_text:'An inference',citation:'[doc:other#lead]'}],omitted_references:[{citation:'[doc:hidden#body]',stream:'passages',rank:1}],requirement_checks:[],missing_information:[],usage_guidance:[],retrieval_assessment:{}});
  expect(packetContextReferences(packet)).toEqual([{document_id:'d',chunk_id:'c'},{memory_id:'m'}]);
 });
});


describe('Display-only source excerpts',()=>{
 it('selects a raw contiguous window near matching text and keeps adjacent qualifications and negation',()=>{
  const prefix='# Project archive\n\n'+('Catalog entry for unrelated administrative material. '.repeat(24));
  const context='The following sentence describes an earlier proposal, not the final decision. The weekly review planned to approve the launch. It did not approve the launch because the safety checks failed. The next review will revisit those unresolved checks.';
  const quote=prefix+context+(' Further archival notes describe unrelated scheduling details.'.repeat(20));
  const excerpt=passageExcerpt(quote,'Did the weekly review approve the launch?');
  expect(excerpt.clipped).toBe(true);expect(excerpt.leading).toBe(true);expect(excerpt.trailing).toBe(true);
  expect(quote.includes(excerpt.text)).toBe(true);expect(excerpt.text.length).toBeLessThanOrEqual(600);
  expect(excerpt.text).toContain('earlier proposal, not the final decision.');expect(excerpt.text).toContain('It did not approve the launch because the safety checks failed.');
  expect(excerpt.text).not.toContain('# Project archive');expect(excerpt.text).not.toContain('…');
 });
 it('keeps short text and unmatched long sources intact instead of inventing a relevant excerpt',()=>{
  expect(passageExcerpt('It was not approved.','approved').text).toBe('It was not approved.');
  const quote='Unrelated archival wording. '.repeat(40);expect(passageExcerpt(quote,'bag design')).toEqual({text:quote,leading:false,trailing:false,clipped:false});
 });
});
