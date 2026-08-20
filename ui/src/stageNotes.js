// Turns a stage's raw event `detail` into a sentence an investigator would
// actually read, instead of the `key=value  key=value` dump the Stages
// timeline used to show unconditionally. Every (stage, decision) pair the
// graph actually emits (app/pipeline/graph.py, plus arrivals in gate.py) is
// covered below. Anything not covered returns null, and the caller falls
// back to the raw dump — nothing that was visible before becomes invisible,
// known stages just stop reading like a debug log.

function list(values) {
  if (!values || values.length === 0) return 'none'
  return values.join(', ')
}

const DESCRIBERS = {
  intake: {
    loaded: (d) => `Loaded ${d.documents} document${d.documents === 1 ? '' : 's'} into ${d.chunks} addressable chunk${d.chunks === 1 ? '' : 's'}.`,
  },
  screen: {
    quarantined: (d) => `Quarantined ${d.quarantined.length} document(s) for containing instructions aimed at the system: ${list(d.quarantined)}.`,
    clean: () => 'No document tried to give the system instructions.',
  },
  classify: {
    attempted: (d) => d.unknown === 0
      ? `Classified all ${d.considered} document(s) confidently on the first pass.`
      : `Classified ${d.considered - d.unknown} of ${d.considered} document(s) confidently; ${d.unknown} could not be identified.`,
    widened_retry: (d) => `First pass couldn't confidently classify ${d.unknown} of ${d.considered} document(s); retrying with a wider strategy that also reads the filename.`,
  },
  escalate: {
    halted: (d) => `Both classification passes failed on ${d.unknown} document(s); escalating to a human rather than extracting facts against labels nobody trusts.`,
  },
  extract: {
    extracted: (d) => `Extracted ${d.claims} claim(s).`
      + (d.chars_not_read ? ` ${d.chars_not_read} character(s) were past the per-call limit and left unread.` : ''),
  },
  screen_parties: {
    unavailable: (d) => `Party screening skipped: ${d.reason}.`,
    screened: (d) => `Screened ${list(d.subjects)} against ${d.watchlist_entries} watchlist entries; raised ${d.alerts} alert(s).`,
  },
  reconcile: {
    compared: (d) => d.conflicts.length === 0
      ? 'No sources disagreed with each other.'
      : `Sources disagreed on: ${list(d.conflicts)}.`,
  },
  compose: {
    built: (d) => `Built the register in ${d.sections.length} section(s): ${list(d.sections)}.`,
  },
  check: {
    // The engine already writes a correct, un-overstated sentence for this —
    // reuse it verbatim rather than re-deriving one from findings/not_evaluated.
    evaluated: (d) => d.summary || null,
  },
  gate: {
    awaiting_human: (d) => `Interrupted for human review: ${d.pending} item(s) need a decision before anything downstream runs.`,
  },
  commit: {
    applied: (d) => `Applied ${d.approved} approved item(s), discarded ${d.rejected} rejected item(s)`
      + (d.escalated > 0 ? `, ${d.escalated} left escalated` : '')
      + (d.conflicts_resolved ? `, resolved ${d.conflicts_resolved} conflict(s)` : '')
      + '.'
      + (d.onboarding_blocked ? ' Onboarding is blocked pending the escalation above.' : ''),
  },
  arrival: {
    proposed: (d) => `${d.because_of} would rebuild: ${list(d.would_rebuild)}. Awaiting human approval before anything changes.`,
    applied: (d) => `${d.because_of} applied — rebuilt ${list(d.sections_rebuilt)}; `
      + `${d.sections_untouched.length} section(s) stayed byte-identical.`,
    rejected: (d) => `${d.because_of} discarded — the register is unchanged.`,
  },
}

export function describeStage(stage, decision, detail) {
  try {
    const sentence = DESCRIBERS[stage]?.[decision]?.(detail || {})
    return sentence || null
  } catch {
    // A shape this doesn't expect must fall back to the raw dump, not crash
    // the whole timeline over one unfamiliar event.
    return null
  }
}
