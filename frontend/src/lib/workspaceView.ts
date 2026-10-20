export type WorkspaceView = 'connections'|'library'|'review'|'health'|'connection'|'evidence';
const KEY='hunter-gatherer.library.view';
const homeViews:WorkspaceView[]=['evidence','connections','library'];
/** Remember the last main workspace; health/review dialogs are not landing pages. */
export function readWorkspaceView():WorkspaceView {
  try {const saved=localStorage.getItem(KEY) as WorkspaceView;return homeViews.includes(saved)?saved:'evidence';}
  catch {return 'evidence';}
}
export function saveWorkspaceView(view:WorkspaceView) {
  if(homeViews.includes(view))try {localStorage.setItem(KEY,view);} catch { /* Navigation works without storage. */ }
}

export type LibraryTab='memories'|'files'|'collections'|'imports';
/** A record search must leave collection/import workflows, preserving a chosen file tab. */
export function recordSearchTab(tab:LibraryTab):'memories'|'files' {return tab==='files'?'files':'memories';}
