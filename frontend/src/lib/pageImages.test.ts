import {describe,expect,it} from 'vitest';
import {documentOriginalHash,originalPageStart,validateOriginalPage} from './pageImages';
import {imagePacket} from './pageImageFixtures';
import {packet} from '../views/tableCellFixtures';
const doc={document_id:'doc-1',version:3,title:'Fixture',created_at:0,original_content_hash:'a'.repeat(64)};
describe('Original page identity and transport bounds',()=>{
 it('uses the exact anchor page, then explicit selection page, then explicit page-one fallback',()=>{
  expect(originalPageStart(doc,'chunk-1',packet().chunks,99)).toBe(7);
  expect(originalPageStart(doc,'missing',packet().chunks,99)).toBe(99);
  expect(originalPageStart({...doc,version:4},'chunk-1',packet().chunks,2)).toBe(2);
  expect(originalPageStart(doc,undefined,[],0)).toBe(1);
 });
 it('accepts bounded, exact source PNGs',()=>{expect(validateOriginalPage(imagePacket(),doc,2)?.page).toBe(2);});
 it.each([
  {document_id:'other'},{version:4},{page:1},{page_count:1},{original_content_hash:'wrong'},
  {mime_type:'image/svg+xml'},{image_bytes:2*1024*1024+1},{image_bytes:67},{width:3073},{width:3000,height:3000},
  {width:0},{height:NaN},{image:{mime_type:'image/png',data:'javascript:bad'}},
  {image:{mime_type:'image/jpeg',data:imagePacket().image.data}},
  {source_reference:{...imagePacket().source_reference,page:3}},
 ])('rejects inconsistent metadata or unsupported pixels (%j)',change=>{expect(validateOriginalPage({...imagePacket(),...change},doc,2)).toBeNull();});
});


it('checks the real document content_hash and rejects conflicting catalog hash aliases',()=>{
 const actual={document_id:'doc-1',version:3,title:'Real metadata',created_at:0,content_hash:'a'.repeat(64)};
 expect(documentOriginalHash(actual)).toBe(actual.content_hash);expect(validateOriginalPage(imagePacket(),actual,2)).not.toBeNull();
 expect(validateOriginalPage(imagePacket(),{...actual,content_hash:'b'.repeat(64)},2)).toBeNull();
 expect(validateOriginalPage(imagePacket(),{...actual,original_content_hash:'b'.repeat(64)},2)).toBeNull();
});
