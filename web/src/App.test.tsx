import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import App from './App'

/** Replace fetch with a mock that returns the given responses in order. */
function mockBackend(...responses: { status?: number; body: unknown }[]) {
  const fetchMock = vi.fn<typeof fetch>()
  for (const { status = 200, body } of responses) {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(body), { status }))
  }
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function sentMessages(fetchMock: ReturnType<typeof mockBackend>, call: number): unknown {
  const init = fetchMock.mock.calls[call][1]
  return JSON.parse(String(init?.body)).messages
}

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

  it('sends the question and shows the answer with the tools used', async () => {
    const fetchMock = mockBackend({
      body: {
        answer: '55 people live in London.',
        tool_calls: [{ name: 'count_people', args: { filter: { city: 'London' } }, result: '{"total": 55}', is_error: false }],
      },
    })
    render(<App />)

    await userEvent.type(screen.getByRole('textbox'), 'How many in London?{Enter}')

    expect(await screen.findByText('55 people live in London.')).toBeInTheDocument()
    expect(screen.getByText('Tools used (1)')).toBeInTheDocument()
    expect(screen.getByText('count_people')).toBeInTheDocument()
    expect(sentMessages(fetchMock, 0)).toEqual([{ role: 'user', content: 'How many in London?' }])
  })

  it('clicking an example asks it', async () => {
    const fetchMock = mockBackend({ body: { answer: '36.4 years.', tool_calls: [] } })
    render(<App />)
    await userEvent.click(screen.getByRole('button', { name: 'What is the average age of employees?' }))
    expect(await screen.findByText('36.4 years.')).toBeInTheDocument()
    expect(sentMessages(fetchMock, 0)).toEqual([{ role: 'user', content: 'What is the average age of employees?' }])
  })

  it('shows backend errors inline and leaves them out of the history', async () => {
    const fetchMock = mockBackend(
      { status: 503, body: { detail: 'The people data service is unavailable.' } },
      { body: { answer: '24.', tool_calls: [] } },
    )
    render(<App />)

    await userEvent.type(screen.getByRole('textbox'), 'London?{Enter}')
    expect(await screen.findByText('The people data service is unavailable.')).toBeInTheDocument()

    await userEvent.type(screen.getByRole('textbox'), 'Tel Aviv?{Enter}')
    expect(await screen.findByText('24.')).toBeInTheDocument()
    // The error bubble is not sent back as if the assistant had said it.
    expect(sentMessages(fetchMock, 1)).toEqual([
      { role: 'user', content: 'London?' },
      { role: 'user', content: 'Tel Aviv?' },
    ])
  })

  it('sends the conversation history for follow-up questions', async () => {
    const fetchMock = mockBackend({ body: { answer: '55.', tool_calls: [] } }, { body: { answer: '27.', tool_calls: [] } })
    render(<App />)
    await userEvent.type(screen.getByRole('textbox'), 'London?{Enter}')
    await screen.findByText('55.')
    await userEvent.type(screen.getByRole('textbox'), 'And NYC?{Enter}')
    await screen.findByText('27.')
    expect(sentMessages(fetchMock, 1)).toEqual([
      { role: 'user', content: 'London?' },
      { role: 'assistant', content: '55.' },
      { role: 'user', content: 'And NYC?' },
    ])
  })
})
