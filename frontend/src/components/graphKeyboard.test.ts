import {describe,it,expect} from 'vitest';
import {nextScopedRecord} from './graphKeyboard';
describe('Graph scoped keyboard navigation',()=>{
 it('wraps only through supplied group records',()=>{expect(nextScopedRecord(['a','b'],'b','ArrowRight')).toBe('a');expect(nextScopedRecord(['a','b'],'a','ArrowUp')).toBe('b');expect(nextScopedRecord(['a','b'],'a','ArrowDown')).toBe('b');});
 it('supports ends and leaves Tab and unknown input untouched',()=>{expect(nextScopedRecord(['a','b'],'a','End')).toBe('b');expect(nextScopedRecord(['a','b'],'b','Home')).toBe('a');expect(nextScopedRecord(['a'],'a','Tab')).toBeNull();expect(nextScopedRecord([],'a','ArrowDown')).toBeNull();});
});
