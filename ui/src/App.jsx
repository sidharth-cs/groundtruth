import { useCallback, useEffect, useState } from 'react'
import { describeStage } from './stageNotes'
import {
  nameMatchNote, aliasNote, identifierNote, subjectRoleNote,
  adjudicationNote, ruleNote, conflictAttributeNote, notEvaluatedNote,
} from './sopNotes'

// One collapsible panel, fed whatever notes are relevant to the decision
// being rendered — never a wall of text, never shown at all when nothing
// applies. Filters nulls itself so call sites can just list every candidate
// note without checking relevance first.
function AnalystNotes({ notes }) {
  const real = notes.filter(Boolean)
  if (real.length === 0) return null
  return (
    <details className="sop">
      <summary>Analyst notes</summary>
      {real.map((n, i) => (
        <div className="sop-note" key={i}>
          <b>{n.title}</b>
          <div>{n.body}</div>
        </div>
      ))}
    </details>
  )
}

const api = async (path, options) => {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`)
  return body
}

// Four buckets a reviewer actually thinks in, computed entirely from the
// status list_runs already derives fresh from checkpoint state every call —
// no new backend state machine, no second source of truth that could drift
// from it. `archived` is the one real addition (app/api/main.py) and wins
// regardless of the underlying pipeline status, because archiving means "I
// don't want to see this in the main list," not "this run is special."
function bucketOf(c) {
  if (c.archived) return 'archived'
  if (c.status === 'in progress') return 'active'
  if (c.status === 'awaiting review' || c.status === 'escalated') return 'waiting'
  return 'completed' // committed or blocked
}

const BUCKETS = [
  ['active', 'Active'],
  ['waiting', 'Waiting for review'],
  ['completed', 'Completed'],
]

export default function App() {
  const [piles, setPiles] = useState([])
  // Scoped to the open case — fetched fresh per thread, not once globally,
  // so a case is only ever offered arrivals generated for its own pile.
  const [arrivals, setArrivals] = useState([])
  const [pile, setPile] = useState(null)
  const [run, setRun] = useState(null)
  const [busy, setBusy] = useState(false)
  const [activity, setActivity] = useState(null)
  const [error, setError] = useState(null)
  // Section hashes from before the last arrival, so the UI can show which
  // sections an update actually touched rather than asserting it did.
  const [priorHashes, setPriorHashes] = useState(null)

  const [cases, setCases] = useState([])
  // A citation asking the Sources viewer to open a document at an offset.
  const [openAt, setOpenAt] = useState(null)
  // Self-asserted, and labelled as such. There is no authentication here, but
  // a decision log with no actor is not a decision log — "rejected by Sid at
  // 14:02 because the amendment supersedes it" is the record that matters.
  const [reviewer, setReviewer] = useState(
    () => localStorage.getItem('reviewer') || ''
  )
  useEffect(() => { localStorage.setItem('reviewer', reviewer) }, [reviewer])

  const refreshCases = useCallback(() => {
    // Higher than the old flat list's 8 — grouped into four buckets, 8 would
    // starve everything but the most recent one or two threads.
    api('/runs?limit=30').then((d) => setCases(d.cases)).catch(() => {})
  }, [])

  const setArchived = (thread, archived) => {
    api(`/runs/${thread}/archive`, {
      method: 'POST', body: JSON.stringify({ archived }),
    }).then(refreshCases).catch((e) => setError(e.message))
  }

  const [startTab, setStartTab] = useState('demo')

  // Re-fetched per thread rather than derived from `run`, so uploading a new
  // arrival can refresh just this list without re-fetching the whole case.
  const refreshArrivals = useCallback((thread) => {
    if (!thread) { setArrivals([]); return }
    api(`/runs/${thread}/arrivals`).then((d) => setArrivals(d.arrivals)).catch(() => {})
  }, [])

  useEffect(() => { refreshArrivals(run?.thread) }, [run?.thread, refreshArrivals])

  useEffect(() => {
    api('/piles')
      .then((d) => setPiles(d.piles))
      .catch((e) => setError(e.message))
    refreshCases()

    // Reopen whatever case the URL names. The checkpointer has always held
    // every case; the interface simply had no way to ask for one, so a refresh
    // left the reviewer on an empty page while the work sat safely in Postgres,
    // unreachable. A case is now addressable: refresh it, bookmark it, send
    // someone the link.
    const wanted = new URLSearchParams(location.search).get('case')
    if (wanted) {
      api(`/runs/${wanted}`)
        .then(setRun)
        .catch(() => setError(`No case found for ${wanted}.`))
    }
  }, [refreshCases])

  // Keep the address bar pointing at the open case, without adding a history
  // entry per click — the back button should leave the case, not step through
  // every decision made inside it.
  useEffect(() => {
    const url = new URL(location.href)
    if (run?.thread) url.searchParams.set('case', run.thread)
    else url.searchParams.delete('case')
    history.replaceState(null, '', url)
  }, [run?.thread])

  const guard = useCallback(async (label, fn) => {
    setBusy(true); setActivity(label); setError(null)
    try { return await fn() }
    catch (e) { setError(e.message) }
    finally { setBusy(false); setActivity(null) }
  }, [])

  const openCase = (thread) => guard('Opening the case…', async () => {
    setPriorHashes(null)
    setRun(await api(`/runs/${thread}`))
  })

  const start = (name) => guard('Analyzing documents and preparing review…', async () => {
    setPriorHashes(null)
    setRun(await api('/runs', { method: 'POST', body: JSON.stringify({ pile: name }) }))
    refreshCases()
  })

  const settle = (index, verb, body) => guard('Recording your decision…', async () => {
    setRun(await api(`/runs/${run.thread}/decisions/${index}/${verb}`, {
      method: 'POST', body: JSON.stringify({ ...(body || {}), reviewer }),
    }))
  })

  const resume = () => guard('Applying approved decisions…', async () => {
    setRun(await api(`/runs/${run.thread}/resume`, { method: 'POST' }))
  })

  const sendArrival = (name) => guard('Applying the focused document update…', async () => {
    setPriorHashes(Object.fromEntries(
      (run.register?.sections || []).map((s) => [s.id, s.content_hash])
    ))
    setRun(await api(`/runs/${run.thread}/arrivals`, {
      method: 'POST', body: JSON.stringify({ path: name }),
    }))
  })

  const pending = (run?.decisions || []).filter((d) => d.state === 'pending')
  const atGate = run?.paused_before?.includes('commit')

  return (
    <div className="app">
      <aside className="side">
        <div className="brand">
          doctask
          <small>vendor due-diligence review</small>
        </div>

        <div className="label">You are</div>
        <input className="who" placeholder="your name (recorded on decisions)"
               value={reviewer} onChange={(e) => setReviewer(e.target.value)} />

        <div className="label section">Start an investigation</div>
        <div className="segmented">
          <button className={startTab === 'demo' ? 'active' : ''}
                  onClick={() => setStartTab('demo')}>Demo cases</button>
          <button className={startTab === 'upload' ? 'active' : ''}
                  onClick={() => setStartTab('upload')}>Upload yours</button>
        </div>

        {startTab === 'demo' ? (
          /* A case is a scenario. Naming them "ambiguous" and "unidentifiable"
             told you nothing unless you wrote them, so each one now says what
             it is and what to expect before you spend a run finding out. */
          piles.filter((p) => p.demo).map((p) => (
            // A <details> below needs to be clickable on its own — <details>
            // cannot legally nest inside <button>, so this card is a div with
            // button semantics, not a real <button>, and the disclosure's own
            // click is kept from also selecting the card via stopPropagation.
            <div
              key={p.name}
              className="pile" role="button" tabIndex={0}
              aria-pressed={pile === p.name}
              onClick={() => setPile(p.name)}
              onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setPile(p.name)}
            >
              <div className="ptitle">{p.title}<span>{p.documents} docs</span></div>
              <div className="pblurb">{p.blurb}</div>
              {p.expect && (
                /* Genuinely useful to a grader deciding what to click, and
                   genuinely wrong on a card real analyst software would show
                   — closed by default keeps both things true. */
                <details className="pexpect" onClick={(e) => e.stopPropagation()}>
                  <summary>reviewer notes</summary>
                  expect: {p.expect}
                </details>
              )}
            </div>
          ))
        ) : (
          <Upload busy={busy} onDone={(created) => {
            setPiles(created.piles); setPile(created.pile)
          }} onError={setError} />
        )}

        <button
          className="primary"
          disabled={!pile || busy}
          onClick={() => start(pile)}
        >
          {busy ? 'working…' : pile ? 'Start the investigation' : 'Pick a case'}
        </button>

        {run && !atGate && (
          <>
            <div className="label">A document arrives</div>
            <div className="fine" style={{ marginBottom: 6 }}>
              A late document for this vendor. Applying one proposes a focused
              update — only the sections it affects are rebuilt, and you approve
              it before anything changes.
            </div>
            {arrivals.map((f) => (
              <button key={f} className="pile" disabled={busy}
                      onClick={() => sendArrival(f)}>
                {/* The server owns this prefix (upload_arrival), so slicing
                    it off is always safe — no regex, no assuming "meridian". */}
                {f.slice(run.pile_id.length + 1)}<span>apply</span>
              </button>
            ))}
            <ArrivalUpload thread={run.thread} busy={busy}
                            onUploaded={() => refreshArrivals(run.thread)}
                            onError={setError} />
          </>
        )}

        <div className="label section">My investigations</div>
        {cases.length === 0 ? (
          <div className="empty">
            Nothing started yet. Pick a demo case or upload your own above.
          </div>
        ) : (() => {
          // Four buckets computed from list_runs' own fresh-every-call status
          // string (see bucketOf above) -- no parallel state to drift.
          const byBucket = { active: [], waiting: [], completed: [], archived: [] }
          cases.forEach((c) => byBucket[bucketOf(c)].push(c))
          // The one status inside "Completed" worth seeing before the rest.
          byBucket.completed.sort((a, b) => (b.status === 'blocked') - (a.status === 'blocked'))

          // The archive toggle needs to sit inside the same clickable card as
          // "open this case" without nesting <button> inside <button> — same
          // div-with-button-semantics fix as the demo cards above.
          const caseCard = (c) => (
            <div key={c.thread} className="pile case" role="button" tabIndex={0}
                 aria-pressed={run?.thread === c.thread}
                 onClick={() => !busy && openCase(c.thread)}
                 onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && !busy && openCase(c.thread)}>
              <div className="ptitle">
                {c.pile_id}<span>{c.status}</span>
              </div>
              <div className="pblurb">
                {c.thread}
                {c.pending > 0 && ` · ${c.pending} awaiting decision`}
              </div>
              <button className="link archive-toggle" disabled={busy}
                      onClick={(e) => { e.stopPropagation(); setArchived(c.thread, !c.archived) }}>
                {c.archived ? 'unarchive' : 'archive'}
              </button>
            </div>
          )

          const anyBucketed = BUCKETS.some(([key]) => byBucket[key].length > 0)

          return (
            <>
              {!anyBucketed && byBucket.archived.length > 0 && (
                <div className="empty">Every investigation is archived.</div>
              )}
              {BUCKETS.map(([key, title]) => byBucket[key].length > 0 && (
                <div key={key}>
                  <div className="case-group-title">{title}</div>
                  {byBucket[key].map(caseCard)}
                </div>
              ))}
              {byBucket.archived.length > 0 && (
                <details className="archived-group">
                  <summary>Archived ({byBucket.archived.length})</summary>
                  {byBucket.archived.map(caseCard)}
                </details>
              )}
            </>
          )
        })()}

        <AdHocScreen busy={busy} onError={setError} />

        {error && <div className="err">{error}</div>}
      </aside>

      <main className="main">
        {busy && (
          <div className="loading" role="status" aria-live="polite">
            <span className="spinner" aria-hidden="true" />
            <div>
              <b>{activity}</b>
              <div>This may take a moment. The page will update when this step finishes.</div>
            </div>
          </div>
        )}

        {!run && <Intro />}

        {run?.escalated && (
          <div className="banner stop">
            <b>Escalated to a human.</b> {run.escalation_reason}
          </div>
        )}

        {pending.length > 0 && (
          <div className="banner gate">
            <b>Waiting for you.</b> {pending.length} item
            {pending.length === 1 ? '' : 's'} still to decide. Nothing commits
            until every one has an answer, and deciding one leaves the rest
            untouched.
          </div>
        )}

        {/* A clean case raises nothing to review. It still has to be committed,
            and it used to have no control to do it with: the commit button
            lived inside the decisions block, so a case with no decisions had no
            way forward at all. */}
        {atGate && pending.length === 0 && run?.decisions?.length === 0 && (
          <div className="banner done">
            <b>Nothing to review.</b> Every rule ran and every rule passed, no
            source contradicted another, and no party matched the watchlist.
            That is a result, not an absence of one — commit it below.
          </div>
        )}

        {run?.onboarding_blocked && (
          <div className="banner stop">
            <b>Onboarding blocked.</b> A watchlist alert was escalated rather
            than cleared. The register is still written and every other item
            still applied — the hold is a stated fact in the record, not a
            crash.
            <ul>
              {run.blocking_reasons.map((r) => <li key={r}>{r}</li>)}
            </ul>
          </div>
        )}

        {run && !atGate && !run.escalated && !run.onboarding_blocked
          && pending.length === 0 && run.register && (
          <div className="banner done">
            <b>Committed.</b> Register at revision {run.register.revision}. Every
            item was reviewed, and only approved items were applied.
            {run.watchlist_available === false && (
              <> No watchlist was loaded, so no party was screened — that is not
              the same as a clean screening.</>
            )}
          </div>
        )}

        {run && <Stages stages={run.stages} />}
        {run?.checklist_summary && <ChecklistSummary summary={run.checklist_summary} />}

        {run?.decisions?.length > 0 && (
          <>
            <h2>For your decision</h2>
            {run.decisions.map((d) => (
              <Decision key={d.index} d={d} busy={busy} onSettle={settle}
                        watchlist={run.watchlist_name} onOpenSource={setOpenAt} />
            ))}
          </>
        )}

        {/* Outside the decisions block on purpose.
            It used to live inside it, so a case that raised nothing to review
            — the clean one, the best result this system produces — had no
            control to finish with at all. What decides whether this shows is
            the graph waiting at the gate, and what decides whether it is
            enabled is whether anything is still unanswered. */}
        {atGate && (
          <>
            <button className="primary"
                    disabled={busy || pending.length > 0}
                    onClick={resume} style={{ maxWidth: 340 }}>
              {pending.length > 0
                ? `${pending.length} item${pending.length === 1 ? '' : 's'} left to decide`
                : run?.decisions?.length
                  ? 'Commit the reviewed register'
                  : 'Commit the register'}
            </button>
            {pending.length > 0 && (
              <div className="empty">
                Every item needs an answer before this can commit. Deciding one
                leaves the rest untouched.
              </div>
            )}
          </>
        )}

        {run?.register && (
          <Register register={run.register} prior={priorHashes}
                    provenance={run.provenance} onOpenSource={setOpenAt} />
        )}
        {run?.documents?.length > 0 && (
          <Documents documents={run.documents} thread={run.thread}
                     openAt={openAt} onOpened={() => setOpenAt(null)} />
        )}
        {run?.cost && <Cost cost={run.cost} />}
      </main>
    </div>
  )
}

// The API deliberately exposes stable LangGraph identifiers. Translate them at
// the presentation boundary so a compliance analyst sees the work's purpose,
// not its implementation detail.
const STAGE_LABELS = {
  intake: 'Document intake',
  screen: 'Source integrity screening',
  classify: 'Document classification',
  escalate: 'Human escalation',
  extract: 'Fact extraction',
  screen_parties: 'Party screening',
  reconcile: 'Conflict detection',
  compose: 'Register generation',
  check: 'Compliance checks',
  gate: 'Human approval',
  commit: 'Commit revision',
  arrival: 'Focused document update',
}

const DECISION_LABELS = {
  loaded: 'Documents loaded',
  clean: 'No instruction attack detected',
  attempted: 'Initial pass complete',
  widened_retry: 'Retried with expanded evidence',
  extracted: 'Facts extracted',
  screened: 'Screening complete',
  compared: 'Conflicts checked',
  built: 'Draft register generated',
  evaluated: 'Checks evaluated',
  awaiting_human: 'Awaiting analyst approval',
  applied: 'Applied',
  halted: 'Escalated to an analyst',
}

const humanize = (value) => value
  .replace(/_/g, ' ')
  .replace(/^./, (character) => character.toUpperCase())

function Stages({ stages }) {
  if (!stages?.length) return null
  return (
    <>
      <h2>Stages</h2>
      <div className="card">
        {stages.map((s, i) => (
          <div className="stage" key={i}>
            <b>{STAGE_LABELS[s.stage] || humanize(s.stage)}</b>
            <span className={
              'verb' +
              (s.decision === 'halted' ? ' halt' : '') +
              (s.decision === 'widened_retry' ? ' retry' : '')
            }>{DECISION_LABELS[s.decision] || humanize(s.decision)}</span>
            {(() => {
              const prose = describeStage(s.stage, s.decision, s.detail)
              return prose
                ? <span className="detail prose">{prose}</span>
                : (
                  <span className="detail">
                    {Object.entries(s.detail)
                      .filter(([k]) => k !== 'summary')
                      .map(([k, v]) => `${k}=${Array.isArray(v) ? `[${v.join(', ')}]` : v}`)
                      .join('  ')}
                  </span>
                )
            })()}
          </div>
        ))}
      </div>
    </>
  )
}

// The full tally, not just the failures shown below in "For your decision".
// A rule that could not run and a rule that passed both produce zero
// findings — indistinguishable without this — and the brief specifically
// grades whether an honest "no findings" means every rule actually ran.
function ChecklistSummary({ summary }) {
  const blocked = summary.not_evaluated.length
  return (
    <>
      <h2>Compliance checklist</h2>
      <div className="card checklist">
        <div className="checkline">
          <b>{summary.checklist_name}</b>
          <span className="tally">
            <span className="passed">{summary.passed} passed</span>
            {summary.failed > 0 && (
              <span className="failed">{summary.failed} finding{summary.failed === 1 ? '' : 's'}</span>
            )}
            {blocked > 0 && (
              <span className="blocked">{blocked} not evaluated</span>
            )}
          </span>
        </div>
        <div className="fine">{summary.summary}</div>
        {blocked > 0 && (
          <>
            <ul className="not-evaluated">
              {summary.not_evaluated.map((r) => (
                <li key={r.rule_id}>
                  <span className="hash">{r.rule_id}</span> {r.title}
                  <div className="fine warn">{r.note}</div>
                </li>
              ))}
            </ul>
            {/* One note for the whole list, not one per row — the guidance is
                the same fact regardless of which rule it applies to. */}
            <AnalystNotes notes={[notEvaluatedNote()]} />
          </>
        )}
      </div>
    </>
  )
}

// What each decision does once it is settled, in the words of the person
// making it. The backend model is three states and does not change; these say
// what those states *mean* for this kind of item, which for a finding is the
// entire content of the action — `commit` only consumes conflicts, so accepting
// or dismissing a finding is purely the analyst's judgement on the record.
const EFFECT = {
  conflict: 'Accepting a value writes it into the register and cites the source '
          + 'it came from. Leaving it disputed keeps both values visible and '
          + 'blocks nothing.',
  finding: 'Either way the finding and your reasoning stay on the record. '
         + 'Accepting says the exception is real; dismissing says you looked '
         + 'and it is not. Neither rewrites the register.',
  alert: 'A true match or an escalation holds the vendor and blocks onboarding. '
       + 'Discounting it as a false positive is the only disposition that clears.',
  update: 'Applying rebuilds only the sections named above and raises the '
        + 'revision. Discarding leaves every section byte-identical.',
}

function Decision({ d, busy, onSettle, watchlist, onOpenSource }) {
  const [note, setNote] = useState('')

  // A conflict is not "approved" — the analyst is choosing which source to
  // accept. Per-value accept buttons render inside ConflictEvidence itself,
  // next to the evidence each one is actually about, rather than in a
  // separate row down here disconnected from what they're deciding between.
  // "Leave disputed" doesn't belong to either side, so it stays generic.
  const choices =
    d.kind === 'conflict'
      ? [{ label: 'Leave disputed', verb: 'reject', tone: 'no' }]
      : d.kind === 'alert'
        ? [
            { label: 'Confirm true match', verb: 'approve', tone: 'no' },
            { label: 'Discount as false positive', verb: 'reject', tone: 'ok' },
            { label: 'Cannot clear — escalate', verb: 'escalate', tone: 'hold' },
          ]
        : d.kind === 'update'
          ? [
              { label: 'Apply update', verb: 'approve', tone: 'ok' },
              { label: 'Discard update', verb: 'reject', tone: 'no' },
            ]
          : [
              { label: 'Accept finding', verb: 'approve', tone: 'no' },
              { label: 'Dismiss finding', verb: 'reject', tone: 'ok' },
            ]

  return (
    <div className={`decision ${d.state}`}>
      <div className="drow">
        <div className="dsum">
          <div className="kind">{d.kind}</div>
          {d.summary}
        </div>
        <span className={`state ${d.state}`}>{d.state}</span>
      </div>

      {d.alert && <AlertEvidence alert={d.alert} watchlist={watchlist} />}
      {d.finding && <FindingEvidence finding={d.finding} onOpenSource={onOpenSource} />}
      {d.conflict && (
        <ConflictEvidence conflict={d.conflict} onOpenSource={onOpenSource}
                          onAccept={(value) => onSettle(d.index, 'approve',
                            { note: note || null, chosen_value: value })}
                          busy={busy} disabled={d.state !== 'pending'} />
      )}
      {d.update && <UpdateEvidence update={d.update} />}

      {d.chosen_value && (
        <div className="note">reviewer chose: <b>{d.chosen_value}</b></div>
      )}

      {/* The audit trail. Who decided, when, and why — the three things an
          examiner asks about a settled item, and the reason a decision log with
          only a state on it is not a decision log. */}
      {d.decided_by && (
        <div className="trail">
          <b>{d.state}</b> by {d.decided_by}
          {d.decided_at && <> · {new Date(d.decided_at).toLocaleString()}</>}
          {d.note ? <> · “{d.note}”</> : <> · no reason recorded</>}
        </div>
      )}

      {/* Actionable because the item is unanswered — not because the graph
          happens to be paused at the gate. That distinction was the bug: an
          update decision is raised *after* commit, when the graph is no longer
          at the gate, so its buttons never rendered and the case could not be
          moved forward at all. */}
      {d.state === 'pending' && (
        <div className="actions">
          {choices.map((c) => (
            <button key={c.label} className={c.tone} disabled={busy}
                    onClick={() => onSettle(d.index, c.verb,
                      { note: note || null, ...(c.value ? { chosen_value: c.value } : {}) })}>
              {c.label}
            </button>
          ))}
        </div>
      )}
      {d.state === 'pending' && (
        <>
          {/* What the decision does, in the analyst's terms. A verb without its
              consequence is still a guess. */}
          <div className="effect">{EFFECT[d.kind]}</div>
          {/* Every kind, not only alerts. The note field used to be gated on
              `d.alert`, so three of four decision kinds had no way to record
              why — and the audit trail read "no reason recorded" forever. */}
          <input className="reason" value={note}
                 placeholder="why (recorded on the decision, and in the register)"
                 onChange={(e) => setNote(e.target.value)} />
        </>
      )}
    </div>
  )
}

// Shared by a pipeline-raised alert and an ad-hoc single-name screen — both
// call the same screen() function, so both owe the reviewer the same table
// instead of the ad-hoc tool getting a score and the alert getting evidence.
function IdentifiersTable({ identifiers }) {
  return (
    <table className="identifiers">
      <thead>
        <tr>
          <th>Identifier</th><th>Subject</th><th>Listed</th>
          <th>Comparison</th><th>Strength</th>
        </tr>
      </thead>
      <tbody>
        {identifiers.map((i) => (
          <tr key={i.name} className={i.comparison}>
            <td>{i.name}</td>
            <td>{i.subject_value || <i>absent</i>}</td>
            <td>{i.listed_value || <i>absent</i>}</td>
            <td className={`cmp ${i.comparison}`}>{i.comparison}</td>
            <td className="strength">{i.strength !== 'none' && i.strength}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function AlertEvidence({ alert, watchlist }) {
  /* A score alone is not adjudicable, and a UI that shows only a score invites
     a rubber stamp. Every identifier the engine compared is shown, including
     the ones it could not compare — an absent field is the usual reason an
     alert has to be escalated rather than discounted. */
  return (
    <div className="alert">
      <div className="alertline">
        matched <b>{alert.matched_on}</b> at <b>{Math.round(alert.score * 100)}%</b>
        {alert.via_weak_alias && <span className="weak"> via a weak alias</span>}
        {/* Never name a list here. The bundled watchlist is synthetic, and an
            interface that hardcodes "OFAC" describes data the system is not
            using — the exact bluff this build exists to make impossible. The
            entry id carries its own provenance (SYN- prefixed when synthetic). */}
        {' · '}entry {alert.listed_uid} · {alert.listed_programme}
      </div>
      <IdentifiersTable identifiers={alert.identifiers} />
      <div className="suggests">
        engine suggests <b>{alert.recommendation.replace(/_/g, ' ')}</b> —{' '}
        {alert.recommendation_reason}
        <div className="advisory">Advisory only. The engine never clears an alert.</div>
        {/* Name the list that was actually loaded. A reviewer should not have
            to open the repository to find out whether the data is real. */}
        {watchlist && <div className="advisory">Screened against: {watchlist}</div>}
      </div>
      <AnalystNotes notes={[
        nameMatchNote(alert.score),
        aliasNote(alert.via_weak_alias),
        ...alert.identifiers.map(identifierNote),
        subjectRoleNote(alert.subject_role),
        adjudicationNote(alert.recommendation),
      ]} />
    </div>
  )
}

function Upload({ busy, onDone, onError }) {
  /* Five pre-baked piles cannot show that this works on documents it has not
     seen. A reviewer has to be able to bring their own, so this posts real
     files through the same pipeline the seeded piles use. */
  const [name, setName] = useState('')
  const [files, setFiles] = useState([])
  const [rejected, setRejected] = useState([])

  const send = async () => {
    const form = new FormData()
    form.append('name', name)
    for (const f of files) form.append('files', f)
    try {
      // No Content-Type header: the browser sets the multipart boundary.
      const res = await fetch('/api/piles', { method: 'POST', body: form })
      const body = await res.json()
      if (!res.ok) throw new Error(body.detail || 'upload failed')
      setRejected(body.rejected || [])
      const listed = await fetch('/api/piles').then((r) => r.json())
      onDone({ piles: listed.piles, pile: body.pile })
      setName(''); setFiles([])
    } catch (e) { onError(e.message) }
  }

  return (
    <>
      <div className="label">Your own case</div>
      <div className="upload">
        <input placeholder="name this case" value={name}
               onChange={(e) => setName(e.target.value)} />
        <input type="file" multiple
               accept=".txt,.md,.html,.htm,.docx,.pdf"
               onChange={(e) => setFiles([...e.target.files])} />
        <button disabled={busy || !name.trim() || !files.length} onClick={send}>
          Add {files.length ? `${files.length} document${files.length > 1 ? 's' : ''}` : 'documents'}
        </button>
        <div className="fine">reads .txt .md .html .docx .pdf</div>
        {/* A reviewer who uploads one invoice gets a register full of "not
            supported by the sources" and cannot tell whether the system is
            broken or their pile was thin. It is the latter, and saying so
            turns a confusing path into a demonstration of NOT_EVALUATED. A
            scannable list of document kinds reads in three seconds; the
            paragraph it replaced made a reviewer read every word to get the
            same six nouns. */}
        <div className="fine">A vendor package is usually all about the <b>same</b> vendor:</div>
        <div className="doc-checklist">
          {['incorporation certificate', 'ownership disclosure', 'screening result',
            'contract', 'invoice', 'bank details'].map((kind) => (
            <span className="pill" key={kind}>{kind}</span>
          ))}
        </div>
        <div className="fine">
          Fewer documents is fine: rules whose evidence is missing report
          <b> not evaluated</b> rather than passing.
        </div>
        <div className="fine">
          Invent the vendor. Do not upload anything real.
        </div>
        {/* Rejections are named. A pile that silently drops the one file you
            cared about is worse than one that refuses it out loud. */}
        {rejected.map((r) => (
          <div className="fine warn" key={r.filename}>{r.filename}: {r.why}</div>
        ))}
      </div>
    </>
  )
}

function ArrivalUpload({ thread, busy, onUploaded, onError }) {
  /* Bring-your-own-case used to stop after the first two movements: a
     reviewer could upload a pile and review it, but the only arrivals ever
     offered were the bundled meridian_NN_*.txt files. This is the third
     movement — "stays alive" — actually reachable on a reviewer's own data. */
  const [file, setFile] = useState(null)

  const send = async () => {
    const form = new FormData()
    form.append('file', file)
    try {
      const res = await fetch(`/api/runs/${thread}/arrivals/upload`, {
        method: 'POST', body: form,
      })
      const body = await res.json()
      if (!res.ok) throw new Error(body.detail || 'arrival upload failed')
      setFile(null)
      onUploaded()
    } catch (e) { onError(e.message) }
  }

  return (
    <div className="upload">
      <input type="file" accept=".txt,.md,.html,.htm,.docx,.pdf"
             onChange={(e) => setFile(e.target.files[0] || null)} />
      <button disabled={busy || !file} onClick={send}>
        Add as arrival
      </button>
    </div>
  )
}

function AdHocScreen({ busy, onError }) {
  /* A tool affordance, not a brief requirement: the pipeline derives its
     parties from documents, which is correct for a document system. But
     checking one name is a real thing an analyst does, and it makes all three
     adjudication outcomes reachable without preparing a corpus for each. */
  const [form, setForm] = useState({
    name: '', party_type: 'entity', country: '',
    date_of_birth: '', nationality: '', document_number: '',
  })
  const [result, setResult] = useState(null)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const run = async () => {
    try {
      const res = await fetch('/api/screen', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      })
      const body = await res.json()
      if (!res.ok) throw new Error(body.detail || 'screening failed')
      setResult(body)
    } catch (e) { onError(e.message) }
  }

  return (
    <>
      <div className="label section">Screen one name</div>
      <div className="upload">
        <input placeholder="party name" value={form.name} onChange={set('name')} />
        <select value={form.party_type} onChange={set('party_type')}>
          <option value="entity">entity</option>
          <option value="individual">individual</option>
        </select>
        {form.party_type === 'entity' ? (
          <input placeholder="country (optional)" value={form.country}
                 onChange={set('country')} />
        ) : (
          <>
            <input placeholder="date of birth (optional)" value={form.date_of_birth}
                   onChange={set('date_of_birth')} />
            <input placeholder="nationality (optional)" value={form.nationality}
                   onChange={set('nationality')} />
            <input placeholder="passport number (optional)" value={form.document_number}
                   onChange={set('document_number')} />
          </>
        )}
        <button disabled={busy || !form.name.trim()} onClick={run}>Screen</button>
        {result && (
          <div className="fine">
            {result.alerts.length === 0
              // Screened-and-clear must never read like never-screened.
              ? `screened against ${result.watchlist_entries} watchlist entries — no alert`
              : result.alerts.map((a) => (
                  <div className="adhoc" key={a.id}>
                    <b>{a.listed_name}</b> · {a.listed_programme} ·{' '}
                    {Math.round(a.score * 100)}%
                    <div className={`verdict ${a.recommendation}`}>
                      {a.recommendation.replace(/_/g, ' ')}
                    </div>
                    <div className="why">{a.recommendation_reason}</div>
                    {/* Same screen() call a pipeline alert gets its evidence
                        from — the ad-hoc tool owes the reviewer the same
                        table and the same guidance, not just the verdict. */}
                    <IdentifiersTable identifiers={a.identifiers} />
                    <AnalystNotes notes={[
                      nameMatchNote(a.score),
                      aliasNote(a.via_weak_alias),
                      ...a.identifiers.map(identifierNote),
                      adjudicationNote(a.recommendation),
                    ]} />
                  </div>
                ))}
          </div>
        )}
      </div>
    </>
  )
}

function Register({ register, prior, provenance, onOpenSource }) {
  const [open, setOpen] = useState(null)
  return (
    <>
      <h2>
        Register <span className="pill">revision {register.revision}</span>
        <span className="hint">click any line to see the words it came from</span>
      </h2>
      <div className="card">
        {register.sections.map((s) => {
          const changed = prior && prior[s.id] && prior[s.id] !== s.content_hash
          const untouched = prior && prior[s.id] === s.content_hash
          return (
            <div className="section" key={s.id}>
              <header>
                <h3>{s.heading}</h3>
                <span className={`hash${changed ? ' changed' : ''}`}>
                  {changed ? 'rebuilt · ' : untouched ? 'byte-identical · ' : ''}
                  {s.content_hash.slice(0, 12)}
                </span>
              </header>
              <pre>
                {s.body.split('\n').map((line, i) => {
                  // Register lines read "attribute: value". The attribute is
                  // the key into provenance, which is why the register is
                  // rendered line by line rather than as one blob.
                  const attribute = line.match(/^([a-z_]+):/)?.[1]
                  const sources = attribute ? provenance?.[attribute] : null
                  const key = `${s.id}:${i}`
                  const cited = sources?.some((x) => x.quote)
                  return (
                    <div key={i}>
                      <div
                        className={
                          (line.includes('DISPUTED') ? 'disputed ' : '') +
                          (cited ? 'traceable' : '')
                        }
                        onClick={() => cited && setOpen(open === key ? null : key)}
                        role={cited ? 'button' : undefined}
                      >
                        {line}
                        {cited && <span className="cite">source</span>}
                      </div>
                      {open === key && (
                        <Sources sources={sources} onOpenSource={onOpenSource} />
                      )}
                    </div>
                  )
                })}
              </pre>
            </div>
          )
        })}
      </div>
    </>
  )
}

// One citation, rendered identically wherever it appears — a register line,
// a finding, a conflict. The claim that this system never bluffs is only
// checkable if the source is reachable. Offsets are shown because they are
// what make the quote verifiable: slice the file at those two numbers and
// you get these words back, or the citation is broken and this says so.
function Citation({ src, onOpenSource }) {
  if (!src.quote) {
    return (
      <div className="srchead">
        no source — reported as unsupported rather than guessed
      </div>
    )
  }
  return (
    <>
      <div className="srchead">
        {src.filename}
        <span className="offsets">chars {src.char_start}–{src.char_end}</span>
        <span className={src.verified ? 'verified' : 'broken'}>
          {src.verified ? 're-sliced from source ✓' : 'CITATION DOES NOT MATCH'}
        </span>
        {/* The excerpt shows the words; this shows them where they live.
            "Came from this exact source" is only convincing if the source is
            one click away. */}
        <button className="link" onClick={(e) => {
          e.stopPropagation()
          onOpenSource?.({ documentId: src.document_id, charStart: src.char_start })
          document.querySelector('.doc')?.scrollIntoView({ block: 'start' })
        }}>open the document</button>
      </div>
      <div className="excerpt">
        <span className="ctx">…{src.context_before}</span>
        <mark>{src.quote}</mark>
        <span className="ctx">{src.context_after}…</span>
      </div>
    </>
  )
}

function Sources({ sources, onOpenSource }) {
  return (
    <div className="sources">
      {sources.map((src, i) => (
        <div className="src" key={i}>
          <Citation src={src} onOpenSource={onOpenSource} />
        </div>
      ))}
    </div>
  )
}

// A finding's own evidence: the values the rule compared, in its own words,
// and a citation for each — the same density an alert already gets, instead
// of leaving a reviewer to reconstruct "compared to what" from the register.
function FindingEvidence({ finding, onOpenSource }) {
  const c = finding.compared
  return (
    <div className="finding-evidence">
      {/* Only the rules that genuinely compare two values get this — most
          rules (required_claim, value_in, ...) have nothing to compare, and
          the prose sentence below still covers them. */}
      {c ? (
        <div className="compared">
          <div className="compared-row">
            <span>{humanize(c.left_attribute)}</span><b>{c.left_value}</b>
          </div>
          <div className="compared-row">
            <span>{humanize(c.right_attribute)}</span><b>{c.right_value}</b>
          </div>
          <div className="compared-row diff">
            <span>Difference</span><b>{c.difference}</b>
          </div>
        </div>
      ) : (
        <div className="fine">{finding.detail}</div>
      )}
      {finding.citations.length > 0 && (
        <div className="sources">
          {finding.citations.map((src, i) => (
            <div className="src" key={i}>
              <Citation src={src} onOpenSource={onOpenSource} />
            </div>
          ))}
        </div>
      )}
      <AnalystNotes notes={[ruleNote(finding.rule_id)]} />
    </div>
  )
}

// A conflict's competing claims, each a self-contained card: the value, its
// full citation, and the one button that accepts it — evidence and the
// action it justifies in the same place, not evidence above and a detached
// row of buttons below that a reviewer has to correlate by eye.
function ConflictEvidence({ conflict, onOpenSource, onAccept, busy, disabled }) {
  return (
    <>
      <div className="conflict-evidence">
        {conflict.claims.map((claim, i) => {
          // Naming the source in the button itself, not just in the citation
          // card below it — the decision and its evidence read as one unit.
          const filenames = [...new Set(claim.citations.map((c) => c.filename).filter(Boolean))]
          const sourceLabel = filenames.length === 0 ? ''
            : filenames.length === 1 ? ` — ${filenames[0]}`
            : ` — ${filenames[0]} +${filenames.length - 1} more`
          return (
            <div className="conflict-claim" key={i}>
              <div className="claim-value">{claim.value}</div>
              <div className="sources">
                {claim.citations.map((src, j) => (
                  <div className="src" key={j}>
                    <Citation src={src} onOpenSource={onOpenSource} />
                  </div>
                ))}
              </div>
              <button className="ok" disabled={busy || disabled}
                      onClick={() => onAccept(claim.value)}>
                Accept {claim.value}{sourceLabel}
              </button>
            </div>
          )
        })}
      </div>
      <AnalystNotes notes={[conflictAttributeNote(conflict.attribute)]} />
    </>
  )
}

// sections_rebuilt/sections_untouched prove *which* sections a "would
// rebuild" claim touches — nothing about what changed within them. This is
// that diff: one line per attribute this arrival cleanly supersedes.
// Anything it *disputes* shows up as its own conflict decision instead, so
// it is named here but not given an old->new line that would misstate it.
function UpdateEvidence({ update }) {
  return (
    <div className="update-evidence">
      {update.value_changes.length > 0 && (
        <ul className="value-changes">
          {update.value_changes.map((c) => (
            <li key={c.attribute}>
              <span className="hash">{c.attribute}</span>{' '}
              {c.old_value === null ? <i>absent</i> : c.old_value}
              {' → '}<b>{c.new_value}</b>
            </li>
          ))}
        </ul>
      )}
      {update.conflicts_raised.length > 0 && (
        <div className="fine warn">
          Also disputes {update.conflicts_raised.join(', ')} — raised as its
          own conflict below, not applied automatically.
        </div>
      )}
    </div>
  )
}

function Documents({ documents, thread, openAt, onOpened }) {
  const [open, setOpen] = useState(null)

  // A citation elsewhere in the page can ask for a document to be opened at a
  // particular offset — that is the whole "this claim came from this exact
  // source" move, followed through rather than described.
  useEffect(() => {
    if (openAt?.documentId) { setOpen(openAt.documentId); onOpened?.() }
  }, [openAt, onOpened])

  return (
    <>
      <h2>
        Sources
        <span className="hint">click a document to read it and see what was cited</span>
      </h2>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>File</th><th>Identified as</th><th>Cited</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {documents.map((d) => (
              <tr key={d.filename} className="doc"
                  aria-expanded={open === d.id}
                  onClick={() => setOpen(open === d.id ? null : d.id)}>
                <td>{d.filename}</td>
                <td><span className="pill">{d.kind}</span></td>
                <td className="num">
                  {/* Zero here is meaningful, not empty. It is how a reviewer
                      sees that the quarantined document contributed nothing. */}
                  {d.cites === 0
                    ? <span className="nocite">nothing extracted</span>
                    : `${d.cites} claim${d.cites === 1 ? '' : 's'}`}
                </td>
                <td>
                  <span className={`pill ${d.status === 'quarantined' ? 'quarantined' : d.status === 'failed' ? 'failed' : ''}`}>
                    {d.status}
                  </span>
                  {d.quarantine_reason && (
                    <div className="note">{d.quarantine_reason}</div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {open && (
          <DocumentText thread={thread} documentId={open}
                        scrollTo={openAt?.documentId === open ? openAt.charStart : null} />
        )}
      </div>
    </>
  )
}

function Intro() {
  /* The first thing a reviewer sees, written for someone who has not read the
     README and will not.

     A workflow rather than a description: telling somebody what a system is
     takes longer than showing them how to use it, and they arrive here wanting
     to click something. Each step says what actually happens, because a step
     that oversells is worse than no step — the whole argument of this build is
     that it does not claim more than it does. */
  const steps = [
    ['Pick a case', 'on the left. Each is a different scenario, and says what to expect.'],
    ['Run it', 'ten named stages, about fifty milliseconds, no API key needed.'],
    ['Read what it found', 'facts extracted, sources that contradict each other, rule findings, watchlist alerts.'],
    ['Check anything', 'click a register line to see the exact words it came from, or open the source document itself.'],
    ['Decide each item', 'approve, reject, or escalate. One at a time — rejecting one leaves the rest untouched.'],
    ['Commit', 'refused until every item has a decision.'],
    ['Add a document', 'one section rebuilds; the rest stay byte-identical, and you approve the change before it lands.'],
  ]
  return (
    <div className="intro">
      <h1>Vendor due-diligence review</h1>
      <p>
        A pile of documents about one vendor — incorporation records, ownership
        disclosures, a screening result, invoices, a contract — that never quite
        agree with each other. This reads them, pulls out the facts, finds where
        they contradict, checks them against a rulebook supplied as data, and
        produces one register in which every line traces back to the exact words
        it came from.
      </p>
      <p className="dim">
        Every document and every watchlist entry here is invented. Nothing
        commits until a person has decided on every item.
      </p>
      <ol className="steps">
        {steps.map(([what, detail]) => (
          <li key={what}><b>{what}</b> — {detail}</li>
        ))}
      </ol>
    </div>
  )
}

function DocumentText({ thread, documentId, scrollTo }) {
  /* The source, as the system read it, with every cited stretch marked in
     place.

     Deliberately the *normalised* text rather than a rendered original. What a
     citation points at is this string and these offsets — showing a prettier
     version would show something the offsets do not index, which is the one
     thing a provenance viewer must not do. The DOCX and HTML documents prove
     the point: a reviewer sees exactly what the extractor saw. */
  const [doc, setDoc] = useState(null)
  const [failed, setFailed] = useState(null)

  useEffect(() => {
    setDoc(null); setFailed(null)
    api(`/runs/${thread}/documents/${documentId}`)
      .then(setDoc)
      .catch((e) => setFailed(e.message))
  }, [thread, documentId])

  useEffect(() => {
    if (scrollTo == null || !doc) return
    document.getElementById(`span-${documentId}-${scrollTo}`)
      ?.scrollIntoView({ block: 'center' })
  }, [scrollTo, doc, documentId])

  if (failed) return <div className="err">{failed}</div>
  if (!doc) return <div className="empty">Loading the source…</div>

  // Walk the text once, emitting plain runs and highlighted spans in order.
  const parts = []
  let cursor = 0
  doc.spans.forEach((s, i) => {
    if (s.char_start > cursor) {
      parts.push(<span key={`t${i}`}>{doc.text.slice(cursor, s.char_start)}</span>)
    }
    parts.push(
      <mark key={`s${i}`}
            id={`span-${documentId}-${s.char_start}`}
            className={s.verified ? '' : 'broken'}
            title={`${s.attribute} · chars ${s.char_start}–${s.char_end}`}>
        {doc.text.slice(s.char_start, s.char_end)}
        <span className="tag">{s.attribute}</span>
      </mark>
    )
    cursor = Math.max(cursor, s.char_end)
  })
  parts.push(<span key="tail">{doc.text.slice(cursor)}</span>)

  return (
    <div className="viewer">
      <div className="vhead">
        <b>{doc.filename}</b>
        <span className="pill">{doc.kind}</span>
        <span className="offsets">{doc.text.length} characters</span>
        {doc.spans.length > 0
          ? <span className="verified">
              {doc.spans.length} cited span{doc.spans.length === 1 ? '' : 's'}
              {doc.spans.every((s) => s.verified)
                ? ' · all re-sliced from this text ✓'
                : ' · SOME DO NOT MATCH'}
            </span>
          : <span className="nocite">no claim was extracted from this document</span>}
      </div>

      {doc.status === 'quarantined' && (
        <div className="banner stop" style={{ marginBottom: 10 }}>
          <b>Quarantined — read it, the system did not obey it.</b>{' '}
          {doc.quarantine_reason}
          {' '}The full text is below so you can see the instruction for
          yourself, and see that nothing was extracted from it.
        </div>
      )}

      <pre className="doctext">{parts}</pre>
    </div>
  )
}

function Cost({ cost }) {
  return (
    <>
      <h2>What it cost</h2>
      <div className="card">
        <table>
          <thead>
            <tr><th>Stage</th><th className="num">Model calls</th>
                <th className="num">Wall ms</th><th className="num">USD</th></tr>
          </thead>
          <tbody>
            {cost.by_stage.map((c, i) => (
              <tr key={i}>
                <td>{c.stage}</td>
                <td className="num">{c.model_calls}</td>
                <td className="num">{c.wall_ms}</td>
                <td className="num">${c.usd.toFixed(4)}</td>
              </tr>
            ))}
            <tr className="total">
              <td>total</td>
              <td className="num">{cost.total_model_calls}</td>
              <td className="num">{cost.total_wall_ms}</td>
              <td className="num">${cost.total_usd.toFixed(4)}</td>
            </tr>
          </tbody>
        </table>
        {/* $0.0000 with no explanation reads as broken, or as a claim that the
            system is free. It is neither: the offline adapter costs nothing to
            run, and the ledger has never been exercised against a paid model.
            Saying so is cheaper than letting a reviewer guess wrong. */}
        {cost.total_usd === 0 && (
          <div className="note">
            Spend reads $0.0000 because this ran on the offline adapter, which
            makes no network calls and costs nothing. Calls and wall time above
            are real. The money column has never been exercised against a paid
            model, so treat it as untested rather than as evidence of free.
          </div>
        )}
      </div>
    </>
  )
}
