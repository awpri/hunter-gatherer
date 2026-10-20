import {describe,expect,it} from 'vitest';
import {originalSourceHref,tableOmissionMessage,tableStructure} from './tableCells';
import {packet,cell} from '../views/tableCellFixtures';

describe('Saved table-cell transport and original route',()=>{
 it('keeps overlapping, inconsistent, out-of-range raw predictions and exact order',()=>{
  const raw=packet(0,[cell(1),cell(2,{text:'0.10',row_span:-1,start_col_offset_idx:-4})],{observations:['overlapping_cells','inconsistent_spans','out_of_range_offsets']});
  const result=tableStructure(raw,'doc-1','chunk-1',3,0)!;
  expect(result.cells).toEqual(raw.table_structure.cells);expect(result.observations).toEqual(raw.table_structure.observations);
 });
 it('refuses mismatched version, document, anchor and malformed cell transports',()=>{
  for(const change of [{version:4},{document_id:'other'},{anchor_citation:'other'},{schema_version:2},{scope:'verified'},{cells:[cell(1,{text:undefined})]},{cells:[cell(1),cell(1)]},{next_cell_offset:-1}])expect(tableStructure(packet(0,[],change),'doc-1','chunk-1',3,0)).toBeNull();
  const raw=packet();raw.chunks=[];expect(tableStructure(raw,'doc-1','chunk-1',3,0)).toBeNull();
 });
 it('accepts omitted oversized cells and unknown dimensions only for omitted snapshots',()=>{
  expect(tableStructure(packet(0,[],{omitted_cells:[{ordinal:1,reason:'cell_size_limit'}]}),'doc-1','chunk-1',3,0)?.omitted_cells).toEqual([{ordinal:1,reason:'cell_size_limit'}]);
  expect(tableStructure(packet(0,[],{status:'omitted',num_rows:null,num_cols:null,orientation:null,omission_reason:'invalid_dimensions'}),'doc-1','chunk-1',3,0)?.status).toBe('omitted');
 });
 it('distinguishes old snapshots, size limits and budget omissions',()=>{
  expect(tableOmissionMessage('not_saved')).toContain('No saved table cells');expect(tableOmissionMessage('invalid_metadata')).toContain('invalid metadata');expect(tableOmissionMessage('section_size_limit')).toContain('size limit');expect(tableOmissionMessage('output_budget')).toContain('read budget');
 });
 it('opens the exact historical document with privacy and known PDF page preserved',()=>{
  expect(originalSourceHref({document_id:'history/id',media_type:'application/pdf'},true,9)).toBe('/api/library/documents/history%2Fid/original?include_sensitive=true#page=9');
  for(const page of [undefined,null,0,-1,NaN])expect(originalSourceHref({document_id:'history/id',media_type:'application/pdf'},false,page)).toBe('/api/library/documents/history%2Fid/original?include_sensitive=false');
  expect(originalSourceHref({document_id:'history/id',media_type:'text/plain'},false,9)).not.toContain('#');
 });
});
