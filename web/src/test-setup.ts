import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// jsdom doesn't implement scrolling. Like current browsers, return a Promise, so a component that
// accidentally returns it from an effect crashes here too (that bug once left the page blank).
Element.prototype.scrollIntoView = vi.fn(() => Promise.resolve()) as unknown as Element['scrollIntoView']

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})
