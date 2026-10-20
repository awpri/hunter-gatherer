export interface EvidenceRef {
  document_id: string; chunk_id?: string; quote?: string; citation?: string;
  location?: { page?: number | null; section?: string; start?: number; end?: number };
}
export interface LibraryMemory {
  icon?: string; image?: string; presentation?: {icon?:string;image?:string};
  id: string; group_id?: string; text: string; type?: string; kind?: string; importance: number; tags: string[];
  created_at: number | string; source?: string; citation?: string; verification?: string;
  metadata: { category?: string; kind?: string; verification?: string; sensitive?: boolean; core?: boolean; retention?: string; evidence_refs?: EvidenceRef[]; [key: string]: unknown };
}
export interface LibraryDocument {
  icon?: string; image?: string; presentation?: {icon?:string;image?:string};
  document_id: string; group_id?: string; source_path?: string; file_name?: string; title: string; author?: string; document_date?: string; category?: string;
  sensitive?: boolean; version: number; is_latest?: boolean; media_type?: string; created_at: number | string;
  original_content_hash?: string; content_hash?: string;
  extraction_status?: string; warnings?: string[]; source_uri?: string;
  extraction?: {backend?: string; parser_version?: string; provenance?: {config?: {ocr_engine?: string;ocr_orientation?: string} | null} | null};
  passages?: {section_id: string; page?: number; section?: string; start: number; end: number; text: string}[];
  citation_map?: DocumentCitationMap;
  next_start?: number | null; total_chars?: number; page?: number | null;
}
export interface DocumentCitationMap {
  coordinate_system: 'section_codepoints_half_open';
  coverage: 'full' | 'partial' | 'none'; complete: boolean;
  spans: {passage_index: number; start: number; end: number; chunk_id: string; citation: string}[];
  omitted_reasons?: string[];
}
export interface LibraryGroup {
  title?:string; description?:string; image?:string; width?:number; height?:number; kind?:'group'|'topic'; manual?:boolean; sensitive?:boolean;
  id: string; label: string; count: number; memory_count: number; document_count: number;
  record_ids: string[]; color_index: number; section_id?: string; section_label?: string; icon?: string;
}
export interface LibrarySection {id:string;label:string;icon:string;count:number;memory_count?:number;document_count?:number;group_count?:number;group_ids?:string[]}
export interface LibraryGroups {groups: LibraryGroup[]; sections?:LibrarySection[]; total: number; method: string}
export interface ImportCandidate {
  candidate_id: string; document_id: string; group_id?: string; text: string; kind: string; importance: number | null;
  source_title: string; status: string; approvable: boolean; warnings: string[]; evidence_refs: EvidenceRef[];
}
export interface Overview {
  store: { id: string; database: string }; counts: { memories: number; documents: number; pending: number };
  categories: {name: string; count: number}[]; kinds: {name: string; count: number}[];
}
export interface Page<T> { total: number; next_offset: number | null; memories?: T[]; documents?: T[]; candidates?: T[] }
export interface HealthReport {
  scope: {documents: number; memories: number; chunks: number; evidence_refs: number};
  counts?: Record<string, number>; issue_counts?: Record<string, number>; issues: {code: string; document_id?: string; memory_id?: string; chunk_id?: string}[];
  warnings: string[]; verification?: {requested: boolean; checked_originals: number};
}
export type Query = Record<string, string | number | boolean | undefined | null>;
export class StaleRequestError extends Error { constructor() { super('Request superseded'); this.name = 'AbortError'; } }
/** Every privacy change invalidates requests, including fetch implementations which ignore abort. */
export class LibraryApi {
  private epoch = 0;
  private controllers = new Set<AbortController>();
  constructor(private fetcher: typeof fetch = (input, init) => globalThis.fetch(input, init)) {}
  private listeners = new Set<()=>void>();
  get contextVersion(){return this.epoch;}
  subscribeInvalidation(listener:()=>void){this.listeners.add(listener);return ()=>{this.listeners.delete(listener);};}
  invalidate() { this.epoch++; for(const listener of this.listeners)listener(); for (const controller of this.controllers) controller.abort(); this.controllers.clear(); }
  async request<T>(path: string, query: Query = {}, body?: unknown, method?: string, signal?: AbortSignal): Promise<T> {
    const epoch = this.epoch;
    const controller = new AbortController(); this.controllers.add(controller);
    const cancel = () => controller.abort();
    signal?.addEventListener('abort', cancel, {once: true});
    if (signal?.aborted) cancel();
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
    const multipart = typeof FormData !== 'undefined' && body instanceof FormData;
    try {
      if (controller.signal.aborted) throw new StaleRequestError();
      const response = await this.fetcher(`/api/library/${path}${params.size ? `?${params}` : ''}`, {
        method: method || (body === undefined ? 'GET' : 'POST'), signal: controller.signal,
        credentials: 'same-origin', headers: body !== undefined && !multipart ? {'Content-Type': 'application/json'} : undefined,
        body: body === undefined ? undefined : multipart ? body as FormData : JSON.stringify(body),
      });
      if (epoch !== this.epoch || controller.signal.aborted) throw new StaleRequestError();
      const data = await response.json();
      if (epoch !== this.epoch || controller.signal.aborted) throw new StaleRequestError();
      if (!response.ok || data.error) throw new Error(typeof data.error === 'string' ? data.error : typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) && typeof data.detail[0]?.msg === 'string' ? data.detail[0].msg : `Library request failed (${response.status}).`);
      return data as T;
    } catch (error) { if (epoch !== this.epoch || controller.signal.aborted) throw new StaleRequestError(); throw error; }
    finally { signal?.removeEventListener('abort', cancel); this.controllers.delete(controller); }
  }
}
export const libraryApi = new LibraryApi();
export const kindOf = (memory: LibraryMemory) => memory.kind || memory.metadata?.kind || memory.type || 'memory';
export const refsOf = (memory: LibraryMemory) => memory.metadata?.evidence_refs || [];
export function dateLabel(value?: number | string) {
  if (!value) return 'Date unavailable';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'});
}
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Could not reach the library.';
export const isAbort = (error: unknown) => error instanceof Error && error.name === 'AbortError';

/** Explicit caller-authored comparisons never establish verified compatibility. */
export interface RouteApplicability {
 status:'match'|'mismatch'|'unknown'; reason?:string; missing_fields:string[]; scope?:string;
 review_status?:string; assertion_status?:string;
 checks:{field:string;status:'match'|'mismatch'|'unknown';reason:string;requested?:string|number|boolean|null;requested_as_of?:string;actual?:string|number|boolean|null}[];
}
/** Retrieval routes are discovery hints, not verified source claims. */
export interface RetrievalPathStep {
  record_id:string; predicate:string; direction?:string; assertion_status?:string; review_status?:string;
  source_citations:string[]; support?:string; valid_from?:string; valid_to?:string;
  relation_validity?:{valid_from?:string;valid_to?:string}; applicability?:RouteApplicability;
}
export interface RetrievalPath {
  reason:string; seed:{record_id:string;label:string;match:string;source_citations?:string[]}; steps:RetrievalPathStep[];
  target:{record_id:string;label:string;citation:string;document_id?:string;chunk_id?:string;identity_citations?:string[];matched_fields?:string[]};
  source_citations?:string[]; original_citations?:string[]; support?:string; applicability?:RouteApplicability;
}
export interface RetrievalPaths {
  status?:string; paths:RetrievalPath[]; scope?:string; truncated?:boolean; omitted_paths?:number;
  limits?:{truncated?:boolean;[key:string]:unknown};
}
