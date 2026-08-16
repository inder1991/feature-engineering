// What varies across the subjects of ONE candidate group.
//
// A group is a cross-product of the same two facts, so its subjects are near-identical by
// construction. Rendering each one in full makes the reviewer diff 96-character strings by eye;
// what they actually have to read is the part that differs. This splits a group's subjects into
// the shared head, the shared tail, and the per-candidate middle, so the card can state the shared
// halves ONCE and the table can carry only the choice.
//
// The cut is aligned to a token boundary rather than to the raw character-level match: the two
// branch candidates on the live catalogs share `cust_pr` before diverging at `pref` / `prim`, and
// showing a reviewer "ef" against "im" isolates the difference at the cost of the word it belongs
// to. Backing off to the last separator keeps the varying slice a thing you can say out loud.

const BOUNDARY = new Set(['.', '_', ' ', '-', '<', '>', ':', '/'])

export interface VaryingParts {
  /** Shared head of every subject, ending on a token boundary. */
  prefix: string
  /** Shared tail of every subject, beginning on a token boundary. */
  suffix: string
  /** The per-subject remainder, in the order the subjects were given. */
  middles: string[]
}

function commonPrefixLength(values: string[]): number {
  const first = values[0]
  let i = 0
  while (i < first.length && values.every(value => value[i] === first[i])) i += 1
  return i
}

// Back a raw character-level match off to the last boundary inside it, so the varying slice starts
// at a token rather than mid-word. Returns 0 when the match contains no boundary at all.
function toBoundary(value: string, length: number): number {
  for (let i = length - 1; i >= 0; i -= 1) {
    if (BOUNDARY.has(value[i])) return i + 1
  }
  return 0
}

export function varyingParts(subjects: string[]): VaryingParts {
  if (subjects.length === 0) return { prefix: '', suffix: '', middles: [] }

  const first = subjects[0]
  // Nothing varies (the same subject proposed under two entities, or a group of one): stripping a
  // shared head off every row would leave the table blank. Say the whole thing instead.
  const matched = commonPrefixLength(subjects)
  if (matched === first.length && subjects.every(subject => subject.length === first.length)) {
    return { prefix: '', suffix: '', middles: [...subjects] }
  }
  const prefixLength = toBoundary(first, matched)
  const prefix = first.slice(0, prefixLength)

  // The tails are matched over what is LEFT after the prefix, so a short subject can never have
  // its prefix and suffix overlap into a middle that reads backwards.
  const tails = subjects.map(subject => subject.slice(prefixLength))
  const reversed = tails.map(tail => [...tail].reverse().join(''))
  const rawSuffix = commonPrefixLength(reversed)
  const shortest = Math.min(...tails.map(tail => tail.length))
  const suffixLength = toBoundary(reversed[0], Math.min(rawSuffix, shortest))
  const suffix = suffixLength === 0 ? '' : tails[0].slice(tails[0].length - suffixLength)

  return {
    prefix,
    suffix,
    middles: tails.map(tail => tail.slice(0, tail.length - suffixLength)),
  }
}
