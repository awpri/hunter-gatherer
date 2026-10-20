import {afterEach,describe,expect,it,vi} from 'vitest';
import {readWorkspaceView,saveWorkspaceView,recordSearchTab} from './workspaceView';
afterEach(()=>vi.unstubAllGlobals());
describe('Main workspace landing',()=>{
 it('starts new users on Ask and restores a previously chosen graph or library',()=>{const store=new Map<string,string>();vi.stubGlobal('localStorage',{getItem:(key:string)=>store.get(key)??null,setItem:(key:string,value:string)=>store.set(key,value)});expect(readWorkspaceView()).toBe('evidence');saveWorkspaceView('connections');expect(readWorkspaceView()).toBe('connections');saveWorkspaceView('health');expect(readWorkspaceView()).toBe('connections');saveWorkspaceView('library');expect(readWorkspaceView()).toBe('library');saveWorkspaceView('evidence');expect(readWorkspaceView()).toBe('evidence');});
 it('keeps Ask usable when storage is unavailable',()=>{vi.stubGlobal('localStorage',{getItem:()=>{throw Error('blocked');},setItem:()=>{throw Error('blocked');}});expect(readWorkspaceView()).toBe('evidence');expect(()=>saveWorkspaceView('library')).not.toThrow();});
});

describe('Record search navigation',()=>{
 it('leaves collection and import workflows for searchable memories',()=>{expect(recordSearchTab('collections')).toBe('memories');expect(recordSearchTab('imports')).toBe('memories');});
 it('preserves the chosen searchable record type',()=>{expect(recordSearchTab('files')).toBe('files');expect(recordSearchTab('memories')).toBe('memories');});
});
