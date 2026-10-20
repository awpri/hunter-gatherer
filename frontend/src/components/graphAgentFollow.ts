import type {CameraView, CanvasSize, Point} from './graphGeometry';

type ActivityPoint = Point & {recordScale: number};

/** Follow reveals activity with surrounding context, without pulling an overview
 * into card detail. An already visible batch leaves the camera exactly alone. */
export function agentFollowCamera(view: CameraView, size: CanvasSize, points: readonly ActivityPoint[], overviewScale: number): CameraView {
  if (!points.length) return view;
  const left = Math.min(...points.map(point => point.x - 148 * point.recordScale));
  const right = Math.max(...points.map(point => point.x + 148 * point.recordScale));
  const top = Math.min(...points.map(point => point.y - 112 * point.recordScale));
  const bottom = Math.max(...points.map(point => point.y + 112 * point.recordScale));
  const marginX = Math.min(96, size.width * .1), marginY = Math.min(80, size.height * .1);
  const screenLeft = marginX, screenRight = size.width - marginX;
  const screenTop = marginY + 22, screenBottom = size.height - marginY - 42;
  if (left * view.scale + view.x >= screenLeft && right * view.scale + view.x <= screenRight
    && top * view.scale + view.y >= screenTop && bottom * view.scale + view.y <= screenBottom) return view;
  const scale = Math.max(.000001, Math.min(view.scale, overviewScale * 1.5,
    (screenRight - screenLeft) / Math.max(1, right - left),
    (screenBottom - screenTop) / Math.max(1, bottom - top)));
  // Preserve the current viewport's center through any necessary zoom-out, then
  // pan only as far as needed to bring the activity inside the contextual margin.
  const x = size.width / 2 - (size.width / 2 - view.x) * scale / view.scale;
  const y = size.height / 2 - (size.height / 2 - view.y) * scale / view.scale;
  return {scale,
    x: Math.max(screenLeft - left * scale, Math.min(screenRight - right * scale, x)),
    y: Math.max(screenTop - top * scale, Math.min(screenBottom - bottom * scale, y))};
}
