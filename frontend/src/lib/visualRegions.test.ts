import {describe,it,expect} from 'vitest';
import {isVisualRegion,validateRegionImage,type VisualRegion} from './visualRegions';
const region:VisualRegion={record_id:'11111111-1111-4111-8111-111111111111',citation:'[region:11111111-1111-4111-8111-111111111111]',description:'Blue diagram',verification:'unverified',source_reference:{document_id:'source',version:1,original_content_hash:'a'.repeat(64),page:2,bbox:[0.1,0.2,0.6,0.8]}};
const data='iVBORw0KGgo=';
const {bbox:_,...source}=region.source_reference;
const image={...source,width:120,height:100,page_count:3,mime_type:'image/png',image_bytes:8,image:{mime_type:'image/png',data},source_reference:source,region_reference:region.source_reference,citation:region.citation,view:'region'};
describe('Exact visual region image boundary',()=>{
 it('accepts matching bounded image identity',()=>{expect(validateRegionImage(image,region,'region')).not.toBeNull();});
 it('rejects wrong page, version, bytes, region, geometry and view before display',()=>{
  for(const patch of [{page:1},{version:2},{image_bytes:9},{citation:'[region:other]'},{region_reference:{...region.source_reference,bbox:[0,0,1,1]}},{view:'page'},{width:100000}])expect(validateRegionImage({...image,...patch},region,'region')).toBeNull();
 });
 it('rejects nonfinite or degenerate discovered bounds and unrecognized citation identities',()=>{
  for(const bbox of [[NaN,0,1,1],[0,0,Infinity,1],[0,0,0,1],[-1,0,1,1],[0,0,1,2]])expect(isVisualRegion({...region,source_reference:{...source,bbox}})).toBe(false);
  expect(isVisualRegion({...region,citation:'[region:other]'})).toBe(false);
 });
});
