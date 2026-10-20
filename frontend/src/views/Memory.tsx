import { useEffect, useState } from "react";
import { Mem, type Memory } from "../lib/api";

export function MemoryView() {
  const [mems, setMems] = useState<Memory[]>([]);
  const [typeFilter, setTypeFilter] = useState<string>("");
  const [searchQ, setSearchQ] = useState("");
  const [searchResults, setSearchResults] = useState<any[] | null>(null);
  const [selected, setSelected] = useState<Memory | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      setError(null);
      setMems(await Mem.list(500, typeFilter || undefined));
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
  }, [typeFilter]);

  async function doSearch() {
    if (!searchQ.trim()) {
      setSearchResults(null);
      return;
    }
    try {
      const r = await Mem.search(searchQ, 20);
      setSearchResults(r.hits);
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function del(id: string) {
    await Mem.delete(id);
    setSelected(null);
    load();
  }

  const displayed = searchResults
    ? searchResults.map((h) => ({ id: h.id, text: h.preview, type: h.type, importance: h.importance, tags: [], created_at: 0, last_accessed: 0, access_count: 0, source: "", metadata: {} } as Memory))
    : mems;

  return (
    <div className="flex h-full">
      {/* List pane */}
      <div className="flex-1 flex flex-col border-r border-zinc-800">
        {/* Toolbar */}
        <div className="px-4 py-3 border-b border-zinc-800 flex items-center gap-2">
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-sm"
          >
            <option value="">all types</option>
            <option value="semantic">semantic</option>
            <option value="episodic">episodic</option>
            <option value="procedural">procedural</option>
          </select>
          <input
            value={searchQ}
            onChange={(e) => setSearchQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doSearch()}
            placeholder="vector search…"
            className="flex-1 bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-sm"
          />
          <button onClick={doSearch} className="px-3 py-1 bg-zinc-700 hover:bg-zinc-600 rounded text-sm">
            Search
          </button>
          {searchResults && (
            <button onClick={() => { setSearchResults(null); setSearchQ(""); }} className="text-xs text-zinc-500 hover:text-zinc-300">
              clear
            </button>
          )}
          <button onClick={() => setShowAdd(true)} className="px-3 py-1 bg-cyan-600 hover:bg-cyan-500 rounded text-sm">
            + Add
          </button>
        </div>
        {error && <div className="px-4 py-2 text-sm text-red-400">{error}</div>}

        {/* Table */}
        <div className="flex-1 overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-zinc-900 border-b border-zinc-800 text-zinc-500 text-xs">
              <tr>
                <th className="text-left px-4 py-2">type</th>
                <th className="text-right px-2 py-2">imp</th>
                <th className="text-left px-2 py-2">text</th>
                <th className="text-left px-2 py-2">source</th>
              </tr>
            </thead>
            <tbody>
              {displayed.map((m) => (
                <tr
                  key={m.id}
                  onClick={() => setSelected(m)}
                  className={`border-b border-zinc-800/50 cursor-pointer hover:bg-zinc-800/50 ${
                    selected?.id === m.id ? "bg-zinc-800" : ""
                  }`}
                >
                  <td className="px-4 py-2 text-cyan-400 text-xs">{m.type}</td>
                  <td className="px-2 py-2 text-right text-zinc-400">{m.importance.toFixed(1)}</td>
                  <td className="px-2 py-2 text-zinc-300 truncate max-w-md">{m.text}</td>
                  <td className="px-2 py-2 text-zinc-600 text-xs">{m.source}</td>
                </tr>
              ))}
              {displayed.length === 0 && (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-zinc-600">no memories</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Detail pane */}
      {selected && (
        <div className="w-96 p-4 overflow-y-auto bg-zinc-900">
          <div className="flex justify-between items-start mb-3">
            <h3 className="font-semibold">Memory detail</h3>
            <button onClick={() => setSelected(null)} className="text-zinc-500 hover:text-zinc-300">✕</button>
          </div>
          <MemoryDetail mem={selected} onDelete={del} />
        </div>
      )}

      {/* Add modal */}
      {showAdd && <AddModal onClose={() => { setShowAdd(false); load(); }} />}
    </div>
  );
}

function MemoryDetail({ mem, onDelete }: { mem: Memory; onDelete: (id: string) => void }) {
  return (
    <div className="space-y-3 text-sm">
      <div>
        <div className="text-zinc-500 text-xs mb-1">text</div>
        <div className="bg-zinc-800 rounded p-2 whitespace-pre-wrap">{mem.text}</div>
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <Field label="type" value={mem.type} />
        <Field label="importance" value={mem.importance.toFixed(1)} />
        <Field label="source" value={mem.source} />
        <Field label="access count" value={String(mem.access_count)} />
        <Field label="created" value={new Date(mem.created_at * 1000).toLocaleString()} />
        <Field label="last accessed" value={new Date(mem.last_accessed * 1000).toLocaleString()} />
      </div>
      {mem.tags.length > 0 && (
        <div>
          <div className="text-zinc-500 text-xs mb-1">tags</div>
          <div className="flex flex-wrap gap-1">
            {mem.tags.map((t) => (
              <span key={t} className="text-xs px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-400">{t}</span>
            ))}
          </div>
        </div>
      )}
      <button
        onClick={() => onDelete(mem.id)}
        className="px-3 py-1 bg-red-900 hover:bg-red-800 text-red-300 rounded text-xs"
      >
        Delete
      </button>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-zinc-800 rounded p-2">
      <div className="text-zinc-600">{label}</div>
      <div className="text-zinc-300">{value}</div>
    </div>
  );
}

function AddModal({ onClose }: { onClose: () => void }) {
  const [text, setText] = useState("");
  const [type, setType] = useState("semantic");
  const [importance, setImportance] = useState(5);
  const [tags, setTags] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    try {
      await Mem.add({
        text,
        type,
        importance,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      });
      onClose();
    } catch (e: any) {
      setErr(e.message);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-6 w-96 space-y-3" onClick={(e) => e.stopPropagation()}>
        <h3 className="font-semibold">Add memory</h3>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="The fact or event to remember…"
          className="w-full bg-zinc-800 border border-zinc-700 rounded px-3 py-2 text-sm h-24 resize-none"
        />
        <div className="flex gap-2">
          <select value={type} onChange={(e) => setType(e.target.value)} className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm">
            <option value="semantic">semantic</option>
            <option value="episodic">episodic</option>
            <option value="procedural">procedural</option>
          </select>
          <input type="number" min={1} max={10} value={importance} onChange={(e) => setImportance(Number(e.target.value))} className="w-20 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm" />
          <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="tags, comma-sep" className="flex-1 bg-zinc-800 border border-zinc-700 rounded px-2 py-1 text-sm" />
        </div>
        {err && <div className="text-red-400 text-sm">{err}</div>}
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="px-3 py-1 text-sm text-zinc-400 hover:text-zinc-200">Cancel</button>
          <button onClick={save} disabled={!text.trim()} className="px-3 py-1 bg-cyan-600 hover:bg-cyan-500 disabled:bg-zinc-700 rounded text-sm">Save</button>
        </div>
      </div>
    </div>
  );
}
