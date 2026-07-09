// Scoped jsdom shim for the LineageView (xyflow) suite ONLY. Importing this module installs it
// for the importing test file's environment; it is deliberately NOT in the global setupFiles so
// it does not change the offsetWidth/offsetHeight contract that every other suite observes.
//
// xyflow measures node boxes off offsetWidth/offsetHeight, which jsdom reports as 0 (no layout
// engine). LineageView sets the computed size as an inline style on every node, so we derive the
// measurement from that inline style (falling back to 1 so nothing measures as zero). The other
// xyflow shims (ResizeObserver, DOMMatrixReadOnly, SVGElement.getBBox) stay global in
// test-setup.ts: those ADD APIs jsdom lacks rather than override an existing DOM contract.
Object.defineProperties(HTMLElement.prototype, {
  offsetHeight: {
    configurable: true,
    get(this: HTMLElement) {
      return parseFloat(this.style.height) || 1
    },
  },
  offsetWidth: {
    configurable: true,
    get(this: HTMLElement) {
      return parseFloat(this.style.width) || 1
    },
  },
})
