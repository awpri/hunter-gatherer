import type {CameraView, Point} from './graphGeometry';

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));
export const CAMERA_FOCUS_MS = 420;
export const ZOOM_FREQUENCY = 18;
export type ZoomMotion = {
  value: number; target: number; velocity: number;
  anchor: Point; world: Point; min: number; max: number;
  lastInput: number; direction: number; burst: number;
};

export function createZoomMotion(view: CameraView, point: Point, minScale: number, maxScale = 4): ZoomMotion {
  const min = Math.log(Math.max(.000001, minScale));
  const max = Math.log(Math.max(Math.exp(min), maxScale));
  const value = Math.log(Math.max(.000001, view.scale));
  return {value, target: clamp(value, min, max), velocity: 0, anchor: point,
    world: {x: (point.x - view.x) / view.scale, y: (point.y - view.y) / view.scale},
    min, max, lastInput: -Infinity, direction: 0, burst: 0};
}

/** Input accumulates against the unfinished target. Changing the pointer resets
 * the world anchor from the displayed camera, never from its future target. */
export function retargetZoomMotion(motion: ZoomMotion, view: CameraView, factor: number, point: Point,
  minScale: number, maxScale: number, now: number, accelerate = false): ZoomMotion {
  if (!Number.isFinite(factor) || factor <= 0) return motion;
  const impulse = clamp(Math.log(factor), accelerate ? -.20 : -1.2, accelerate ? .20 : 1.2), direction = Math.sign(impulse);
  const continuing = direction !== 0 && direction === motion.direction && now - motion.lastInput < 110;
  const burst = continuing ? Math.min(5, motion.burst + 1) : 0;
  const gain = accelerate ? 1 + burst * .036 : 1;
  const min = Math.log(Math.max(.000001, minScale)), max = Math.log(Math.max(Math.exp(min), maxScale));
  // A reversed gesture sheds unfinished momentum immediately instead of asking
  // the user to unwind a queued target from the preceding wheel burst.
  const reversed = direction !== 0 && motion.direction !== 0 && direction !== motion.direction;
  const value = Math.log(Math.max(.000001, view.scale));
  const target = clamp((reversed ? value : motion.target) + impulse * gain, min, max);
  const velocity = reversed || motion.velocity * (target - value) < 0 ? 0 : motion.velocity;
  return {value, target, velocity, min, max, anchor: point,
    world: {x: (point.x - view.x) / view.scale, y: (point.y - view.y) / view.scale},
    lastInput: now, direction: direction || motion.direction, burst};
}

/** Exact critically damped solution: timing is independent of display refresh.
 * Monotonic clipping also prevents a retargeted spring overshooting its goal. */
export function stepZoomMotion(motion: ZoomMotion, seconds: number): {motion: ZoomMotion; view: CameraView; settled: boolean} {
  const dt = Math.max(0, seconds), offset = motion.value - motion.target;
  const coefficient = motion.velocity + ZOOM_FREQUENCY * offset, decay = Math.exp(-ZOOM_FREQUENCY * dt);
  const candidate = motion.target + (offset + coefficient * dt) * decay;
  const low = Math.min(motion.value, motion.target), high = Math.max(motion.value, motion.target);
  let value = clamp(candidate, Math.max(motion.min, low), Math.min(motion.max, high));
  let velocity = (motion.velocity - ZOOM_FREQUENCY * coefficient * dt) * decay;
  if (value !== candidate) velocity = 0;
  const settled = Math.abs(value - motion.target) < .0002 && Math.abs(velocity) < .005;
  if (settled) {value = motion.target; velocity = 0;}
  const scale = Math.exp(value);
  return {motion: {...motion, value, velocity},
    view: {x: motion.anchor.x - motion.world.x * scale, y: motion.anchor.y - motion.world.y * scale, scale}, settled};
}

/** Quintic S-curve starts and finishes with zero speed and acceleration. */
export function focusCameraFrame(start: CameraView, target: CameraView, progress: number): CameraView {
  const t = clamp(progress, 0, 1), ease = t * t * t * (t * (t * 6 - 15) + 10);
  return {x: start.x + (target.x - start.x) * ease, y: start.y + (target.y - start.y) * ease,
    scale: Math.exp(Math.log(start.scale) + (Math.log(target.scale) - Math.log(start.scale)) * ease)};
}

/** Screen-space gain decreases smoothly as the board is magnified relative to fit. */
export function panGain(relativeZoom: number): number {
  const zoom = Number.isFinite(relativeZoom) ? Math.max(1, relativeZoom) : 1;
  return .48 + .47 / zoom;
}

export const PAN_FREQUENCY = 18;
export type PanMotion = {value: CameraView; target: Point; velocity: Point; direction: Point};
export function createPanMotion(view: CameraView): PanMotion {
  return {value: {...view}, target: {x: view.x, y: view.y}, velocity: {x: 0, y: 0}, direction: {x: 0, y: 0}};
}
/** Accumulate each axis independently; reversal sheds its unfinished target and velocity. */
export function retargetPanMotion(motion: PanMotion, dx: number, dy: number): PanMotion {
  const target = {...motion.target}, velocity = {...motion.velocity}, direction = {...motion.direction};
  for (const axis of ['x', 'y'] as const) {
    const delta = axis === 'x' ? dx : dy;
    if (!Number.isFinite(delta) || delta === 0) continue;
    const sign = Math.sign(delta), reversed = direction[axis] !== 0 && direction[axis] !== sign;
    target[axis] = (reversed ? motion.value[axis] : target[axis]) + delta;
    if (reversed || velocity[axis] * (target[axis] - motion.value[axis]) < 0) velocity[axis] = 0;
    direction[axis] = sign;
  }
  return {...motion, target, velocity, direction};
}
/** Exact critically damped ramp matches zoom's acceleration and gentle stop. */
export function stepPanMotion(motion: PanMotion, seconds: number): {motion: PanMotion; view: CameraView; settled: boolean} {
  const dt = Math.max(0, seconds), decay = Math.exp(-PAN_FREQUENCY * dt);
  const view = {...motion.value}, velocity = {...motion.velocity};
  let settled = true;
  for (const axis of ['x', 'y'] as const) {
    const offset = motion.value[axis] - motion.target[axis];
    const coefficient = velocity[axis] + PAN_FREQUENCY * offset;
    const candidate = motion.target[axis] + (offset + coefficient * dt) * decay;
    view[axis] = clamp(candidate, Math.min(motion.value[axis], motion.target[axis]), Math.max(motion.value[axis], motion.target[axis]));
    velocity[axis] = view[axis] === candidate ? (velocity[axis] - PAN_FREQUENCY * coefficient * dt) * decay : 0;
    const axisSettled = Math.abs(view[axis] - motion.target[axis]) < .05 && Math.abs(velocity[axis]) < .5;
    if (axisSettled) {view[axis] = motion.target[axis]; velocity[axis] = 0;}
    settled = settled && axisSettled;
  }
  return {motion: {...motion, value: view, velocity}, view, settled};
}
