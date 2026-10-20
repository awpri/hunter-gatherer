import {describe,expect,it} from 'vitest';
import {isOriginalFileReference,originalFileCitation,fileLeadReference} from './originalFiles';
import {citationReference,referenceKey,uniqueReferences,collectionBody,type Collection} from './evidenceWorkspace';
const ref={document_id:'asset-1',version:2,original_content_hash:'a'.repeat(64)};
describe('Original file selections',()=>{
 it('preserves exact identity through citation parsing and collection saves',()=>{
  expect(citationReference(originalFileCitation(ref))).toEqual(ref);
  const draft:Collection={id:'',revision:0,name:'Asset',requirements:[],references:[ref],voice_examples:[],unresolved:[]};
  expect(collectionBody(draft,'Question','',false,'retry').references).toEqual([ref]);
  expect(uniqueReferences([ref,ref])).toEqual([ref]);
  expect(referenceKey(ref)).not.toBe(referenceKey({...ref,page:1}));
 });
 it('rejects extra prose, ambiguous pages and mismatched lead identity',()=>{
  expect(isOriginalFileReference({...ref,quote:'invented'})).toBe(false);
  expect(isOriginalFileReference({...ref,page:1})).toBe(false);
  expect(fileLeadReference({document_id:'asset-1',version:3,content_hash:ref.original_content_hash,source_reference:ref})).toBeNull();
 });
});
