import {useEffect, useState} from 'react';

/** Track accessibility and pause controls for activity effects without an
 * ambient clock that repeatedly recomputes the entire graph. */
export function useGraphMotion(paused: boolean, list: boolean) {
  const [reduced, setReduced] = useState(false);
  const [hidden, setHidden] = useState(false);
  useEffect(() => {
    const media = matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => setReduced(media.matches || document.documentElement.dataset.motion === 'reduced');
    const visibility = () => setHidden(document.hidden);
    update(); visibility();
    media.addEventListener('change', update);
    document.addEventListener('visibilitychange', visibility);
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, {attributes: true, attributeFilter: ['data-motion']});
    return () => {media.removeEventListener('change', update); document.removeEventListener('visibilitychange', visibility); observer.disconnect();};
  }, []);
  const stopped = paused || reduced || hidden || list;
  return {reduced, stopped};
}
