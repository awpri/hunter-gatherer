/** A file reference identifies original bytes, not an extracted quotation. */
export interface OriginalFileReference {document_id:string;version:number;original_content_hash:string;page?:never}
export function isOriginalFileReference(value:unknown):value is OriginalFileReference {
 if(!value||typeof value!=='object'||Array.isArray(value))return false;
 const ref=value as Record<string,unknown>;
 return Object.keys(ref).sort().join(',')==='document_id,original_content_hash,version'&&typeof ref.document_id==='string'&&ref.document_id.length<=100&&/^[^\s\[\]@:\x00]+$/.test(ref.document_id)&&Number.isSafeInteger(ref.version)&&Number(ref.version)>0&&typeof ref.original_content_hash==='string'&&/^[0-9a-f]{64}$/.test(ref.original_content_hash);
}
export function originalFileReference(ref:OriginalFileReference):OriginalFileReference {return {document_id:ref.document_id,version:ref.version,original_content_hash:ref.original_content_hash};}
export function originalFileCitation(ref:OriginalFileReference){return `[file:${ref.document_id}@${ref.version}:${ref.original_content_hash}]`;}
export function fileLeadReference(file:{document_id:string;version:number;content_hash:string;source_reference?:OriginalFileReference}):OriginalFileReference|null {
 const ref=file.source_reference??{document_id:file.document_id,version:file.version,original_content_hash:file.content_hash};
 return isOriginalFileReference(ref)&&ref.document_id===file.document_id&&ref.version===file.version&&ref.original_content_hash===file.content_hash?originalFileReference(ref):null;
}
