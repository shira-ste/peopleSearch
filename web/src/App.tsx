import { useEffect, useRef, useState } from 'react'

// Mirrors the chat backend's response models (chat/main.py).
interface ToolCall {
  name: string
  args: Record<string, unknown>
  result: string
  is_error: boolean
}

interface ChatResponse {
  answer: string
  tool_calls: ToolCall[]
}

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  toolCalls?: ToolCall[]
  isError?: boolean
}

const EXAMPLES = [
  'How many people live in London?',
  'What is the average age of employees?',
  'How many hourly employees work in London?',
  'Average salary by office',
  'Average salary of everyone in USD',
  'Who reports to Bea?',
]

async function askBackend(history: ChatMessage[]): Promise<ChatResponse> {
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // Error bubbles are UI-only; send just the real conversation.
    body: JSON.stringify({
      messages: history.filter((m) => !m.isError).map(({ role, content }) => ({ role, content })),
    }),
  })
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null
    const detail = typeof body?.detail === 'string' ? body.detail : `Request failed (${res.status})`
    throw new Error(detail)
  }
  return (await res.json()) as ChatResponse
}

function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
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
          <pre>{prettyJson(call.result)}</pre>
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

  useEffect(() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' }), [messages, loading])

  async function send(question: string) {
    const text = question.trim()
    if (!text || loading) return
    const history: ChatMessage[] = [...messages, { role: 'user', content: text }]
    setMessages(history)
    setInput('')
    setLoading(true)
    try {
      const { answer, tool_calls } = await askBackend(history)
      setMessages([...history, { role: 'assistant', content: answer, toolCalls: tool_calls }])
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Something went wrong.'
      setMessages([...history, { role: 'assistant', content: message, isError: true }])
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
        {messages.map((m, i) => (
          <div key={i} className={`message ${m.role}${m.isError ? ' error' : ''}`}>
            <div className="content">{m.content}</div>
            {m.toolCalls && m.toolCalls.length > 0 && <ToolCalls calls={m.toolCalls} />}
          </div>
        ))}
        {loading && <div className="message assistant loading">Thinking…</div>}
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
