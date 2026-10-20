/** Original folded P / bookmark mark. Deliberately distinct from the graph navigation icon. */
export function ProHarnessMark({size = 30, className = ''}: {size?: number; className?: string}) {
  return <svg className={`proharness-mark ${className}`} width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true">
    <rect className="proharness-mark-frame" x="1" y="1" width="30" height="30" rx="9"/>
    <path className="proharness-mark-fold" d="M9 7.5h9.5a6.5 6.5 0 0 1 0 13H14V26l-5-3.5V7.5Z"/>
    <path className="proharness-mark-cut" d="M14 12.5h4.5a1.5 1.5 0 0 1 0 3H14v-3Z"/>
    <path className="proharness-mark-seam" d="M9 7.5l5 5V26"/>
  </svg>;
}
