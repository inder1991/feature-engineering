import { describe, expect, it } from 'vitest'
import { varyingParts } from './candidateDiff'

// The candidate group is a CHOICE, and a choice needs the things chosen to be comparable. The
// subjects in one group are near-identical by construction (they are a cross-product of the same
// two facts), so what a reviewer must read is the part that VARIES — everything else is shared
// context that belongs on the card, stated once.
describe('varyingParts — what actually differs across a candidate group', () => {
  it('isolates the one token that differs between two otherwise identical subjects', () => {
    const parts = varyingParts([
      'cib.bo_cib_customer.cust_pref_branch_cd <-> ftr.tran_dly.tran_branch_sol_id',
      'cib.bo_cib_customer.cust_prim_branch_cd <-> ftr.tran_dly.tran_branch_sol_id',
    ])

    expect(parts.prefix).toBe('cib.bo_cib_customer.cust_')
    expect(parts.suffix).toBe('_branch_cd <-> ftr.tran_dly.tran_branch_sol_id')
    expect(parts.middles).toEqual(['pref', 'prim'])
  })

  it('falls back to the whole subject when the subjects do not actually differ', () => {
    // Two fact_keys can carry the same subject under different proposed entities — the live
    // catalogs do exactly this, proposing cust_swift_cd <-> sender_bic as both a bank and a
    // counterparty. Splitting on "what varies" would leave every cell blank, which reads as a
    // rendering fault. Nothing varies, so nothing is stripped.
    const same = 'cib.bo_cib_customer.cust_swift_cd <-> ftr.tran_dly.sender_bic'
    const parts = varyingParts([same, same])

    expect(parts.prefix).toBe('')
    expect(parts.suffix).toBe('')
    expect(parts.middles).toEqual([same, same])
  })
})
