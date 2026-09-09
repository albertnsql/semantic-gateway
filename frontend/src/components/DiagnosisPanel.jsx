/**
 * components/DiagnosisPanel.jsx — renders a `status: "diagnosis"` response.
 *
 * Shared by the dashboard chat (ChatMessage) and the standalone query page
 * (QueryResultPanel) so the two cannot drift, which they have before.
 *
 * The design carries two rules from the diagnostic architecture that matter more
 * than the layout:
 *
 * 1. CONTRIBUTION AND ASSOCIATION MUST NOT LOOK ALIKE. A contribution is
 *    arithmetic — this dimension accounts for N% of the gap, and it is checkable.
 *    An association is not: the numbers move together and nothing establishes why.
 *    Rendering them identically is how a correlation gets read as a cause, so
 *    association is visually demoted and labelled.
 *
 * 2. EVERY CAUSAL CLAIM SHOWS ITS EVIDENCE. Citations like [F3] are rendered as
 *    chips and the evidence table lists every probe with its id, so a reader can
 *    check the claim instead of trusting it. That is the whole reason the
 *    diagnostic path is allowed to state a cause when the narrative path is not.
 *
 * Ruled-out hypotheses are shown deliberately. "I checked payment method and it is
 * not that" is often the most useful line in the answer.
 */
import { useState } from 'react';
import {
  Activity, AlertTriangle, CheckCircle2, ChevronDown, ChevronRight,
  HelpCircle, Info, Minus, Search, XCircle,
} from 'lucide-react';

const VERDICTS = {
  explains: {
    label: 'Explains',
    tone: '#059669',
    bg: 'rgba(5,150,105,0.08)',
    Icon: CheckCircle2,
    hint: 'accounts for most of the gap',
  },
  partial: {
    label: 'Partial',
    tone: '#0D9488',
    bg: 'rgba(13,148,136,0.08)',
    Icon: Activity,
    hint: 'accounts for some of the gap',
  },
  not_it: {
    label: 'Ruled out',
    tone: '#64748B',
    bg: 'rgba(100,116,139,0.08)',
    Icon: Minus,
    hint: 'checked, and it does not explain the gap',
  },
  inconclusive: {
    label: 'Inconclusive',
    tone: '#B45309',
    bg: 'rgba(180,83,9,0.08)',
    Icon: HelpCircle,
    hint: 'the arithmetic could not be trusted here',
  },
};

const FONT = { fontFamily: 'DM Sans, sans-serif' };

/** Render [F3] style references as chips so a claim is visibly tied to evidence. */
function WithCitations({ text }) {
  if (!text) return null;
  const parts = String(text).split(/(\[[^\]]*F\d+[^\]]*\])/g);
  return (
    <>
      {parts.map((part, i) => {
        if (/^\[[^\]]*F\d+[^\]]*\]$/.test(part)) {
          return (
            <span
              key={i}
              className="inline-block mx-1 px-1.5 py-0.5 rounded-md text-[9px] font-bold align-middle"
              style={{ background: 'rgba(13,148,136,0.10)', color: '#0D9488', ...FONT }}
              title="Evidence — see the probes below"
            >
              {part.slice(1, -1)}
            </span>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </>
  );
}

function Hypothesis({ item }) {
  const verdict = VERDICTS[item.verdict] || VERDICTS.inconclusive;
  const isAssociation = item.confidence === 'association';
  const { Icon } = verdict;

  return (
    <li
      className="flex gap-2.5 py-2.5"
      style={{ borderTop: '1px solid rgba(13,148,136,0.08)' }}
    >
      <Icon size={14} className="shrink-0 mt-0.5" style={{ color: verdict.tone }} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5 mb-1">
          <span className="text-[11px] font-bold text-[#1A3A38]" style={FONT}>
            {item.dimension}
          </span>
          <span
            className="px-1.5 py-0.5 rounded-full text-[8.5px] font-bold uppercase tracking-wide"
            style={{ background: verdict.bg, color: verdict.tone, ...FONT }}
            title={verdict.hint}
          >
            {verdict.label}
          </span>
          {/*
            An association is demoted, not hidden. It may be true and relevant, but
            nothing here establishes that it caused anything, and a reader must be
            able to see that at a glance.
          */}
          {isAssociation && (
            <span
              className="px-1.5 py-0.5 rounded-full text-[8.5px] font-bold uppercase tracking-wide"
              style={{ background: 'rgba(100,116,139,0.10)', color: '#64748B', ...FONT }}
              title="These move together. That is not evidence of a cause."
            >
              Correlation only
            </span>
          )}
        </div>
        <p
          className={`text-[12px] leading-relaxed ${isAssociation ? 'text-[#64748B] italic' : 'text-[#1A3A38]'}`}
          style={FONT}
        >
          {item.statement}
        </p>
        {item.evidence?.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-1.5">
            {item.evidence.map((id) => (
              <span
                key={id}
                className="px-1.5 py-0.5 rounded-md text-[8.5px] font-bold"
                style={{ background: 'rgba(13,148,136,0.10)', color: '#0D9488', ...FONT }}
              >
                {id}
              </span>
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

export default function DiagnosisPanel({ diagnosis, message, compact = false }) {
  const [showEvidence, setShowEvidence] = useState(false);
  const [showReasoning, setShowReasoning] = useState(false);
  if (!diagnosis) return null;

  const {
    answer, summary, metric, target_window: targetWindow,
    comparison_window: comparisonWindow,
    dimensions_examined: dimensionsExamined = [], hypotheses = [], evidence = [],
    cautions = [], notes = [], stopped_because: stoppedBecause,
    data_warnings: dataWarnings = [],
  } = diagnosis;

  // The bottom line is what the reader came for, so it is the only prose shown by
  // default. `answer` — the full linear text — repeats every section below it, and
  // showing both is what made a live answer unreadable: the data warning appeared
  // in the red box and again six lines down, the one finding appeared in the prose
  // and again under its own EXPLAINS badge, and five ruled-out axes were listed
  // immediately above a section headed "checked and ruled out".
  const bottomLine = summary || answer || message;
  const hasFullReasoning = Boolean(answer) && answer !== bottomLine;

  // Ruled-out findings are separated rather than dropped: knowing what was checked
  // and eliminated is often the most useful part of an answer.
  const leading = hypotheses.filter((h) => h.verdict === 'explains' || h.verdict === 'partial');
  const ruledOut = hypotheses.filter((h) => h.verdict === 'not_it');
  const unusable = hypotheses.filter((h) => h.verdict === 'inconclusive');
  const failedProbes = evidence.filter((e) => e.error);

  return (
    <div className="w-full">
      <div className="flex items-center gap-1.5 text-[#7C3AED] font-bold text-xs mb-2" style={FONT}>
        <Search size={13} /> Diagnosis
        {metric && (
          <span className="font-medium text-[#4A7B76] text-[10px]">· {metric}</span>
        )}
      </div>

      {/* The comparison is stated up front: "revenue is down 18%" means nothing
          without "against what". */}
      {(targetWindow || comparisonWindow) && (
        <div className="flex flex-wrap items-center gap-1.5 mb-2.5 text-[9.5px]" style={FONT}>
          <span
            className="px-2 py-0.5 rounded-full font-bold"
            style={{ background: 'rgba(124,58,237,0.08)', color: '#7C3AED' }}
          >
            {targetWindow}
          </span>
          <span className="text-[#4A7B76]">compared with</span>
          <span
            className="px-2 py-0.5 rounded-full font-bold"
            style={{ background: 'rgba(100,116,139,0.10)', color: '#475569' }}
          >
            {comparisonWindow || 'no baseline'}
          </span>
        </div>
      )}

      {/*
        Data warnings sit ABOVE the answer, deliberately. A `high` one means the
        finding is probably an artefact of the data rather than a business event —
        that has to be read before the finding, not after it. A live June 2026 churn
        diagnosis reported a 163.7% spike for the exact month whose monthly append is
        documented as having written orphan rows across seven tables, and said
        nothing about it.
      */}
      {dataWarnings.length > 0 && (
        <div className="flex flex-col gap-1.5 mb-2.5">
          {dataWarnings.map((w) => {
            const high = w.severity === 'high';
            return (
              <div
                key={w.id}
                className="px-3 py-2 rounded-xl border-l-[3px]"
                style={{
                  background: high ? 'rgba(220,38,38,0.06)' : 'rgba(100,116,139,0.06)',
                  borderColor: high ? '#DC2626' : '#64748B',
                }}
              >
                <div
                  className="flex items-center gap-1.5 font-bold text-[9.5px] uppercase tracking-wider mb-1"
                  style={{ color: high ? '#B91C1C' : '#475569', ...FONT }}
                >
                  <AlertTriangle size={11} />
                  {high ? 'This may not be a real movement' : 'Data note'}
                </div>
                <p
                  className="text-[11px] leading-snug"
                  style={{ color: high ? '#7F1D1D' : '#334155', ...FONT }}
                >
                  {w.summary}{w.guidance ? ` ${w.guidance}` : ''}
                </p>
              </div>
            );
          })}
        </div>
      )}

      {/* The conclusion, given the visual weight of a conclusion. */}
      <div
        className="px-3 py-2.5 rounded-xl mb-2.5"
        style={{ background: 'rgba(124,58,237,0.05)', border: '1px solid rgba(124,58,237,0.12)' }}
      >
        <div
          className="text-[9px] font-bold uppercase tracking-wider mb-1"
          style={{ color: '#7C3AED', ...FONT }}
        >
          Bottom line
        </div>
        <p
          className="text-sm text-[#1A3A38] leading-relaxed whitespace-pre-line"
          style={FONT}
        >
          <WithCitations text={bottomLine} />
        </p>
      </div>

      {/* Axes that produced no usable attribution. Kept OUT of the verdict list
          below, because an "Inconclusive" badge beside a dimension name reads as a
          weak finding when it actually means no share was computed at all — either
          the metric does not sum across segments (a rate), or there was no gap to
          explain. The statement says which; the heading must be true of both. */}
      {unusable.length > 0 && (
        <div
          className="mb-2.5 px-3 py-2 rounded-xl border-l-[3px]"
          style={{ background: 'rgba(100,116,139,0.05)', borderColor: '#94A3B8' }}
        >
          <div
            className="flex items-center gap-1.5 font-bold text-[9.5px] uppercase tracking-wider mb-1"
            style={{ color: '#475569', ...FONT }}
          >
            <HelpCircle size={11} /> Not usable for attribution
          </div>
          <ul className="list-disc pl-4 m-0 space-y-1">
            {unusable.map((h, i) => (
              <li key={i} className="text-[11px] text-[#334155] leading-snug" style={FONT}>
                {h.statement}
              </li>
            ))}
          </ul>
        </div>
      )}

      {leading.length > 0 && (
        <ul className="list-none p-0 m-0 mb-1">
          {leading.map((h, i) => <Hypothesis key={`${h.dimension}-${i}`} item={h} />)}
        </ul>
      )}

      {ruledOut.length > 0 && (
        <div className="mt-2">
          <div className="text-[9px] font-bold uppercase tracking-wider text-[#64748B] mb-1" style={FONT}>
            Checked and ruled out
          </div>
          <ul className="list-none p-0 m-0">
            {ruledOut.map((h, i) => <Hypothesis key={`${h.dimension}-out-${i}`} item={h} />)}
          </ul>
        </div>
      )}

      {/* Caveats are metric-specific traps that would otherwise turn a data artifact
          into a confident business finding — fct_mrr_monthly's trailing churn-only
          period being the standing example. */}
      {cautions.length > 0 && (
        <div
          className="mt-2.5 px-3 py-2 rounded-xl border-l-[3px]"
          style={{ background: 'rgba(245,158,11,0.06)', borderColor: '#F59E0B' }}
        >
          <div className="flex items-center gap-1.5 text-[#B45309] font-bold text-[9.5px] uppercase tracking-wider mb-1" style={FONT}>
            <AlertTriangle size={11} /> Read with care
          </div>
          <ul className="list-disc pl-4 m-0 space-y-1">
            {cautions.map((c, i) => (
              <li key={i} className="text-[11px] text-[#78350F] leading-snug" style={FONT}>{c}</li>
            ))}
          </ul>
        </div>
      )}

      {notes.length > 0 && (
        <div className="mt-2 space-y-1">
          {notes.map((n, i) => (
            <div key={i} className="flex gap-1.5 text-[10.5px] text-[#4A7B76] leading-snug" style={FONT}>
              <Info size={11} className="shrink-0 mt-0.5" />
              <span>{n}</span>
            </div>
          ))}
        </div>
      )}

      {stoppedBecause && (
        <div className="mt-2 text-[10.5px] text-[#B45309]" style={FONT}>
          {stoppedBecause}
        </div>
      )}

      {hasFullReasoning && (
        <div className="mt-2.5">
          <button
            type="button"
            onClick={() => setShowReasoning((v) => !v)}
            className="inline-flex items-center gap-1 text-[10px] font-bold text-[#7C3AED] hover:underline"
            style={FONT}
          >
            {showReasoning ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            Full reasoning
          </button>
          {showReasoning && (
            <p
              className="mt-1.5 px-3 py-2 rounded-xl text-[11px] text-[#334155] leading-relaxed whitespace-pre-line"
              style={{ background: 'rgba(255,255,255,0.5)', ...FONT }}
            >
              <WithCitations text={answer} />
            </p>
          )}
        </div>
      )}

      {evidence.length > 0 && (
        <div className="mt-2.5">
          <button
            type="button"
            onClick={() => setShowEvidence((v) => !v)}
            className="inline-flex items-center gap-1 text-[10px] font-bold text-[#0D9488] hover:underline"
            style={FONT}
          >
            {showEvidence ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            Evidence · {evidence.length} {evidence.length === 1 ? 'probe' : 'probes'}
            {failedProbes.length > 0 && (
              <span className="text-[#B45309] font-medium">
                ({failedProbes.length} failed)
              </span>
            )}
          </button>

          {showEvidence && (
            <div className="mt-1.5 overflow-x-auto rounded-xl" style={{ background: 'rgba(255,255,255,0.5)' }}>
              <table className="w-full text-[10.5px] border-collapse" style={FONT}>
                <thead>
                  <tr className="text-[#4A7B76] text-left">
                    <th className="px-2 py-1.5 font-bold">Id</th>
                    <th className="px-2 py-1.5 font-bold">Probe</th>
                    <th className="px-2 py-1.5 font-bold text-right">Rows</th>
                  </tr>
                </thead>
                <tbody>
                  {evidence.map((e) => (
                    <tr key={e.id} style={{ borderTop: '1px solid rgba(13,148,136,0.08)' }}>
                      <td className="px-2 py-1.5">
                        <span
                          className="px-1.5 py-0.5 rounded-md text-[9px] font-bold"
                          style={{
                            background: e.error ? 'rgba(220,38,38,0.08)' : 'rgba(13,148,136,0.10)',
                            color: e.error ? '#B91C1C' : '#0D9488',
                          }}
                        >
                          {e.id}
                        </span>
                      </td>
                      <td className="px-2 py-1.5 text-[#1A3A38]">
                        {e.label}
                        {e.from_cache && (
                          <span className="ml-1.5 text-[8.5px] text-[#4A7B76]">cached</span>
                        )}
                        {e.error && (
                          <div className="flex items-start gap-1 text-[9.5px] text-[#B91C1C] mt-0.5">
                            <XCircle size={10} className="shrink-0 mt-0.5" />
                            <span className="break-words">{e.error}</span>
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-1.5 text-right text-[#4A7B76] tabular-nums">
                        {e.row_count}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {!compact && dimensionsExamined.length > 0 && (
        <div className="mt-2 text-[9.5px] text-[#4A7B76]" style={FONT}>
          Decomposed by {dimensionsExamined.join(', ')}
        </div>
      )}
    </div>
  );
}
