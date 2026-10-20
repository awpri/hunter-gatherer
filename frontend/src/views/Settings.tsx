import { useEffect, useState } from "react";
import { Settings as SettingsApi, type SettingsResponse } from "../lib/api";

export function SettingsView() {
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [draft, setDraft] = useState<Record<string, any>>({});
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    try {
      setErr(null);
      const d = await SettingsApi.get();
      setData(d);
      setDraft({ ...d.runtime_tunable });
    } catch (e: any) {
      setErr(e.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function save() {
    try {
      setErr(null);
      // Only send changed fields.
      const changed: Record<string, any> = {};
      if (data) {
        for (const [k, v] of Object.entries(draft)) {
          if (data.runtime_tunable[k] !== v) changed[k] = v;
        }
      }
      if (Object.keys(changed).length === 0) {
        setSaved("nothing changed");
        setTimeout(() => setSaved(null), 1500);
        return;
      }
      const r = await SettingsApi.patch(changed);
      setSaved(`applied: ${Object.keys(r.applied).join(", ")}`);
      setTimeout(() => setSaved(null), 2500);
      load();
    } catch (e: any) {
      setErr(e.message);
    }
  }

  if (err) return <div className="p-4 text-red-400 text-sm">{err}</div>;
  if (!data) return <div className="p-4 text-zinc-500">loading…</div>;

  return (
    <div className="p-6 overflow-y-auto h-full space-y-6">
      <div className="flex items-center gap-4">
        <h2 className="text-lg font-semibold">Settings</h2>
        <button onClick={save} className="px-3 py-1 bg-cyan-600 hover:bg-cyan-500 rounded text-sm">Apply changes</button>
        {saved && <span className="text-green-400 text-sm">{saved}</span>}
      </div>

      {/* Runtime-tunable */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <div className="text-sm font-medium mb-1">Runtime-tunable</div>
        <div className="text-xs text-zinc-500 mb-3">Changes apply live (no restart). α changes clear the router cache.</div>
        <div className="grid grid-cols-2 gap-3">
          {Object.entries(draft).sort().map(([k, v]) => (
            <SettingInput key={k} k={k} v={v} onChange={(nv) => setDraft((d) => ({ ...d, [k]: nv }))} />
          ))}
        </div>
      </div>

      {/* Startup-only (read-only) */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <div className="text-sm font-medium mb-1">Startup-only</div>
        <div className="text-xs text-zinc-500 mb-3">Require a server restart to change.</div>
        <div className="grid grid-cols-2 gap-2 text-xs">
          {Object.entries(data.startup_only).sort().map(([k, v]) => (
            <div key={k} className="flex justify-between bg-zinc-800 rounded px-2 py-1">
              <span className="text-zinc-400">{k}</span>
              <span className="text-zinc-500 font-mono truncate ml-2 max-w-[60%]">{formatVal(v)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function SettingInput({ k, v, onChange }: { k: string; v: any; onChange: (v: any) => void }) {
  const isBool = typeof v === "boolean";
  return (
    <div className="flex items-center gap-2">
      <label className="text-xs text-zinc-400 w-40 truncate" title={k}>{k}</label>
      {isBool ? (
        <input type="checkbox" checked={v} onChange={(e) => onChange(e.target.checked)} className="accent-cyan-500" />
      ) : typeof v === "number" ? (
        <input
          type="number"
          value={v}
          step={Number.isInteger(v) ? 1 : 0.01}
          onChange={(e) => onChange(Number(e.target.value))}
          className="flex-1 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs font-mono"
        />
      ) : (
        <input
          type="text"
          value={String(v)}
          onChange={(e) => onChange(e.target.value)}
          className="flex-1 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-xs font-mono"
        />
      )}
    </div>
  );
}

function formatVal(v: any): string {
  if (v === "" || v === null) return "(empty)";
  if (typeof v === "string" && v.length > 30) return v.slice(0, 30) + "…";
  return String(v);
}
