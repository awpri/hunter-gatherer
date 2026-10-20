import {describe,it,expect} from 'vitest';
import {matchSearchCommands,nextSearchIndex} from './unifiedSearchModel';
describe('unified search',()=>{
 it('matches all query terms across labels and keywords',()=>{expect(matchSearchCommands([{label:'Import file',keywords:['document','upload']},{label:'Files'}],'IMPORT document').map(x=>x.label)).toEqual(['Import file']);});
 it('keeps matching groups visible alongside commands',()=>{const commands=Array.from({length:20},(_,i)=>({label:`Project ${i}`,section:'Navigate'}));expect(matchSearchCommands([...commands,{label:'Project planning',section:'Groups'}],'project')[0].label).toBe('Project planning');});
 it('wraps keyboard navigation and tolerates no results',()=>{expect(nextSearchIndex(0,-1,4)).toBe(3);expect(nextSearchIndex(3,1,4)).toBe(0);expect(nextSearchIndex(0,1,0)).toBe(0);});
});

it('shows one contextual Ask action and keeps the command available in commands mode',()=>{const commands=[{id:'gather-evidence',label:'Gather evidence',description:'For projects'},{id:'files',label:'Project files'}];expect(matchSearchCommands(commands,'project',true).map(command=>command.id)).toEqual(['files']);expect(matchSearchCommands(commands,'',true).map(command=>command.id)).not.toContain('gather-evidence');expect(matchSearchCommands(commands,'project',false).map(command=>command.id)).toContain('gather-evidence');});
