import type {GroupSize} from './graphLayout';

/** Backend dimensions bound the request; layout may reserve more room to keep
 * all cards safe when the requested region is too small for its contents. */
export function clampResizeSize(size: GroupSize, minimum: GroupSize = {width: 540, height: 300}): GroupSize {
  const width = Number.isFinite(size.width) ? size.width : 540;
  const height = Number.isFinite(size.height) ? size.height : 300;
  return {width: Math.max(Math.min(4000, minimum.width), Math.min(4000, width)), height: Math.max(Math.min(16000, minimum.height), Math.min(16000, height))};
}

/** Tokens fence optimistic previews, not server writes. An old completion must
 * never erase a newer drag on the same group, nor another group's preview. */
export class ResizePreviewLedger {
  private sequence = 0;
  private pending = new Map<string, {token: number; size: GroupSize}>();
  begin(id: string, size: GroupSize): number {
    const token = ++this.sequence;
    this.pending.set(id, {token, size});
    return token;
  }
  update(id: string, token: number, size: GroupSize): boolean {
    if (this.pending.get(id)?.token !== token) return false;
    this.pending.set(id, {token, size});
    return true;
  }
  settle(id: string, token: number): boolean {
    if (this.pending.get(id)?.token !== token) return false;
    this.pending.delete(id);
    return true;
  }
  snapshot(): Record<string, GroupSize> {
    return Object.fromEntries([...this.pending].map(([id, entry]) => [id, entry.size]));
  }
}
