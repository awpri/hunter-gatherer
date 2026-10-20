import {describe, expect, it} from 'vitest';
import {wheelGesture, type WheelInput} from './graphInput';
const input: WheelInput = {deltaX: 3, deltaY: 10, deltaMode: 0, ctrlKey: false, metaKey: false, shiftKey: false};
describe('separate scroll zoom and Shift pan', () => {
  it('zooms normal scrolling and switches only while Shift is held', () => {
    expect(wheelGesture(input)).toEqual({kind:'zoom', factor:Math.exp(-.03)});
    expect(wheelGesture({...input, shiftKey:true})).toEqual({kind:'pan', dx:-3, dy:-10});
    expect(wheelGesture({...input, shiftKey:false})).toEqual({kind:'zoom', factor:Math.exp(-.03)});
  });
  it('keeps native pinch zoom independent of Shift and bounds its impulse', () => {
    for (const modifier of [{ctrlKey:true},{metaKey:true}]) for (const shiftKey of [false,true]) {
      expect(wheelGesture({...input,...modifier,shiftKey})).toEqual({kind:'zoom', factor:Math.exp(-.03)});
    }
    expect(wheelGesture({...input,ctrlKey:true,deltaY:-10000})).toEqual({kind:'zoom',factor:Math.exp(.3)});
  });
  it('preserves both pan axes without rotating vertical input', () => {
    expect(wheelGesture({...input,shiftKey:true,deltaX:0})).toEqual({kind:'pan',dx:-0,dy:-10});
    expect(wheelGesture({...input,shiftKey:true,deltaY:0})).toEqual({kind:'pan',dx:-3,dy:-0});
    expect(wheelGesture({...input,deltaY:0})).toEqual({kind:'zoom',factor:Math.exp(-.009)});
  });
  it('normalizes line and page deltas for panning and zooming', () => {
    expect(wheelGesture({...input,shiftKey:true,deltaMode:1})).toEqual({kind:'pan',dx:-48,dy:-160});
    expect(wheelGesture({...input,shiftKey:true,deltaMode:2},600)).toEqual({kind:'pan',dx:-1800,dy:-6000});
    expect(wheelGesture({...input,deltaMode:1,deltaY:1})).toEqual({kind:'zoom',factor:Math.exp(-.048)});
  });
  it('ignores non-finite and empty input', () => {
    expect(wheelGesture({...input,deltaX:NaN,deltaY:Infinity})).toEqual({kind:'zoom',factor:1});
    expect(wheelGesture({...input,deltaX:0,deltaY:0})).toEqual({kind:'zoom',factor:1});
  });
});
