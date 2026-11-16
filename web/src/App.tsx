import { useEffect, useRef, useState } from 'react'

// Mirrors the chat backend's streamed events (chat/src/people_chat/models.py): one JSON object per line.
type StreamEvent =
  | { type: 'text'; text: string }
  | { type: 'tool_call'; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; name: string; args: Record<string, unknown>; result: string; is_error: boolean }
  | { type: 'error'; status: number; detail: string }
  | { type: 'done' }

interface ToolCall {
  name: string
  args: Record<string, unknown>
  result?: string // missing while the tool is still running
  is_error?: boolean
}

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  toolCalls?: ToolCall[]
  isError?: boolean
  pending?: boolean // still streaming
}

const EXAMPLES = [
  'How many people live in London?',
  'What is the average age of employees?',
  'How many hourly employees work in London?',
  'Average salary by office',
  'Average salary of everyone in USD',
  'Who reports to Bea?',
]

/** POST the conversation and call onEvent for each streamed event as it arrives. */
async function streamChat(history: ChatMessage[], onEvent: (event: StreamEvent) => void): Promise<void> {
  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // Error bubbles and empty replies are UI-only; send just the real conversation.
    body: JSON.stringify({
      messages: history.filter((m) => !m.isError && m.content.trim()).map(({ role, content }) => ({ role, content })),
    }),
  })
  if (!res.ok || !res.body) {
    // Requests rejected before streaming starts (bad input, missing API key) come back as plain JSON errors.
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null
    throw new Error(typeof body?.detail === 'string' ? body.detail : `Request failed (${res.status})`)
  }

  let finished = false // a complete stream always ends with a "done" or "error" event
  const handle = (line: string) => {
    if (!line.trim()) return
    const event = JSON.parse(line) as StreamEvent
    finished ||= event.type === 'done' || event.type === 'error'
    onEvent(event)
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? '' // keep a partial line until the rest arrives
    lines.forEach(handle)
  }
  handle(buffer) // a last line without a trailing newline
  if (!finished) throw new Error('The connection was interrupted before the answer finished. Please try again.')
}

/** Apply one streamed event to the assistant message being built. */
function applyEvent(message: ChatMessage, event: StreamEvent): ChatMessage {
  const toolCalls = message.toolCalls ?? []
  switch (event.type) {
    case 'text':
      return { ...message, content: message.content + event.text }
    case 'tool_call':
      return { ...message, toolCalls: [...toolCalls, { name: event.name, args: event.args }] }
    case 'tool_result': {
      // Fill in the oldest call of this tool that is still waiting for its result.
      const index = toolCalls.findIndex((c) => c.name === event.name && c.result === undefined)
      const done = { name: event.name, args: event.args, result: event.result, is_error: event.is_error }
      return { ...message, toolCalls: index < 0 ? [...toolCalls, done] : toolCalls.with(index, done) }
    }
    case 'error':
      return { ...message, content: event.detail, isError: true, pending: false }
    case 'done':
      return { ...message, pending: false }
  }
}

function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

/** Live progress while tools run, e.g. "Looking up count_people…". */
function ToolProgress({ calls }: { calls: ToolCall[] }) {
  return (
    <ul className="tool-progress">
      {calls.map((call, i) => (
        <li key={i}>
          {call.result === undefined ? '🔧 Looking up' : call.is_error ? '⚠️ Problem with' : '✓ Checked'}{' '}
          <code>{call.name}</code>
          {call.result === undefined && '…'}
        </li>
      ))}
    </ul>
  )
}

function ToolCalls({ calls }: { calls: ToolCall[] }) {
  return (
    <details className="tools">
      <summary>Tools used ({calls.length})</summary>
      {calls.map((call, i) => (
        <div key={i} className={call.is_error ? 'tool tool-error' : 'tool'}>
          <code className="tool-name">{call.name}</code>
          <pre>{JSON.stringify(call.args, null, 2)}</pre>
          <div className="tool-label">{call.is_error ? 'Error' : 'Result'}</div>
          <pre>{prettyJson(call.result ?? '')}</pre>
        </div>
      ))}
    </details>
  )
}

export default function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // Block body on purpose: an effect's return value is treated as a cleanup function.
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  /** Update the last message (the assistant reply being streamed). */
  function updateReply(update: (reply: ChatMessage) => ChatMessage) {
    setMessages((current) => current.with(-1, update(current[current.length - 1])))
  }

  async function send(question: string) {
    const text = question.trim()
    if (!text || loading) return
    const history: ChatMessage[] = [...messages, { role: 'user', content: text }]
    setMessages([...history, { role: 'assistant', content: '', toolCalls: [], pending: true }])
    setInput('')
    setLoading(true)
    try {
      await streamChat(history, (event) => updateReply((reply) => applyEvent(reply, event)))
      updateReply((reply) => ({ ...reply, pending: false }))
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Something went wrong.'
      updateReply(() => ({ role: 'assistant', content: message, isError: true }))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="app">
      <header>
        <h1>People Search</h1>
        <p>Ask questions about the people records. Answers come from the MCP server.</p>
      </header>

      <section className="messages">
        {messages.length === 0 && (
          <div className="examples">
            {EXAMPLES.map((q) => (
              <button key={q} onClick={() => void send(q)}>
                {q}
              </button>
            ))}
          </div>
        )}
        {messages.map((m, i) => {
          const calls = m.toolCalls ?? []
          const waiting = m.pending && !m.content
          return (
            <div key={i} className={`message ${m.role}${m.isError ? ' error' : ''}${waiting ? ' loading' : ''}`}>
              {m.pending && calls.length > 0 && <ToolProgress calls={calls} />}
              {waiting && calls.length === 0 && <div>Thinking…</div>}
              {m.content && <div className="content">{m.content}</div>}
              {!m.pending && calls.length > 0 && <ToolCalls calls={calls} />}
            </div>
          )
        })}
        <div ref={bottomRef} />
      </section>

      <form
        onSubmit={(e) => {
          e.preventDefault()
          void send(input)
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="e.g. How many people live in Tel Aviv?"
          autoFocus
        />
        <button type="submit" disabled={loading || !input.trim()}>
          Send
        </button>
      </form>
    </main>
  )
}
