import type {OriginalPageImage} from './pageImages';
const data='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV1sAAAAASUVORK5CYII=';
/** Synthetic one-pixel PNG: no real source bytes appear in tests. */
export function imagePacket(page=2):OriginalPageImage{return {document_id:'doc-1',version:3,original_content_hash:'a'.repeat(64),page,page_count:3,width:1,height:1,mime_type:'image/png',image_bytes:68,image:{mime_type:'image/png',data},source_reference:{document_id:'doc-1',version:3,original_content_hash:'a'.repeat(64),page}};}
