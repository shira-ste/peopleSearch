import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import App from './App'

type Reply = { events: object[] } | { status: number; body: unknown }

/** A streamed reply (one JSON event per line), as the backend sends from /api/chat/stream. */
function streamed(...events: object[]): Reply {
  return { events }
}

const answer = (text: string, ...tools: object[]): Reply =>
  streamed(...tools, { type: 'text', text: text.slice(0, 3) }, { type: 'text', text: text.slice(3) }, { type: 'done' })

/** Replace fetch with a mock that returns the given replies in order. */
function mockBackend(...replies: Reply[]) {
  const fetchMock = vi.fn<typeof fetch>()
  for (const reply of replies) {
    const response =
      'events' in reply
        ? new Response(reply.events.map((e) => JSON.stringify(e) + '\n').join(''), {
            headers: { 'Content-Type': 'application/x-ndjson' },
          })
        : new Response(JSON.stringify(reply.body), { status: reply.status })
    fetchMock.mockResolvedValueOnce(response)
  }
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function sentMessages(fetchMock: ReturnType<typeof mockBackend>, call: number): unknown {
  const init = fetchMock.mock.calls[call][1]
  return JSON.parse(String(init?.body)).messages
}

const LONDON_TOOL = [
  { type: 'tool_call', name: 'count_people', args: { filter: { city: 'London' } } },
  { type: 'tool_result', name: 'count_people', args: { filter: { city: 'London' } }, result: '{"total": 55}', is_error: false },
]

describe('People Search chat', () => {
  it('shows example questions before the first message', () => {
    render(<App />)
    expect(screen.getByRole('button', { name: 'How many people live in London?' })).toBeInTheDocument()
  })

  it('disables Send until a question is typed', async () => {
    render(<App />)
    const send = screen.getByRole('button', { name: 'Send' })
    expect(send).toBeDisabled()
    await userEvent.type(screen.getByRole('textbox'), 'Hi')
    expect(send).toBeEnabled()
  })

  it('streams the answer and shows the tools used', async () => {
    const fetchMock = mockBackend(answer('55 people live in London.', ...LONDON_TOOL))
    render(<App />)

    await userEvent.type(screen.getByRole('textbox'), 'How many in London?{Enter}')

    expect(await screen.findByText('55 people live in London.')).toBeInTheDocument() // pieces joined
    expect(screen.getByText('Tools used (1)')).toBeInTheDocument()
    expect(screen.getByText('count_people')).toBeInTheDocument()
    expect(fetchMock.mock.calls[0][0]).toBe('/api/chat/stream')
    expect(sentMessages(fetchMock, 0)).toEqual([{ role: 'user', content: 'How many in London?' }])
  })

  it('shows live tool progress before the answer arrives', async () => {
    let finish = () => {}
    const encoder = new TextEncoder()
    const chunks = [encoder.encode(JSON.stringify(LONDON_TOOL[0]) + '\n')]
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        const next = chunks.shift()
        if (next) return controller.enqueue(next)
        // Hold the stream open (tool still running) until the test releases it.
        return new Promise<void>((resolve) => {
          finish = () => {
            controller.enqueue(encoder.encode('{"type":"text","text":"55."}\n{"type":"done"}\n'))
            controller.close()
            resolve()
          }
        })
      },
    })
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(body)))
    render(<App />)

    await userEvent.type(screen.getByRole('textbox'), 'London?{Enter}')
    expect(await screen.findByText(/Looking up/)).toBeInTheDocument()

    finish()
    expect(await screen.findByText('55.')).toBeInTheDocument()
    expect(screen.queryByText(/Looking up/)).not.toBeInTheDocument()
  })

  it('clicking an example asks it', async () => {
    const fetchMock = mockBackend(answer('36.45 years.'))
    render(<App />)
    await userEvent.click(screen.getByRole('button', { name: 'What is the average age of employees?' }))
    expect(await screen.findByText('36.45 years.')).toBeInTheDocument()
    expect(sentMessages(fetchMock, 0)).toEqual([{ role: 'user', content: 'What is the average age of employees?' }])
  })

  it('shows an error event from the stream inline', async () => {
    mockBackend(streamed({ type: 'error', status: 503, detail: 'The people data service is unavailable.' }))
    render(<App />)
    await userEvent.type(screen.getByRole('textbox'), 'London?{Enter}')
    expect(await screen.findByText('The people data service is unavailable.')).toBeInTheDocument()
  })

  it('shows errors rejected before streaming and leaves them out of the history', async () => {
    const fetchMock = mockBackend({ status: 500, body: { detail: 'ANTHROPIC_API_KEY is not set.' } }, answer('24.'))
    render(<App />)

    await userEvent.type(screen.getByRole('textbox'), 'London?{Enter}')
    expect(await screen.findByText('ANTHROPIC_API_KEY is not set.')).toBeInTheDocument()

    await userEvent.type(screen.getByRole('textbox'), 'Tel Aviv?{Enter}')
    expect(await screen.findByText('24.')).toBeInTheDocument()
    // The error bubble is not sent back as if the assistant had said it.
    expect(sentMessages(fetchMock, 1)).toEqual([
      { role: 'user', content: 'London?' },
      { role: 'user', content: 'Tel Aviv?' },
    ])
  })

  it('treats a stream that stops without "done" as interrupted, not as a finished answer', async () => {
    const fetchMock = mockBackend(streamed({ type: 'text', text: 'Half an ans' }), answer('24.'))
    render(<App />)
    await userEvent.type(screen.getByRole('textbox'), 'London?{Enter}')
    expect(await screen.findByText(/connection was interrupted/)).toBeInTheDocument()

    await userEvent.type(screen.getByRole('textbox'), 'Tel Aviv?{Enter}')
    await screen.findByText('24.')
    // The cut-off reply is not sent back as if the assistant had said it.
    expect(sentMessages(fetchMock, 1)).toEqual([
      { role: 'user', content: 'London?' },
      { role: 'user', content: 'Tel Aviv?' },
    ])
  })

  it('reads a final event that has no trailing newline', async () => {
    const body = '{"type":"text","text":"55."}\n{"type":"done"}'
    vi.stubGlobal('fetch', vi.fn<typeof fetch>().mockResolvedValue(new Response(body)))
    render(<App />)
    await userEvent.type(screen.getByRole('textbox'), 'London?{Enter}')
    expect(await screen.findByText('55.')).toBeInTheDocument()
    expect(screen.queryByText(/interrupted/)).not.toBeInTheDocument()
  })

  it('sends the conversation history for follow-up questions', async () => {
    const fetchMock = mockBackend(answer('55 in London.'), answer('27 in NYC.'))
    render(<App />)
    await userEvent.type(screen.getByRole('textbox'), 'London?{Enter}')
    await screen.findByText('55 in London.')
    await userEvent.type(screen.getByRole('textbox'), 'And NYC?{Enter}')
    await screen.findByText('27 in NYC.')
    expect(sentMessages(fetchMock, 1)).toEqual([
      { role: 'user', content: 'London?' },
      { role: 'assistant', content: '55 in London.' },
      { role: 'user', content: 'And NYC?' },
    ])
  })
})
