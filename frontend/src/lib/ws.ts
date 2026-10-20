// WebSocket client for the /ws/stream streaming chat endpoint.

export interface LoopEvent {
  kind: string; // turn_start | route | skill | tool | token | reflection | autotune | cost | turn_end | error
  data: Record<string, any>;
}

export type EventCallback = (event: LoopEvent) => void;

export class StreamClient {
  private ws: WebSocket | null = null;
  private url: string;

  constructor() {
    // Construct the WS URL from the current location + token.
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const params = new URLSearchParams();
    const token = localStorage.getItem("proharness_token");
    if (token) params.set("token", token);
    this.url = `${proto}//${window.location.host}/ws/stream${params.toString() ? `?${params}` : ""}`;
  }

  connect(onEvent: EventCallback, onClose?: () => void): void {
    this.ws = new WebSocket(this.url);
    this.ws.onmessage = (e) => {
      try {
        const evt = JSON.parse(e.data) as LoopEvent;
        onEvent(evt);
      } catch {
        // ignore malformed
      }
    };
    this.ws.onclose = () => onClose?.();
    this.ws.onerror = () => onClose?.();
  }

  send(text: string): void {
    this.ws?.send(JSON.stringify({ text }));
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}
