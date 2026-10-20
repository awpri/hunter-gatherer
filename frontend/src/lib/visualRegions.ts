import {isOriginalPageReference,validateOriginalPage,type OriginalPageImage,type OriginalPageReference} from './pageImages';
import type {LibraryDocument} from './libraryApi';

export interface VisualRegion {
 record_id:string;
 citation:string;
 description:string;
 description_omitted?:boolean;
 verification:'unverified';
 source_reference:OriginalPageReference&{bbox:[number,number,number,number]};
}
export interface RegionImage extends OriginalPageImage {
 citation:string;
 region_reference:VisualRegion['source_reference'];
 view:'region'|'page';
}
export function isVisualRegion(value:unknown):value is VisualRegion {
 if(!value||typeof value!=='object')return false;
 const region=value as VisualRegion;
 const box=region.source_reference?.bbox;
 return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(region.record_id)
  &&region.citation===`[region:${region.record_id}]`&&typeof region.description==='string'&&region.verification==='unverified'
  &&isOriginalPageReference(region.source_reference)&&Array.isArray(box)&&box.length===4
  &&box.every(value=>typeof value==='number'&&Number.isFinite(value))
  &&0<=box[0]&&box[0]<box[2]&&box[2]<=1&&0<=box[1]&&box[1]<box[3]&&box[3]<=1;
}
/** Exact identity and bounded PNG checks run before pixels enter the DOM. */
export function validateRegionImage(value:unknown,region:VisualRegion,view:'region'|'page'):RegionImage|null {
 if(!isVisualRegion(region)||!value||typeof value!=='object')return null;
 const result=value as RegionImage,ref=region.source_reference,actual=result.region_reference;
 const page=validateOriginalPage(value,{document_id:ref.document_id,version:ref.version,original_content_hash:ref.original_content_hash} as LibraryDocument,ref.page);
 if(!page||result.citation!==region.citation||result.view!==view||!isOriginalPageReference(actual)
  ||actual.document_id!==ref.document_id||actual.version!==ref.version||actual.original_content_hash!==ref.original_content_hash||actual.page!==ref.page
  ||!Array.isArray(actual.bbox)||actual.bbox.length!==4||actual.bbox.some((number,index)=>number!==ref.bbox[index]))return null;
 return result;
}
