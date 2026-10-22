import type {HealthReport} from './libraryApi';
export type HealthSummary = {tone: 'green' | 'amber' | 'red'; scannedAt: number; scope: 'public' | 'private'; issues: number};
const critical = new Set(['evidence_source_missing', 'evidence_document_mismatch', 'evidence_source_deleted', 'evidence_section_missing', 'evidence_quote_mismatch', 'evidence_location_mismatch', 'invalid_evidence_refs', 'invalid_evidence_ref', 'chunk_section_document_mismatch', 'chunk_span_mismatch', 'chunk_vector_invalid', 'chunk_vector_dimension_mismatch', 'chunk_vector_fingerprint_mismatch', 'original_checksum_mismatch', 'missing_original', 'unsafe_original_path', 'document_has_no_chunks']);
/** Endpoint counts contain every issue, even when the displayed list is capped.
 * Generic diagnostic warnings are explanatory caveats, not integrity findings. */
export function summarizeHealth(report: HealthReport, includePrivate: boolean, now = Date.now()): HealthSummary {
  const counts = report.issue_counts || report.counts;
  const codes = counts ? Object.entries(counts).filter(([, count]) => count > 0).map(([code]) => code) : report.issues.map(issue => issue.code);
  const issues = counts ? Object.values(counts).reduce((sum, count) => sum + Math.max(0, count), 0) : report.issues.length;
  return {tone: codes.some(code => critical.has(code)) ? 'red' : issues ? 'amber' : 'green', scannedAt: now, scope: includePrivate ? 'private' : 'public', issues};
}
export function healthLabel(summary: HealthSummary | null): string {
  if (!summary) return 'Health: no previous scan';
  const status = summary.tone === 'green' ? 'no integrity issues reported' : summary.tone === 'red' ? 'integrity issues reported' : 'review items reported';
  return `Health: ${status} in last ${summary.scope === 'public' ? 'public' : 'private-inclusive'} scan, ${new Date(summary.scannedAt).toLocaleString()}. This is a saved scan result, not a live check.`;
}
export function readHealthSummary(storeId: string): HealthSummary | null {
  try {
    const value = JSON.parse(localStorage.getItem(`hunter_gatherer.health.summary.v1.${storeId}`) || 'null');
    return value && ['green', 'amber', 'red'].includes(value.tone) && value.scope === 'public' && Number.isFinite(value.scannedAt) && Number.isFinite(value.issues) && value.issues >= 0 ? value : null;
  } catch {return null;}
}
export function saveHealthSummary(storeId: string, summary: HealthSummary) {
  if (summary.scope !== 'public') return;
  try {localStorage.setItem(`hunter_gatherer.health.summary.v1.${storeId}`, JSON.stringify(summary));} catch { /* Indicator still works for this session. */ }
}
