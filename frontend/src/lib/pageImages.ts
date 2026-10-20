import type {LibraryDocument} from './libraryApi';
import type {SourcePassage} from './evidenceWorkspace';

export function documentOriginalHash(doc:LibraryDocument):string|undefined {
 if(doc.original_content_hash!==undefined&&doc.content_hash!==undefined&&doc.original_content_hash!==doc.content_hash)return '';
 return doc.original_content_hash??doc.content_hash;
}
export interface OriginalPageReference {document_id:string;version:number;original_content_hash:string;page:number}
export function isOriginalPageReference(value:unknown):value is OriginalPageReference {
 if(!value||typeof value!=='object'||Array.isArray(value))return false;
 const ref=value as Record<string,unknown>;
 return typeof ref.document_id==='string'&&ref.document_id.length<=100&&/^[^\s\[\]@:\x00]+$/.test(ref.document_id)&&Number.isSafeInteger(ref.version)&&Number(ref.version)>0&&typeof ref.original_content_hash==='string'&&/^[0-9a-f]{64}$/.test(ref.original_content_hash)&&Number.isSafeInteger(ref.page)&&Number(ref.page)>0&&Number(ref.page)<=1_000_000;
}
export function originalPageReference(ref:OriginalPageReference):OriginalPageReference {return {document_id:ref.document_id,version:ref.version,original_content_hash:ref.original_content_hash,page:ref.page};}
export interface OriginalPageImage {
 document_id:string;version:number;original_content_hash:string;page:number;page_count:number;
 width:number;height:number;mime_type:'image/png';image_bytes:number;
 image:{mime_type:'image/png';data:string};
 source_reference:OriginalPageReference;
}
const record=(value:unknown):value is Record<string,any>=>!!value&&typeof value==='object'&&!Array.isArray(value);
const positive=(value:unknown)=>Number.isSafeInteger(value)&&Number(value)>0;
/** Only an exact source-version anchor supplies the initial image page. */
export function originalPageStart(doc:LibraryDocument,chunkId?:string,chunks:SourcePassage[]=[],selectedPage?:number){
 const anchor=chunks.find(chunk=>chunk.chunk_id===chunkId&&chunk.document_id===doc.document_id&&chunk.version===doc.version);
 const page=anchor?.location?.page;
 return positive(page)?Number(page):positive(selectedPage)?Number(selectedPage):1;
}
/** Fail closed before placing bounded PNG pixels in the DOM. Never persist this packet. */
export function validateOriginalPage(value:unknown,doc:LibraryDocument,page:number):OriginalPageImage|null {
 if(!record(value)||value.document_id!==doc.document_id||value.version!==doc.version||value.page!==page
  ||!isOriginalPageReference(value.source_reference)
  ||(documentOriginalHash(doc)!==undefined&&value.original_content_hash!==documentOriginalHash(doc))
  ||!positive(value.page_count)||page>value.page_count||value.mime_type!=='image/png'
  ||!positive(value.width)||!positive(value.height)||value.width>3072||value.height>3072||value.width*value.height>4_000_000
  ||!positive(value.image_bytes)||value.image_bytes>2*1024*1024
  ||!record(value.source_reference)||value.source_reference.document_id!==doc.document_id||value.source_reference.version!==doc.version
  ||value.source_reference.page!==page||value.source_reference.original_content_hash!==value.original_content_hash
  ||!record(value.image)||value.image.mime_type!=='image/png'||typeof value.image.data!=='string')return null;
 const data=value.image.data;
 if(data.length>Math.ceil(2*1024*1024/3)*4||data.length%4!==0||!/^iVBORw0KGgo[A-Za-z0-9+/]*={0,2}$/.test(data))return null;
 const bytes=data.length/4*3-(data.endsWith('==')?2:data.endsWith('=')?1:0);
 return bytes===value.image_bytes?value as OriginalPageImage:null;
}
