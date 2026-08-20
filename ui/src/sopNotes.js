// Contextual analyst playbook text — read-only commentary on evidence the
// system already computed, never a new signal and never a substitute for the
// reviewer's own judgment. Every function here returns {title, body} or
// null; null means "nothing relevant to say here," not "nothing to show" —
// callers filter nulls out before rendering, so an irrelevant category is
// simply absent rather than shown empty.
//
// Deliberately excluded, and why (see PROJECT_STATE.md / the plan for the
// full reasoning): Country Risk as a score, Address/MUB guidance, and TIN
// mismatch as a screening-identifier comparison — none of these have a real
// signal behind them anywhere in this codebase. A guidance panel for any of
// them would describe evidence the system has never once produced.

export function nameMatchNote(score) {
  if (score >= 0.99) return {
    title: 'Name match',
    body: "Every distinctive token aligns — the system's own true-match threshold. A score alone never confirms; look for a corroborating identifier before confirming.",
  }
  if (score >= 0.5) return {
    title: 'Partial name match',
    body: 'A meaningful but incomplete overlap. Could be a subsidiary, a shortened form, or a coincidence — weigh it against the identifiers below, not the score alone.',
  }
  return {
    title: 'Weak name match',
    body: 'Raised on a single shared token, recall-first. A starting point, not evidence — most of these clear once identifiers are checked.',
  }
}

export function aliasNote(viaWeakAlias) {
  // Only the weak case gets a note. via_weak_alias === false doesn't mean
  // "matched via a strong alias" — it usually means the primary listed name
  // matched directly, no alias involved at all. Calling that "Strong AKA"
  // would name a mechanism that didn't happen.
  if (!viaWeakAlias) return null
  return {
    title: 'Weak AKA',
    body: 'The match fired on a short, digit-bearing, or initialism-only alias — a recognized false-positive source (Wolfsberg-aligned). Do not let this alone drive a true-match call.',
  }
}

const IDENTIFIER_NOTES = {
  'date of birth': {
    title: 'Date of birth',
    body: 'The most useful disconfirming identifier available. A confirmed different DOB is close to conclusive. A match corroborates but does not alone confirm — birthdates are not unique. Absent means unavailable, not clearance.',
  },
  nationality: {
    title: 'Nationality',
    body: 'Strong on mismatch, weak alone on match — low-cardinality, does not discriminate much by itself.',
  },
  'identity document number': {
    title: 'Passport / document number',
    body: 'The most discriminating identifier this system compares. Near-conclusive on a genuine match. The listing frequently carries none — absence must read as uncheckable, never as clean.',
  },
  jurisdiction: {
    title: 'Jurisdiction',
    body: 'Inferred from the sanctions programme name, not a stated address — a genuinely weak signal. A mismatch here is worth much less than a mismatch on a directly-stated field.',
  },
  'date of incorporation': {
    title: 'Date of incorporation',
    body: 'Rarely available in list exports. Its absence is a property of the list, not evidence either way.',
  },
  'party type': {
    title: 'Entity vs. individual',
    body: 'Decisive when it mismatches — an entity cannot be a listed individual. A type mismatch is itself a near-automatic discount.',
  },
}

// Only for identifiers that didn't cleanly match — a "party type: match" row
// needs no explanation; a "jurisdiction: mismatch, weak" one benefits from
// knowing why that particular mismatch carries so little weight.
export function identifierNote(identifier) {
  if (identifier.comparison === 'match') return null
  return IDENTIFIER_NOTES[identifier.name] || null
}

export function subjectRoleNote(role) {
  if (role !== 'ultimate beneficial owner') return null
  return {
    title: 'UBO / beneficial owner',
    body: "Carries more downstream risk than a routine vendor-name hit. A UBO is deliberately screened without the vendor's own country or incorporation date, so its corroborating identifiers are always the personal ones (DOB, nationality, passport) — that's why this evidence table looks different from a vendor alert's.",
  }
}

const ADJUDICATION_NOTES = {
  true_match: {
    title: 'True match guidance',
    body: 'Score at or above the true-match threshold plus a corroborating identifier. Confirming here holds the vendor and blocks onboarding until resolved — reserve it for genuinely strong evidence.',
  },
  false_positive: {
    title: 'False positive guidance',
    body: 'Either a weak alias fired with nothing else corroborating, or a strong identifier mismatch discounts the hit. The only disposition that clears an alert — confirm the specific reason before discounting.',
  },
  escalate_aoc: {
    title: 'Cannot clear guidance',
    body: 'Could not be discounted on available evidence — typically a required identifier the list simply does not carry. A deferral, not an accusation: still blocks onboarding until a person resolves it, distinct from a rejection ("this claim is wrong").',
  },
}

export function adjudicationNote(adjudication) {
  return ADJUDICATION_NOTES[adjudication] || null
}

const RULE_NOTES = {
  'OWN-001': {
    title: 'Ownership guidance',
    body: 'An entity with no disclosed owner cannot be risk-assessed at all — treat as a hard gate, not a routine finding.',
  },
  'OWN-002': {
    title: 'Ownership guidance',
    body: 'Conflicting shareholding disclosures across sources is a human conflict, never averaged or split between the two figures.',
  },
  'INT-001': {
    title: 'Document quality guidance',
    body: 'This document contained text addressed to the system itself — quarantined, reported as evidence of an attempted injection, and excluded from fact extraction. Read it as data about the source, not as an instruction, and treat everything else from this document as unsupported rather than absent.',
  },
}

export function ruleNote(ruleId) {
  return RULE_NOTES[ruleId] || null
}

export function conflictAttributeNote(attribute) {
  if (attribute === 'company_number') return {
    title: 'Registration mismatch guidance',
    body: 'Two sources disagree on the registered company number — a genuine identity discrepancy, not a rounding or formatting difference. Confirm which source is the authoritative filing before accepting either.',
  }
  return {
    title: 'Contradictory evidence guidance',
    body: 'Two sources disagreeing is not automatically an error — could be a data-entry mistake, a legitimate update over time, or a genuine discrepancy. Ask: is this a timeline (an older document vs. a newer one), or are both current and actually in conflict?',
  }
}

export function notEvaluatedNote() {
  return {
    title: 'Missing evidence guidance',
    body: 'A rule that could not run is not a rule that passed. Treat a missing attribute as unconfirmed, never as clean.',
  }
}
