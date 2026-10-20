import { useEffect, useState } from "react";
import { Telemetry, type Cost } from "../lib/api";

export function CostView() {
  const [cost, setCost] = useState<Cost | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    try {
      setErr(null);
      setCost(await Telemetry.cost());
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
  if (!cost) return <div className="p-4 text-zinc-500">loading…</div>;

  const models = Object.entries(cost.per_model).sort((a, b) => (b[1].in_tokens + b[1].out_tokens) - (a[1].in_tokens + a[1].out_tokens));
  const totalInOut = models.reduce((acc, [, u]) => ({ in: acc.in + u.in_tokens, out: acc.out + u.out_tokens }), { in: 0, out: 0 });
  const maxTokens = Math.max(1, ...models.map(([, u]) => u.in_tokens + u.out_tokens));

  return (
    <div className="p-6 overflow-y-auto h-full space-y-6">
      <h2 className="text-lg font-semibold">Cost dashboard</h2>

      {/* Totals */}
      <div className="grid grid-cols-3 gap-4">
        <Stat label="total tokens" value={cost.total_tokens.toLocaleString()} />
        <Stat label="total cost" value={`$${cost.total_cost_usd.toFixed(4)}`} />
        <Stat label="models used" value={String(models.length)} />
      </div>

      {/* Per-model breakdown */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <div className="text-sm font-medium mb-3">Per model</div>
        <div className="space-y-2">
          {models.map(([model, u]) => {
            const total = u.in_tokens + u.out_tokens;
            return (
              <div key={model}>
                <div className="flex justify-between text-xs mb-1">
                  <span className="text-zinc-300">{model}</span>
                  <span className="text-zinc-500">{total.toLocaleString()} tokens</span>
                </div>
                <div className="flex h-2 rounded overflow-hidden bg-zinc-800">
                  <div className="bg-cyan-600" style={{ width: `${(u.in_tokens / maxTokens) * 100}%` }} />
                  <div className="bg-cyan-400" style={{ width: `${(u.out_tokens / maxTokens) * 100}%` }} />
                </div>
                <div className="flex justify-between text-xs text-zinc-600 mt-0.5">
                  <span>in {u.in_tokens.toLocaleString()}</span>
                  <span>out {u.out_tokens.toLocaleString()}</span>
                </div>
              </div>
            );
          })}
          {models.length === 0 && <div className="text-zinc-600 text-sm text-center py-4">no model calls yet</div>}
        </div>
        <div className="flex gap-4 mt-4 text-xs text-zinc-500 pt-3 border-t border-zinc-800">
          <span>■ in {totalInOut.in.toLocaleString()}</span>
          <span>■ out {totalInOut.out.toLocaleString()}</span>
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className="text-2xl font-mono mt-1">{value}</div>
    </div>
  );
}
