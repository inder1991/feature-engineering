import type { EffectiveMetadataField } from '../api'
import { attestedByLabel, attributionTitle, provenanceTone, showsProposal } from './assetDetailFields'

// The one component in the field-rendering set. It lives alone so the helper module stays a plain
// module of functions: mixing components and constants in one file breaks fast refresh.
export function AuthorityBadge({ field }: { field: EffectiveMetadataField }) {
  if (showsProposal(field)) {
    return (
      <span className="badge gj-proposed" title={attributionTitle(field)}>
        {`${field.evidence_provenance ?? 'proposed'} · unconfirmed`}
      </span>
    )
  }
  const label = attestedByLabel(field)
  return (
    <span className={`badge ${provenanceTone(label)}`} title={attributionTitle(field)}>
      {label}
    </span>
  )
}
