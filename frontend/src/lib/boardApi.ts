import { StaleRequestError } from './libraryApi';
export type BoardLayout = 'board' | 'constellation' | 'columns';
export type BoardGrouping = 'subjects' | 'kind' | 'source';
export interface VisualMetadata {icon?: string | null; image?: string | null}
export interface BoardGroupSettings {
  icon?: string; image?: string;
  title?: string; description?: string; width?: number; height?: number;
  record_ids?: string[]; kind?: 'group' | 'topic'; sensitive?: boolean;
}
export interface BoardSettings {
  aliases?:Record<string,string>;version: number; revision: number; layout: BoardLayout; grouping: BoardGrouping; follow_agent: boolean;
  groups: Record<string, BoardGroupSettings>; records: Record<string, VisualMetadata>;
}
export type BoardVisualPatch = {icon?: string | null; image?: string | null};
export type BoardGroupPatch = Omit<BoardGroupSettings, 'icon' | 'image'> & BoardVisualPatch;
export type BoardPatch = Partial<Pick<BoardSettings, 'layout' | 'grouping' | 'follow_agent'>> & {
  aliases?:Record<string,string|null>;groups?: Record<string, BoardGroupPatch | null>; records?: Record<string, BoardVisualPatch | null>;
};
export type NewBoardGroup = BoardGroupSettings & {title: string};
export class BoardConflictError extends Error {
  constructor(public latest?: BoardSettings) {super('The board changed while you were editing. Reload the latest board before saving.'); this.name = 'BoardConflictError';}
}
/** Board writes are versioned; conflicts never overwrite a newer agent/user edit. */
export class BoardApi {
  private epoch = 0;
  private controllers = new Set<AbortController>();
  constructor(private fetcher: typeof fetch = (input, init) => globalThis.fetch(input, init)) {}
  invalidate() {this.epoch++; for (const controller of this.controllers) controller.abort(); this.controllers.clear();}
  private async request<T>(path: string, includeSensitive: boolean, body?: unknown, method = 'GET', signal?: AbortSignal): Promise<T> {
    const epoch = this.epoch, controller = new AbortController(); this.controllers.add(controller);
    const abort = () => controller.abort(); signal?.addEventListener('abort', abort, {once: true}); if (signal?.aborted) abort();
    try {
      if (controller.signal.aborted) throw new StaleRequestError();
      const response = await this.fetcher(`/api/library/${path}?include_sensitive=${includeSensitive}`, {method, signal: controller.signal, credentials: 'same-origin', headers: body === undefined ? undefined : {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
      if (epoch !== this.epoch || controller.signal.aborted) throw new StaleRequestError();
      const data = await response.json();
      if (epoch !== this.epoch || controller.signal.aborted) throw new StaleRequestError();
      if (response.status === 409) throw new BoardConflictError();
      if (!response.ok || data.error) {
        const validation = Array.isArray(data.detail) ? data.detail[0]?.msg : undefined;
        throw new Error(typeof data.detail === 'string' ? data.detail : typeof data.error === 'string' ? data.error : typeof validation === 'string' ? validation : `Board request failed (${response.status}).`);
      }
      return data as T;
    } catch (error) {if (epoch !== this.epoch || controller.signal.aborted) throw new StaleRequestError(); throw error;}
    finally {signal?.removeEventListener('abort', abort); this.controllers.delete(controller);}
  }
  load(includeSensitive = false, signal?: AbortSignal) {return this.request<BoardSettings>('board', includeSensitive, undefined, 'GET', signal);}
  private async write<T>(path: string, revision: number, body: unknown, includeSensitive: boolean, method: string) {
    try {return await this.request<T>(path, includeSensitive, {...body as object, revision, include_sensitive: includeSensitive}, method);}
    catch (error) {
      if (error instanceof BoardConflictError) {
        try {error.latest = await this.load(includeSensitive);} catch (refreshError) {if (refreshError instanceof Error && refreshError.name === 'AbortError') throw refreshError;}
      }
      throw error;
    }
  }
  patch(revision: number, patch: BoardPatch, includeSensitive = false) {return this.write<BoardSettings>('board', revision, patch, includeSensitive, 'PATCH');}
  createGroup(revision: number, group: NewBoardGroup, includeSensitive = false) {return this.write<BoardSettings & {group_id: string}>('groups', revision, group, includeSensitive, 'POST');}
}
export const boardApi = new BoardApi();
export const MAX_VISUAL_BYTES = 1024 * 1024;
export const MAX_VISUAL_INPUT_BYTES = 15 * 1024 * 1024;
export const VISUAL_ICONS = ['topics', 'source', 'workflow', 'preferences', 'decision', 'project', 'compass', 'education', 'archive', 'knowledge', 'work', 'personal', 'memory', 'lock', 'pin'];
export const isVisualEmoji = (icon?: string | null) => !!icon && icon.length <= 16 && /[\p{Extended_Pictographic}\p{Regional_Indicator}]/u.test(icon);
export const VISUAL_EMOJI = ['🌿', '🎨', '☕', '💡', '🚀', '🌍', '📚', '🎯', '🧠', '✨'];
export function validateVisualFile(file: {size: number; type: string}, bytes: Uint8Array, maxBytes = MAX_VISUAL_BYTES) {
  if (file.size === 0) throw new Error('Choose a nonempty image.');
  if (file.size > maxBytes) throw new Error(`Choose an image smaller than ${maxBytes / (1024 * 1024)} MiB.`);
  const png = bytes[0] === 137 && bytes[1] === 80 && bytes[2] === 78 && bytes[3] === 71 && bytes[4] === 13 && bytes[5] === 10 && bytes[6] === 26 && bytes[7] === 10;
  const jpeg = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
  const webp = String.fromCharCode(...bytes.slice(0, 4)) === 'RIFF' && String.fromCharCode(...bytes.slice(8, 12)) === 'WEBP';
  if (!((file.type === 'image/png' && png) || (file.type === 'image/jpeg' && jpeg) || (file.type === 'image/webp' && webp))) throw new Error('Choose a PNG, JPEG, or WebP image.');
}
export function visualImage(value: unknown): string | undefined {
  if (typeof value !== 'string' || value.length > Math.ceil(MAX_VISUAL_BYTES / 3) * 4 + 40) return;
  return /^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) ? value : undefined;
}

/** Raster decoding strips file metadata and resizes entirely on this device. */
export async function prepareVisualImage(file: File): Promise<string> {
  if (file.size > MAX_VISUAL_INPUT_BYTES) throw new Error('Choose an image smaller than 15 MiB.');
  const signature = new Uint8Array(await file.slice(0, 12).arrayBuffer());
  validateVisualFile(file, signature, MAX_VISUAL_INPUT_BYTES);
  let bitmap: ImageBitmap;
  try {bitmap = await createImageBitmap(file);} catch {throw new Error('This image could not be opened. Choose another PNG, JPEG, or WebP image.');}
  try {
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, 1200 / Math.max(bitmap.width, bitmap.height));
    canvas.width = Math.max(1, Math.round(bitmap.width * scale)); canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const context = canvas.getContext('2d'); if (!context) throw new Error('Image processing is unavailable in this browser.');
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    for (const quality of [.82, .65, .45]) {
      const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/webp', quality));
      if (!blob) throw new Error('This image could not be processed.');
      if (blob.size <= MAX_VISUAL_BYTES) {
        return await new Promise<string>((resolve, reject) => {const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(new Error('This image could not be read.')); reader.readAsDataURL(blob);});
      }
    }
    throw new Error('This image is too detailed to fit. Choose a smaller image.');
  } finally {bitmap.close();}
}

export const isDerivedBoardGroup = (id?: string) => !!id && (id.startsWith('kind:') || id.startsWith('source:'));
/** Derived arrangements keep their memberships tied to the recorded kind/source. */
export function groupEditorChanges(id: string | undefined, value: BoardGroupSettings): BoardGroupSettings {
  if (!isDerivedBoardGroup(id)) return value;
  const {record_ids: _records, kind: _kind, sensitive: _sensitive, ...visual} = value;
  return visual;
}
