import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// RTL auto-cleanup only self-registers when a global afterEach exists (Vitest globals: true).
// This config runs without globals, so unmount rendered trees between tests explicitly —
// otherwise DOM from earlier tests accumulates and queries find multiple matches.
afterEach(cleanup)

// ---- @xyflow/react under jsdom (LineageView tests) ----------------------------------------
// React Flow measures nodes and the viewport with browser APIs jsdom does not implement.
// These are the mocks from the xyflow testing guide (reactflow.dev/learn/advanced-use/testing):
//  * ResizeObserver: fires the callback once on observe so nodes report as measured.
//  * DOMMatrixReadOnly: the zoom pane reads m22 for the current scale.
//  * offsetWidth/offsetHeight: derived from the inline style LineageView sets on every node
//    (falling back to 1 so nothing measures as zero).
//  * SVGElement.getBBox: edge label placement.
class ResizeObserverStub {
  callback: ResizeObserverCallback
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
  }
  observe(target: Element) {
    // xyflow reads both entry.target (node measurement) and entry.contentRect (zoom extent).
    // Fire ASYNCHRONOUSLY like a real ResizeObserver: the node-measurement callback needs the
    // flow container's own mount effect (which registers domNode) to have run first, and child
    // effects run before parent effects in React.
    const contentRect = target.getBoundingClientRect()
    queueMicrotask(() => {
      this.callback(
        [{ target, contentRect } as ResizeObserverEntry],
        this as unknown as ResizeObserver,
      )
    })
  }
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver

class DOMMatrixReadOnlyStub {
  m22: number
  constructor(transform?: string) {
    const scale = transform?.match(/scale\(([0-9.]+)\)/)?.[1]
    this.m22 = scale === undefined ? 1 : +scale
  }
}
globalThis.DOMMatrixReadOnly = DOMMatrixReadOnlyStub as unknown as typeof DOMMatrixReadOnly

Object.defineProperties(HTMLElement.prototype, {
  offsetHeight: {
    get(this: HTMLElement) {
      return parseFloat(this.style.height) || 1
    },
  },
  offsetWidth: {
    get(this: HTMLElement) {
      return parseFloat(this.style.width) || 1
    },
  },
})

;(SVGElement.prototype as unknown as { getBBox: () => DOMRect }).getBBox = () =>
  ({ x: 0, y: 0, width: 0, height: 0 }) as DOMRect
