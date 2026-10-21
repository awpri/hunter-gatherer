import {afterEach,describe,expect,it,vi} from 'vitest';
import {readWorkspaceView,saveWorkspaceView,recordSearchTab} from './workspaceView';
afterEach(()=>vi.unstubAllGlobals());
describe('Main workspace landing',()=>{
 it('starts new users on Graph and restores a previously chosen library',()=>{const store=new Map<string,string>();vi.stubGlobal('localStorage',{getItem:(key:string)=>store.get(key)??null,setItem:(key:string,value:string)=>store.set(key,value)});expect(readWorkspaceView()).toBe('connections');saveWorkspaceView('library');expect(readWorkspaceView()).toBe('library');saveWorkspaceView('health');expect(readWorkspaceView()).toBe('library');saveWorkspaceView('connections');expect(readWorkspaceView()).toBe('connections');});
 it('migrates the former Ask page to Graph',()=>{vi.stubGlobal('localStorage',{getItem:()=> 'evidence'});expect(readWorkspaceView()).toBe('connections');});
 it('keeps Graph usable when storage is unavailable',()=>{vi.stubGlobal('localStorage',{getItem:()=>{throw Error('blocked');},setItem:()=>{throw Error('blocked');}});expect(readWorkspaceView()).toBe('connections');expect(()=>saveWorkspaceView('library')).not.toThrow();});
});

describe('Record search navigation',()=>{
 it('leaves collection and import workflows for searchable memories',()=>{expect(recordSearchTab('collections')).toBe('memories');expect(recordSearchTab('imports')).toBe('memories');});
 it('preserves the chosen searchable record type',()=>{expect(recordSearchTab('files')).toBe('files');expect(recordSearchTab('memories')).toBe('memories');});
});
