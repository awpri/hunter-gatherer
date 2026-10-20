import {describe,expect,it} from 'vitest';
import {searchIndexLabel,shouldPollSearchIndex,shouldPollImport} from './LibraryImports';
describe('Import search-index readiness',()=>{
 it('does not infer readiness from ingestion success or absent readiness data',()=>{expect(searchIndexLabel()).toContain('unavailable');expect(searchIndexLabel({status:'pending',ready:false,lexical_pending:true,vector_pending:true})).toBe('Search index pending · lexical preparation pending · vector preparation pending');});
 it('accepts ready without native vector acceleration while disclosing pending acceleration',()=>{expect(searchIndexLabel({status:'ready',ready:true,vector_pending:true,vector_acceleration:false})).toBe('Search index ready · vector acceleration pending or unavailable');});
 it('reports current readiness independently from a prior failed attempt',()=>{expect(searchIndexLabel({status:'ready',ready:true})).toBe('Search index ready');});
});

describe('Import readiness polling',()=>{
 it('continues after ingestion when preparation is pending, absent or busy',()=>{for(const status of ['pending','not_installed','busy'])expect(shouldPollSearchIndex({status,ready:false})).toBe(true);});
 it('stops on ready, not required, failure or unavailable acceleration with readiness',()=>{for(const status of ['ready','not_required','failed','cancelled','unavailable'])expect(shouldPollSearchIndex({status,ready:status==='ready'})).toBe(false);expect(shouldPollSearchIndex({status:'pending',ready:true,vector_pending:true})).toBe(false);expect(shouldPollSearchIndex()).toBe(false);});
});

 describe('Import worker polling',()=>{
 it('keeps queued retries and running dispatchers polling when the archive index is ready',()=>{
  for(const worker_state of ['queued','running'])expect(shouldPollImport({job_id:'retry',complete:false,total_items:1,counts:{pending:1,running:0,failed:0},worker_state,search_index:{status:'ready',ready:true}})).toBe(true);
 });
 it('stops when idle and ready and tolerates an absent detail response',()=>{
  expect(shouldPollImport({job_id:'done',complete:true,total_items:1,counts:{succeeded:1,running:0},worker_state:'idle',search_index:{status:'ready',ready:true}})).toBe(false);
  expect(shouldPollImport(null)).toBe(false);
 });
});
