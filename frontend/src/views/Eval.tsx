import { useEffect, useState } from "react";
import { Telemetry, type EvalResult } from "../lib/api";

export function EvalView() {
  const [result, setResult] = useState<EvalResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    setLoading(true);
    setErr(null);
    try {
      setResult(await Telemetry.eval());
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    run();
  }, []);

  return (
    <div className="p-6 overflow-y-auto h-full space-y-6">
      <div className="flex items-center gap-4">
        <h2 className="text-lg font-semibold">Eval</h2>
        <button
          onClick={run}
          disabled={loading}
          className="px-3 py-1 bg-cyan-600 hover:bg-cyan-500 disabled:bg-zinc-700 rounded text-sm"
        >
          {loading ? "running…" : "re-run"}
        </button>
      </div>

      {err && <div className="text-red-400 text-sm">{err}</div>}

      {result && (
        <>
          <div className="grid grid-cols-3 gap-4">
            <Stat label="behavior pass" value={`${result.behavior_pass}/${result.behavior_total}`} good={result.behavior_pass === result.behavior_total} />
            <Stat label="behavior %" value={`${result.behavior_pct.toFixed(0)}%`} good={result.behavior_pass === result.behavior_total} />
            <Stat label="tasks" value={String(result.behavior_total)} />
          </div>

          <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
            <div className="text-sm font-medium mb-3">Per-task signals</div>
            <div className="space-y-1">
              {result.results.map((t) => (
                <div key={t.id} className="flex items-center gap-2 text-sm">
                  <span className={t.passed ? "text-green-400" : "text-red-400"}>{t.passed ? "✓" : "✗"}</span>
                  <span className="text-zinc-400 text-xs w-20">{t.difficulty}</span>
                  <span className="text-zinc-300 flex-1 truncate">{t.id}</span>
                  {!t.passed && (
                    <span className="text-xs text-red-400/70">
                      {t.signals.filter((s) => !s.passed).map((s) => s.name).join(", ")}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Stat({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className={`text-2xl font-mono mt-1 ${good === true ? "text-green-400" : good === false ? "text-red-400" : ""}`}>{value}</div>
    </div>
  );
}
