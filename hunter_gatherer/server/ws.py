"""WebSocket /ws/stream — streaming chat over the AgentLoop.

Receives ``{text}``, runs ``AgentLoop.run(text, on_event=ws.send_json,
stream_final=True)`` in a worker thread, and forwards every LoopEvent (now
including ``token`` deltas) as a JSON message. Handles disconnect → sets a
cancel event so the running turn aborts promptly.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)


def register_ws(app: FastAPI) -> None:
    """Register the /ws/stream WebSocket endpoint on the app."""

    @app.websocket("/ws/stream")
    async def ws_stream(ws: WebSocket) -> None:
        await ws.accept()
        loop = app.state.loop
        # Each WebSocket connection gets its own session id (idea #2) so
        # concurrent clients don't interleave conversation history.
        import uuid
        session_id = str(uuid.uuid4())
        # Bridge: worker thread → asyncio → ws.send_json.
        out_queue: asyncio.Queue = asyncio.Queue()
        loop_ref = asyncio.get_running_loop()
        cancel_event = threading.Event()

        def _put(event_dict):
            # Called from the worker thread; schedule onto the event loop.
            with contextlib.suppress(RuntimeError):
                loop_ref.call_soon_threadsafe(out_queue.put_nowait, event_dict)

        async def _pump():
            """Forward queued events to the WebSocket until 'turn_end' or error."""
            try:
                while True:
                    event = await out_queue.get()
                    await ws.send_json(event)
                    if event.get("kind") == "turn_end":
                        return
            except WebSocketDisconnect:
                return
            except Exception as e:
                log.debug("ws pump ended: %s", e)

        try:
            while True:  # one iteration per turn
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_json({"kind": "error", "data": {"detail": "invalid JSON"}})
                    continue
                text = msg.get("text", "").strip()
                # Client may override the session id; otherwise use the
                # connection's own (one session per WS connection).
                turn_session = msg.get("session_id", session_id)
                if not text:
                    continue
                # Run the turn in a worker thread; stream events via _put.
                cancel_event.clear()

                def _run_turn(_text=text, _sid=turn_session):
                    from hunter_gatherer.loop import LoopEvent

                    def on_event(ev: LoopEvent):
                        _put({"kind": ev.kind, "data": ev.data})
                    try:
                        loop.run(_text, on_event=on_event, stream_final=True,
                                 cancel_event=cancel_event, session_id=_sid)
                    except Exception as e:
                        _put({"kind": "error", "data": {"detail": f"{type(e).__name__}: {e}"}})
                        _put({"kind": "turn_end", "data": {"answer": "", "stopped_reason": "error"}})

                worker = threading.Thread(target=_run_turn, daemon=True)
                worker.start()
                # Pump events to the client until the turn ends.
                await _pump()
                worker.join(timeout=1.0)
        except WebSocketDisconnect:
            # Client went away — cancel any in-flight turn.
            cancel_event.set()
        except Exception as e:
            log.warning("ws_stream error: %s", e)
        finally:
            cancel_event.set()

    # ── pipeline run streaming ────────────────────────────────────────────
    @app.websocket("/ws/pipeline/{pipeline_id}/run")
    async def ws_pipeline_run(ws: WebSocket, pipeline_id: str) -> None:
        """Stream a pipeline run: node_start/node_end events as each node
        executes, ending with pipeline_end or pipeline_error. Client sends
        ``{"inputs": {...}}`` to start the run."""
        await ws.accept()
        loop = app.state.loop
        out_queue: asyncio.Queue = asyncio.Queue()
        loop_ref = asyncio.get_running_loop()
        cancel_event = threading.Event()

        def _put(event_dict):
            with contextlib.suppress(RuntimeError):
                loop_ref.call_soon_threadsafe(out_queue.put_nowait, event_dict)

        async def _pump():
            try:
                while True:
                    event = await out_queue.get()
                    await ws.send_json(event)
                    if event.get("kind") in ("pipeline_end", "pipeline_error"):
                        return
            except WebSocketDisconnect:
                return
            except Exception as e:
                log.debug("pipeline ws pump ended: %s", e)

        try:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"kind": "pipeline_error", "data": {"detail": "invalid JSON"}})
                return
            inputs = msg.get("inputs", {})

            # Load the pipeline + build the interpreter.
            pipeline = loop.store.get_pipeline(pipeline_id)
            if not pipeline:
                await ws.send_json({"kind": "pipeline_error", "data": {"detail": "pipeline not found"}})
                return

            from hunter_gatherer.pipelines.interpreter import PipelineInterpreter
            from hunter_gatherer.pipelines.nodes import PipelineContext

            ctx = PipelineContext.from_loop(loop)
            interp = PipelineInterpreter(pipeline, ctx)

            def _run_pipeline():
                from hunter_gatherer.pipelines.interpreter import LoopEvent

                def on_event(ev: LoopEvent):
                    _put({"kind": ev.kind, "data": ev.data})
                try:
                    interp.run(inputs=inputs, on_event=on_event, cancel_event=cancel_event)
                except Exception as e:
                    _put({"kind": "pipeline_error", "data": {"detail": f"{type(e).__name__}: {e}"}})

            worker = threading.Thread(target=_run_pipeline, daemon=True)
            worker.start()
            await _pump()
            worker.join(timeout=2.0)
        except WebSocketDisconnect:
            cancel_event.set()
        except Exception as e:
            log.warning("ws_pipeline_run error: %s", e)
        finally:
            cancel_event.set()
