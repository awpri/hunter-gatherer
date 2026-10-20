import type {LibraryDocument} from './libraryApi';
import type {SourcePassage} from './evidenceWorkspace';

export interface TableCell {
 ordinal:number;kind:'text'|'rich';text:string;ref:string|null;
 row_span:number;col_span:number;start_row_offset_idx:number;end_row_offset_idx:number;
 start_col_offset_idx:number;end_col_offset_idx:number;
 column_header:boolean;row_header:boolean;row_section:boolean;fillable:boolean;
 bbox:{l:number;t:number;r:number;b:number;coord_origin:string}|null;
}
export interface TableStructure {
 schema_version:number;scope:string;status:'complete'|'omitted';document_id:string;version:number;
 anchor_citation:string;table_ref:string;num_rows:number|null;num_cols:number|null;orientation:string|null;
 total_cell_count:number;cell_offset:number;cells:TableCell[];omitted_cells:{ordinal:number;reason:string}[];
 next_cell_offset:number|null;budget_blocked:boolean;observations:string[];omission_reason:string|null;
}
export interface TableWindow {chunks:SourcePassage[];table_structure?:unknown;table_structure_omitted?:{reason:string}}
const record=(value:unknown):value is Record<string,any>=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const integer=(value:unknown)=>Number.isSafeInteger(value);
const nonnegative=(value:unknown)=>integer(value)&&Number(value)>=0;
const cellIntegers=['row_span','col_span','start_row_offset_idx','end_row_offset_idx','start_col_offset_idx','end_col_offset_idx'];
const flags=['column_header','row_header','row_section','fillable'];
function validCell(value:unknown):value is TableCell {
 if(!record(value)||!integer(value.ordinal)||value.ordinal<1||typeof value.text!=='string'||!['text','rich'].includes(value.kind))return false;
 if(value.kind==='text'?value.ref!==null:typeof value.ref!=='string')return false;
 if(!cellIntegers.every(key=>integer(value[key])&&Math.abs(value[key])<=1000000)||!flags.every(key=>typeof value[key]==='boolean'))return false;
 return value.bbox===null||(record(value.bbox)&&['l','t','r','b'].every(key=>typeof value.bbox[key]==='number'&&Number.isFinite(value.bbox[key]))&&['TOPLEFT','BOTTOMLEFT'].includes(value.bbox.coord_origin));
}
/** Fail closed on malformed transport; representable invalid predictions remain unchanged. */
export function tableStructure(packet:TableWindow,documentId:string,chunkId:string,version:number,offset:number):TableStructure|null {
 const value=packet.table_structure,anchor=packet.chunks?.find(chunk=>chunk.chunk_id===chunkId);
 if(!record(value)||!anchor||anchor.document_id!==documentId||typeof anchor.quote!=='string'||typeof anchor.citation!=='string'
  ||anchor.version!==version||value.document_id!==documentId||value.version!==version||value.anchor_citation!==anchor.citation
  ||value.schema_version!==1||value.scope!=='unverified_docling_table_model_observations'||!['complete','omitted'].includes(value.status)
  ||!nonnegative(value.total_cell_count)||value.cell_offset!==offset||typeof value.table_ref!=='string'
  ||!Array.isArray(value.cells)||value.cells.length>64||!value.cells.every(validCell)
  ||!Array.isArray(value.omitted_cells)||value.omitted_cells.length>64||!value.omitted_cells.every((cell:unknown)=>record(cell)&&integer(cell.ordinal)&&cell.ordinal>0&&typeof cell.reason==='string')
  ||!Array.isArray(value.observations)||!value.observations.every((item:unknown)=>typeof item==='string')
  ||typeof value.budget_blocked!=='boolean'||!(value.next_cell_offset===null||(nonnegative(value.next_cell_offset)&&value.next_cell_offset>=offset&&value.next_cell_offset<value.total_cell_count))
  ||!(value.omission_reason===null||typeof value.omission_reason==='string')
  ||!['num_rows','num_cols'].every(key=>(value.status==='omitted'&&value[key]===null)||(nonnegative(value[key])&&value[key]<=10000))
  ||!(value.orientation===null&&value.status==='omitted'||['rot_0','rot_90','rot_180','rot_270'].includes(value.orientation)))return null;
 const ordinals=[...value.cells,...value.omitted_cells].map(cell=>cell.ordinal);
 if(new Set(ordinals).size!==ordinals.length||ordinals.some(ordinal=>ordinal<=offset||ordinal>value.total_cell_count)
  ||value.status==='omitted'&&(ordinals.length>0||value.omission_reason===null))return null;
 return value as TableStructure;
}
export function originalSourceHref(doc:Pick<LibraryDocument,'document_id'|'media_type'>,includePrivate:boolean,page?:number|null){
 const fragment=doc.media_type==='application/pdf'&&nonnegative(page)&&Number(page)>0?`#page=${page}`:'';
 return `/api/library/documents/${encodeURIComponent(doc.document_id)}/original?include_sensitive=${includePrivate}${fragment}`;
}
export function tableOmissionMessage(reason?:string){
 if(reason==='not_saved')return 'No saved table cells for this source version. Older or non-table passages may have no snapshot.';
 if(reason==='output_budget'||reason==='anchor_exceeds_output_budget')return 'Table inspection did not fit the read budget. Try the larger budget or open the original.';
 if(reason==='section_size_limit')return 'This section exceeds the cell inspection size limit.';
 if(reason)return `Table cells unavailable: ${reason.replaceAll('_',' ')}. Source text remains available.`;
 return 'No table-cell summary was returned. Try the larger budget or open the original.';
}
