/** A quiet loading state, separate from actual agent activity. */
export function LibraryLoading({graph, cards}: {graph: boolean; cards: boolean}) {
  return <div className={`library-loading ${graph ? 'graph-loading' : cards ? 'cards-loading' : 'rows-loading'}`} role="status" aria-label="Loading records">
    {graph ? <div className="loading-orbit" aria-hidden="true"><span/><i/><b/></div> : Array.from({length: 6}, (_, i) => <div className="loading-record" key={i} aria-hidden="true"><i/><div><span/><span/><span/></div></div>)}
    <span className="sr-only">Loading records</span>
  </div>;
}
