import {describe, expect, it} from 'vitest';
import {createZoomMotion, focusCameraFrame, retargetZoomMotion, stepZoomMotion, type ZoomMotion, panGain, createPanMotion, retargetPanMotion, stepPanMotion} from './graphCamera';
import type {CameraView} from './graphGeometry';

const initial = {x: -180, y: 35, scale: 1}, anchor = {x: 420, y: 230};
function run(motion: ZoomMotion, milliseconds: number, hz = 60) {
  let result = stepZoomMotion(motion, 0);
  const count = Math.ceil(milliseconds * hz / 1000);
  for (let i = 0; i < count; i++) result = stepZoomMotion(result.motion, milliseconds / count / 1000);
  return result;
}

describe('graph camera motion', () => {
  it('bounds each wheel impulse and keeps sustained burst gain below 1.18', () => {
    let motion=createZoomMotion(initial,anchor,.01,100);
    for(let i=0;i<10;i++){
      const previous=motion.target;
      motion=retargetZoomMotion(motion,initial,100,anchor,.01,100,i*20,true);
      expect(motion.target-previous).toBeLessThanOrEqual(.20*1.18+.000001);
    }
    expect(stepZoomMotion(motion,.016).view.scale).toBeLessThan(1.1);
  });
  it('ramps rather than jumping, reaches a twofold target gently, and settles within 700 ms', () => {
    let motion = createZoomMotion(initial, anchor, .1, 4);
    motion = retargetZoomMotion(motion, initial, 2, anchor, .1, 4, 0);
    expect(stepZoomMotion(motion, .016).view.scale).toBeLessThan(1.05);
    const fast = run(motion, 300);
    expect(fast.view.scale).toBeGreaterThan(1.95);
    expect(fast.view.scale).toBeLessThanOrEqual(2);
    const settled = run(motion, 700);
    expect(settled.settled).toBe(true);
    expect(settled.view.scale).toBeCloseTo(2, 10);
  });

  it('preserves the world point under the cursor during every integrated frame', () => {
    let motion = retargetZoomMotion(createZoomMotion(initial, anchor, .1), initial, 1.4, anchor, .1, 4, 0);
    const world = motion.world;
    for (let i = 0; i < 35; i++) {
      const result = stepZoomMotion(motion, 1 / 60); motion = result.motion;
      expect((anchor.x - result.view.x) / result.view.scale).toBeCloseTo(world.x, 10);
      expect((anchor.y - result.view.y) / result.view.scale).toBeCloseTo(world.y, 10);
    }
  });

  it('accumulates unfinished wheel bursts and preserves velocity instead of restarting', () => {
    let motion = retargetZoomMotion(createZoomMotion(initial, anchor, .1), initial, 1.1, anchor, .1, 4, 0, true);
    let view: CameraView = initial;
    for (let i = 1; i <= 5; i++) {
      const result = stepZoomMotion(motion, .025); view = result.view;
      const next = retargetZoomMotion(result.motion, view, 1.1, anchor, .1, 4, i * 25, true);
      expect(next.velocity).toBe(result.motion.velocity);
      motion = next;
    }
    expect(Math.exp(motion.target)).toBeGreaterThan(1.7);
    expect(Math.exp(motion.target)).toBeLessThan(1.9);
    expect(run(motion, 300).view.scale).toBeGreaterThan(1.7);
    expect(run(motion, 700).settled).toBe(true);
  });

  it('responds immediately to reversal without continuing toward a stale queued target', () => {
    const input = retargetZoomMotion(createZoomMotion(initial, anchor, .1), initial, 1.4, anchor, .1, 4, 0, true);
    const moving = run(input, 80);
    expect(moving.motion.velocity).toBeGreaterThan(0);
    const reversed = retargetZoomMotion(moving.motion, moving.view, .9, anchor, .1, 4, 90, true);
    expect(reversed.velocity).toBe(0);
    expect(stepZoomMotion(reversed, .016).view.scale).toBeLessThan(moving.view.scale);
    expect(run(reversed, 700).view.scale).toBeCloseTo(moving.view.scale * .9, 10);
  });

  it('reanchors from the rendered view when the cursor changes, without a camera jump', () => {
    const first = retargetZoomMotion(createZoomMotion(initial, anchor, .1), initial, 1.3, anchor, .1, 4, 0);
    const moving = run(first, 70), nextAnchor = {x: 80, y: 540};
    const retargeted = retargetZoomMotion(moving.motion, moving.view, 1.1, nextAnchor, .1, 4, 80);
    expect(stepZoomMotion(retargeted, 0).view.x).toBeCloseTo(moving.view.x, 10);
    expect(stepZoomMotion(retargeted, 0).view.y).toBeCloseTo(moving.view.y, 10);
    const next = stepZoomMotion(retargeted, .02).view;
    expect((nextAnchor.x - next.x) / next.scale).toBeCloseTo(retargeted.world.x, 10);
  });

  it('never overshoots targets or bounds, even with accumulated velocity and abrupt bounds', () => {
    let motion = createZoomMotion(initial, anchor, .5, 1.5);
    motion = retargetZoomMotion(motion, initial, 10, anchor, .5, 1.5, 0, true);
    motion = retargetZoomMotion(motion, initial, 10, anchor, .5, 1.5, 20, true);
    let last = initial.scale;
    for (let i = 0; i < 60; i++) {
      const result = stepZoomMotion(motion, 1 / 60); motion = result.motion;
      expect(result.view.scale).toBeGreaterThanOrEqual(last);
      expect(result.view.scale).toBeLessThanOrEqual(1.5); last = result.view.scale;
    }
    const lower = retargetZoomMotion(motion, run(motion, 1).view, .001, anchor, .5, 1.5, 1100);
    const changed = retargetZoomMotion(lower, run(lower, 100).view, .001, anchor, .5, 1.5, 1120);
    expect(run(changed, 1000).view.scale).toBeGreaterThanOrEqual(.5);
    const awkward = {...motion, target: motion.value + .001, velocity: 100};
    expect(stepZoomMotion(awkward, .05).motion.value).toBeLessThanOrEqual(awkward.target);
  });

  it('has equivalent elapsed-time behavior at 30, 60 and 120 Hz', () => {
    const motion = retargetZoomMotion(createZoomMotion(initial, anchor, .1), initial, 1.4, anchor, .1, 4, 0);
    const reference = run(motion, 180, 60).view;
    for (const hz of [30, 120]) {
      const actual = run(motion, 180, hz).view;
      expect(actual.scale).toBeCloseTo(reference.scale, 10);
      expect(actual.x).toBeCloseTo(reference.x, 10);
    }
  });

  it('focus flights ramp up and down with logarithmic scale interpolation', () => {
    const target = {x: 100, y: 200, scale: 4};
    expect(focusCameraFrame(initial, target, 0)).toEqual(initial);
    expect(focusCameraFrame(initial, target, 1)).toEqual(target);
    expect(focusCameraFrame(initial, target, .5).scale).toBeCloseTo(2, 10);
    const startDistance = focusCameraFrame(initial, target, .05).x - initial.x;
    const middleDistance = focusCameraFrame(initial, target, .55).x - focusCameraFrame(initial, target, .5).x;
    const endDistance = target.x - focusCameraFrame(initial, target, .95).x;
    expect(startDistance).toBeLessThan(middleDistance / 10);
    expect(endDistance).toBeCloseTo(startDistance, 8);
  });
});

describe('trackpad pan motion', () => {
  it('reduces gain continuously as relative zoom increases and remains bounded', () => {
    expect(panGain(.1)).toBeCloseTo(.95);
    expect(panGain(1)).toBeCloseTo(.95);
    expect(panGain(4)).toBeLessThan(panGain(2));
    expect(panGain(10000)).toBeGreaterThanOrEqual(.48);
    expect(panGain(NaN)).toBeCloseTo(.95);
    expect(Math.abs(panGain(2.001)-panGain(2))).toBeLessThan(.001);
  });
  it('retains all accumulated distances and scale while smoothing frequent input', () => {
    let motion = createPanMotion(initial);
    for (let i = 0; i < 120; i++) {
      const previousVelocity = motion.velocity;
      motion = retargetPanMotion(motion, 5, -2);
      expect(motion.velocity).toEqual(previousVelocity);
      const result = stepPanMotion(motion, 1/120);
      motion = result.motion;
      expect(result.view.scale).toBe(initial.scale);
      // Sustained input has a bounded lag while preserving every pixel.
      expect(motion.target.x-result.view.x).toBeLessThan(70);
    }
    const result = stepPanMotion(motion, .7);
    expect(result.settled).toBe(true);
    expect(result.view).toEqual({x: initial.x+600, y: initial.y-240, scale: initial.scale});
  });
  it('accelerates from rest and decelerates to an exact stop without changing scale', () => {
    let motion = retargetPanMotion(createPanMotion(initial), 200, -85);
    const first = stepPanMotion(motion, 1/120), second = stepPanMotion(first.motion, 1/120);
    expect(first.motion.velocity.x).toBeGreaterThan(0);
    expect(second.view.x-first.view.x).toBeGreaterThan(first.view.x-initial.x);
    const peak = stepPanMotion(motion, 1/18), later = stepPanMotion(peak.motion, .2);
    expect(later.motion.velocity.x).toBeLessThan(peak.motion.velocity.x);
    let previous = initial;
    for (let i = 0; i < 120; i++) {
      const result = stepPanMotion(motion, 1/120); motion = result.motion;
      expect(result.view.x).toBeGreaterThanOrEqual(previous.x);
      expect(result.view.x).toBeLessThanOrEqual(motion.target.x);
      expect(result.view.y).toBeLessThanOrEqual(previous.y);
      expect(result.view.y).toBeGreaterThanOrEqual(motion.target.y);
      expect(result.view.scale).toBe(initial.scale); previous = result.view;
    }
    expect(stepPanMotion(motion, 0).settled).toBe(true);
    expect(motion.velocity).toEqual({x: 0, y: 0});
    expect(motion.value).toEqual({...initial, x: initial.x+200, y: initial.y-85});
  });
  it('reverses immediately on one axis while preserving the other unfinished input', () => {
    const moving = stepPanMotion(retargetPanMotion(createPanMotion(initial), 200, 85), .08).motion;
    const reversed = retargetPanMotion(moving, -20, 0);
    expect(reversed.target.x).toBe(moving.value.x-20);
    expect(reversed.velocity.x).toBe(0);
    expect(reversed.target.y).toBe(moving.target.y);
    expect(reversed.velocity.y).toBe(moving.velocity.y);
    const next = stepPanMotion(reversed, 1/60);
    expect(next.view.x).toBeLessThan(moving.value.x);
    expect(next.view.y).toBeGreaterThan(moving.value.y);
    const settled = stepPanMotion(next.motion, 1);
    expect(settled.view.x).toBe(reversed.target.x);
    expect(settled.view.y).toBe(moving.target.y);
  });
  it('clips unusually high carried velocity at the target', () => {
    const motion = retargetPanMotion(createPanMotion(initial), 1, -1);
    const result = stepPanMotion({...motion, velocity: {x: 10000, y: -10000}}, .05);
    expect(result.view).toEqual({...initial, ...motion.target});
    expect(result.motion.velocity).toEqual({x: 0, y: 0});
  });
  it('integrates equivalently at different refresh rates without overshoot', () => {
    const motion = retargetPanMotion(createPanMotion(initial), -200, 85);
    const single = stepPanMotion(motion, .1);
    for (const hz of [30, 60, 120]) {
      let repeated = motion;
      for (let i = 0; i < hz / 10; i++) repeated = stepPanMotion(repeated, 1/hz).motion;
      expect(repeated.value.x).toBeCloseTo(single.view.x, 10);
      expect(repeated.value.y).toBeCloseTo(single.view.y, 10);
      expect(repeated.velocity.x).toBeCloseTo(single.motion.velocity.x, 10);
      expect(repeated.velocity.y).toBeCloseTo(single.motion.velocity.y, 10);
    }
    expect(single.view.x).toBeGreaterThan(motion.target.x);
    expect(single.view.y).toBeLessThan(motion.target.y);
  });
});
