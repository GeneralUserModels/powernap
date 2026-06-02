"""GET /api/events — Server-Sent Events stream for real-time push."""

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api", tags=["events"])

logger = logging.getLogger(__name__)


@router.get("/events")
async def stream_events(request: Request):
    state = request.app.state.server

    async def generator():
        # Bounded: a slow/orphaned consumer holds at most this many messages.
        # broadcast() drops the oldest message when full, so the queue can
        # never grow without limit regardless of how far a client falls behind.
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        state.sse_queues.add(queue)
        logger.info(f"SSE client connected ({len(state.sse_queues)} total)")
        try:
            # Flush headers immediately (fires the client's onopen, defeats proxy
            # buffering) and set the reconnect backoff to tame reconnect storms.
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                # Wake periodically even with no events so we re-check the
                # disconnect above; the keepalive write also fails fast on a
                # dead socket, breaking the loop instead of parking forever.
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(msg)}\n\n"
        finally:
            state.sse_queues.discard(queue)
            logger.info(f"SSE client disconnected ({len(state.sse_queues)} total)")

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
