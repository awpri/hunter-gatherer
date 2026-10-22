import { useEffect, useRef, useState } from "react";
import { Feedback } from "../lib/api";
import { StreamClient, type LoopEvent } from "../lib/ws";

interface Message {
  role: "user" | "assistant";
  content: string;
  events: LoopEvent[]; // route/tool/skill/etc. chips for this turn
  streaming?: boolean;
}

export function ChatView({ onTurnComplete }: { onTurnComplete: () => void }) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const clientRef = useRef<StreamClient | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Pending turn state (accumulated during streaming).
  const pendingEvents = useRef<LoopEvent[]>([]);
  const pendingText = useRef<string>("");

  useEffect(() => {
    const client = new StreamClient();
    clientRef.current = client;
    client.connect(
      (evt) => handleEvent(evt),
      () => setConnected(false),
    );
    // Connection opens asynchronously.
    setTimeout(() => setConnected(client.isConnected), 200);
    return () => client.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  function handleEvent(evt: LoopEvent) {
    if (evt.kind === "turn_start") {
      pendingEvents.current = [];
      pendingText.current = "";
      setBusy(true);
      return;
    }
    if (evt.kind === "token") {
      pendingText.current += evt.data.delta || "";
      // Update the in-flight assistant message.
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === "assistant" && last.streaming) {
          next[next.length - 1] = { ...last, content: pendingText.current };
        }
        return next;
      });
      return;
    }
    if (evt.kind === "route" || evt.kind === "tool" || evt.kind === "skill" || evt.kind === "reflection" || evt.kind === "autotune") {
      pendingEvents.current.push(evt);
      return;
    }
    if (evt.kind === "turn_end") {
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === "assistant" && last.streaming) {
          next[next.length - 1] = {
            ...last,
            content: evt.data.answer || pendingText.current,
            events: pendingEvents.current,
            streaming: false,
          };
        }
        return next;
      });
      setBusy(false);
      onTurnComplete();
      return;
    }
    if (evt.kind === "error") {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `⚠️ ${evt.data.detail}`, events: [], streaming: false },
      ]);
      setBusy(false);
      return;
    }
  }

  function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setMessages((prev) => [
      ...prev,
      { role: "user", content: text, events: [] },
      { role: "assistant", content: "", events: [], streaming: true },
    ]);
    clientRef.current?.send(text);
  }

  return (
    <div className="flex flex-col h-full">
      {/* Connection bar */}
      <div className="px-4 py-2 border-b border-zinc-800 flex items-center gap-2 text-xs">
        <span
          className={`inline-block w-2 h-2 rounded-full ${connected ? "bg-green-500" : "bg-zinc-600"}`}
        />
        <span className="text-zinc-500">{connected ? "connected" : "disconnected"}</span>
        {busy && <span className="text-cyan-400 animate-pulse">● working…</span>}
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-6 space-y-4">
        {messages.length === 0 && (
          <div className="text-zinc-600 text-sm text-center mt-20">
            Ask hunter/gatherer anything. Routed, memory-primed, tool-using.
          </div>
        )}
        {messages.map((m, i) => (
          <MessageBubble key={i} msg={m} showFeedback={!m.streaming && i === messages.length - 1} />
        ))}
      </div>

      {/* Composer */}
      <div className="border-t border-zinc-800 p-4">
        <div className="flex gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            placeholder="Type a message…"
            className="flex-1 bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-sm focus:outline-none focus:border-cyan-500"
            disabled={busy}
          />
          <button
            onClick={send}
            disabled={busy || !input.trim()}
            className="px-4 py-2 bg-cyan-600 hover:bg-cyan-500 disabled:bg-zinc-700 disabled:text-zinc-500 rounded text-sm font-medium transition-colors"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}

function MessageBubble({ msg, showFeedback = false }: { msg: Message; showFeedback?: boolean }) {
  const isUser = msg.role === "user";
  const [rated, setRated] = useState<number | null>(null);
  const [correction, setCorrection] = useState("");
  const [showCorrect, setShowCorrect] = useState(false);

  async function rate(rating: number) {
    setRated(rating);
    if (rating < 0) setShowCorrect(true); // offer a correction on 👎
    else if (correction.trim()) {
      await Feedback.rate(rating, correction.trim());
    } else {
      await Feedback.rate(rating);
    }
  }

  async function submitCorrection() {
    if (correction.trim()) await Feedback.rate(-1, correction.trim());
    setShowCorrect(false);
  }

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[80%] ${isUser ? "order-2" : ""}`}>
        {/* Event chips (route/tool/etc.) above the assistant answer */}
        {!isUser && msg.events.length > 0 && (
          <div className="flex flex-wrap gap-1 mb-1">
            {msg.events.map((e, i) => (
              <EventChip key={i} event={e} />
            ))}
          </div>
        )}
        <div
          className={`rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
            isUser
              ? "bg-cyan-600 text-white"
              : msg.streaming
                ? "bg-zinc-900 text-zinc-300 border border-zinc-800"
                : "bg-zinc-800 text-zinc-100"
          }`}
        >
          {msg.content || (msg.streaming ? "…" : "")}
          {msg.streaming && <span className="inline-block w-2 h-4 bg-cyan-400 ml-1 animate-pulse" />}
        </div>
        {/* Explicit feedback (2026-08-31 #3): gold labels for the flywheels */}
        {showFeedback && !isUser && rated === null && (
          <div className="flex gap-2 mt-1 text-xs text-zinc-500">
            <button onClick={() => rate(1)} className="hover:text-green-400 transition-colors" title="Helpful — gold label for tuning">
              👍
            </button>
            <button onClick={() => rate(-1)} className="hover:text-red-400 transition-colors" title="Not helpful — you can leave a correction">
              👎
            </button>
          </div>
        )}
        {showFeedback && !isUser && showCorrect && (
          <div className="flex gap-2 mt-1">
            <input
              value={correction}
              onChange={(e) => setCorrection(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submitCorrection()}
              placeholder="Optional correction (stored as a correction memory)…"
              className="flex-1 bg-zinc-900 border border-zinc-700 rounded px-2 py-1 text-xs focus:outline-none focus:border-cyan-500"
            />
            <button
              onClick={submitCorrection}
              className="px-2 py-1 bg-zinc-700 hover:bg-zinc-600 rounded text-xs"
            >
              Send
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function EventChip({ event }: { event: LoopEvent }) {
  const { kind, data } = event;
  const tierStyles: Record<string, string> = {
    local: "bg-cyan-500/20 text-cyan-400 border-cyan-500/30",
    cheap: "bg-amber-500/20 text-amber-400 border-amber-500/30",
    cloud: "bg-pink-500/20 text-pink-400 border-pink-500/30",
  };
  const styles: Record<string, string> = {
    route: `text-xs px-1.5 py-0.5 rounded border ${tierStyles[data.tier] || tierStyles.local}`,
    skill: "text-xs px-1.5 py-0.5 rounded bg-green-500/20 text-green-400 border border-green-500/30",
    tool: "text-xs px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400 border border-blue-500/30",
    reflection: "text-xs px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400 border border-purple-500/30",
    autotune: "text-xs px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/30",
  };
  const label: Record<string, string> = {
    route: `route → ${data.tier}`,
    skill: `skill: ${data.name}`,
    tool: `🔧 ${data.name}`,
    reflection: `reflection (summarized ${data.summarized})`,
    autotune: `α ${data.old_alpha}→${data.new_alpha}`,
  };
  return <span className={styles[kind] || "text-xs text-zinc-500"}>{label[kind] || kind}</span>;
}
