import { CHAT_EVENT_NAMES, conversationEventsUrl, type ChatEvent } from '~/lib/api'

/** One frame of the stream with the sequence number its `id:` line carried. */
export interface StreamedEvent {
  seq: number
  event: ChatEvent
}

/** Consecutive drops with no event between them before the stream gives up on a turn. */
const MAX_SILENT_RETRIES = 6
const RETRY_BASE_MS = 1000
const RETRY_CAP_MS = 10000

/**
 * The `chat_events` stream of one conversation, as `EventSource` cannot be used bare
 * (docs/web.md § The agent panel, docs/chat.md § Conversations, turns, events).
 *
 * The server replays every event after `?after=N` and then follows the live tail, and
 * **ends the stream itself** when no turn is queued or running. `EventSource` reads that
 * end as a dropped connection and reconnects on its own — to the *same URL*, so it would
 * replay from the original `after` and hand every event back a second time. So the source
 * is closed on every `error`, and this composable decides what happens next: nothing while
 * the stream is idle (the server closed it because there was nothing to send), and a
 * reconnect from the last seen sequence while a turn is in flight (a real drop, mid-turn).
 *
 * "In flight" is what the stream has seen — `turn_start` opens it, `turn_done` and
 * `turn_error` close it — plus `expectTurn()`, which the panel calls the moment it has
 * posted a message, so a stream that the server closed a beat before the turn was queued
 * is reopened rather than left idle. A turn the server never starts is not waited on
 * forever: a run of drops with no event between them gives up.
 */
export function useConversationStream(onEvent: (frame: StreamedEvent) => void) {
  /** The last sequence seen — the `after` of the next open. */
  const lastSeq = ref(0)
  const connected = ref(false)
  /** A turn is in flight as far as the stream knows. */
  const active = ref(false)

  let conversationId: number | null = null
  let source: EventSource | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let silentRetries = 0

  function clearRetry() {
    if (retryTimer !== null) {
      clearTimeout(retryTimer)
      retryTimer = null
    }
  }

  function disconnect() {
    clearRetry()
    if (source) {
      source.close()
      source = null
    }
    connected.value = false
  }

  function connect() {
    if (conversationId === null || typeof EventSource === 'undefined') {
      return
    }
    disconnect()
    const id = conversationId
    const stream = new EventSource(conversationEventsUrl(id, lastSeq.value), {
      withCredentials: true,
    })
    source = stream

    stream.onopen = () => {
      if (source === stream) {
        connected.value = true
      }
    }

    for (const name of CHAT_EVENT_NAMES) {
      stream.addEventListener(name, (raw) => {
        if (source !== stream) {
          return
        }
        const message = raw as MessageEvent<string>
        let data: Record<string, unknown>
        try {
          data = JSON.parse(message.data || '{}') as Record<string, unknown>
        } catch {
          return
        }
        const seq = Number.parseInt(message.lastEventId, 10)
        if (Number.isFinite(seq) && seq > lastSeq.value) {
          lastSeq.value = seq
        }
        silentRetries = 0
        if (name === 'turn_start') {
          active.value = true
        } else if (name === 'turn_done' || name === 'turn_error') {
          active.value = false
        }
        onEvent({ seq: lastSeq.value, event: { ...data, event: name } as ChatEvent })
      })
    }

    stream.onerror = () => {
      if (source !== stream) {
        return
      }
      // Closed either way — see the file comment — and reopened only mid-turn.
      disconnect()
      if (!active.value) {
        return
      }
      if (silentRetries >= MAX_SILENT_RETRIES) {
        active.value = false
        return
      }
      const delay = Math.min(RETRY_CAP_MS, RETRY_BASE_MS * 2 ** silentRetries)
      silentRetries += 1
      retryTimer = setTimeout(connect, delay)
    }
  }

  /**
   * Starts following `id` from `after` — 0 replays the whole log, which is how the panel
   * recovers the commits of earlier edits. Switching conversations forgets the previous
   * one's cursor.
   */
  function open(id: number, after = 0) {
    disconnect()
    conversationId = id
    lastSeq.value = after
    active.value = false
    silentRetries = 0
    connect()
  }

  /** A turn was just queued: the stream must be up, and a close now is a drop, not idleness. */
  function expectTurn() {
    active.value = true
    silentRetries = 0
    if (!source && retryTimer === null) {
      connect()
    }
  }

  function close() {
    disconnect()
    conversationId = null
    active.value = false
  }

  onBeforeUnmount(close)

  return { lastSeq, connected, active, open, expectTurn, close }
}
