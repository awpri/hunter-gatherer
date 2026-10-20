import { useEffect, useState } from "react";
import { Telemetry, type RouterAlpha } from "../lib/api";

export function RouterView() {
  const [alpha, setAlpha] = useState<RouterAlpha | null>(null);
  const [decisions, setDecisions] = useState<any[]>([]);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    try {
      setErr(null);
      const [a, d] = await Promise.all([Telemetry.alpha(), Telemetry.routerDecisions(100)]);
      setAlpha(a);
      setDecisions(d.decisions);
    } catch (e: any) {
      setErr(e.message);
    }
  }

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  if (err) return <div className="p-4 text-red-400 text-sm">{err}</div>;
  if (!alpha) return <div className="p-4 text-zinc-500">loading…</div>;

  const inGuard = alpha.current_alpha >= alpha.guardrails.min && alpha.current_alpha <= alpha.guardrails.max;
  const range = alpha.guardrails.max - alpha.guardrails.min;
  const pct = ((alpha.current_alpha - alpha.guardrails.min) / range) * 100;

  return (
    <div className="p-6 overflow-y-auto h-full space-y-6">
      <h2 className="text-lg font-semibold">Router dashboard</h2>

      {/* α gauge */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <div className="flex items-baseline gap-4 mb-3">
          <div className="text-4xl font-mono">{alpha.current_alpha.toFixed(2)}</div>
          <div className="text-sm text-zinc-500">current α</div>
          {alpha.recommended_alpha !== null && (
            <div className="text-sm text-amber-400 ml-auto">
              recommended: {alpha.recommended_alpha.toFixed(2)} ({alpha.n_scored} scored)
            </div>
          )}
        </div>
        {/* Guardrail bar */}
        <div className="relative h-3 bg-zinc-800 rounded">
          <div className="absolute h-3 bg-cyan-600/30 rounded" style={{ width: `${pct}%` }} />
          <div className="absolute h-3 w-0.5 bg-cyan-400" style={{ left: `${pct}%` }} />
        </div>
        <div className="flex justify-between text-xs text-zinc-500 mt-1">
          <span>{alpha.guardrails.min}</span>
          <span>{inGuard ? "in guardrails" : "OUT OF RANGE"}</span>
          <span>{alpha.guardrails.max}</span>
        </div>
        <div className="text-xs text-zinc-500 mt-2">
          autotune: {alpha.guardrails.autotune_enabled ? "enabled" : "disabled"} · max step {alpha.guardrails.max_step} · min decisions {alpha.guardrails.min_decisions}
        </div>
        <div className="text-xs text-zinc-600 mt-1">{alpha.note}</div>
      </div>

      {/* Sweep curve */}
      {alpha.sweep_curve.length > 1 && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
          <div className="text-sm font-medium mb-2">α sweep (cost/quality curve)</div>
          <div className="text-xs text-zinc-500 mb-3">
            Each row: a simulated α, how many decisions go local vs cloud, and the bad-outcome rate on local.
          </div>
          <div className="space-y-1 font-mono text-xs">
            {alpha.sweep_curve.map((p, i) => (
              <div key={i} className="flex gap-3">
                <span className="text-zinc-400 w-12">α={p.alpha.toFixed(2)}</span>
                <span className="text-cyan-400 w-20">local {p.local}</span>
                <span className="text-pink-400 w-20">cloud {p.cloud}</span>
                <span className="text-zinc-500 w-20">bad {p.local_bad_rate.toFixed(2)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Decisions log */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <div className="text-sm font-medium mb-2">Recent routing decisions ({decisions.length})</div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-zinc-500">
              <tr>
                <th className="text-left py-1">tier</th>
                <th className="text-left py-1">outcome</th>
                <th className="text-right py-1">score</th>
                <th className="text-right py-1">α</th>
                <th className="text-right py-1">tokens</th>
                <th className="text-left py-1">query</th>
              </tr>
            </thead>
            <tbody>
              {decisions.slice(0, 50).map((d, i) => (
                <tr key={i} className="border-t border-zinc-800/50">
                  <td className="py-1"><TierBadge tier={d.tier} /></td>
                  <td className="py-1"><OutcomeBadge outcome={d.outcome} /></td>
                  <td className="py-1 text-right text-zinc-400">{d.score !== null ? d.score.toFixed(2) : "—"}</td>
                  <td className="py-1 text-right text-zinc-400">{d.alpha.toFixed(2)}</td>
                  <td className="py-1 text-right text-zinc-400">{d.in_tokens}</td>
                  <td className="py-1 text-zinc-500 truncate max-w-xs">{d.query}</td>
                </tr>
              ))}
              {decisions.length === 0 && (
                <tr><td colSpan={6} className="py-4 text-center text-zinc-600">no decisions logged yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function TierBadge({ tier }: { tier: string }) {
  const colors: Record<string, string> = {
    local: "text-cyan-400", cheap: "text-amber-400", cloud: "text-pink-400",
  };
  return <span className={colors[tier] || "text-zinc-400"}>{tier}</span>;
}

function OutcomeBadge({ outcome }: { outcome: string }) {
  const colors: Record<string, string> = {
    good: "text-green-400", revised: "text-amber-400", max_steps: "text-red-400", repeat: "text-orange-400",
  };
  return <span className={colors[outcome] || "text-zinc-400"}>{outcome}</span>;
}
