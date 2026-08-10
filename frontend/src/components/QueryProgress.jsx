/**
 * components/QueryProgress.jsx — Staged progress indicator for the query pipeline.
 *
 * The gateway answers each query with a SINGLE request (no server-sent stage
 * events), so this is an *estimated*, time-based progression calibrated to the
 * real pipeline:
 *   intent extraction (LLM #1) → semantic validation → MetricFlow compile
 *   (warm in-process engine) → DuckDB execution → narrative summary (LLM #2).
 *
 * Since the warehouse became a local DuckDB file, the two LLM calls are ~90 %
 * of the round trip: each runs ~1-2 s, while compilation is ~60-96 ms and the
 * query itself is single-digit milliseconds. So the typical total is ~3-5 s,
 * not the ~30 s the Snowflake-era version of this file assumed.
 *
 * The bar advances optimistically and only reaches 100 % when the actual
 * response arrives (the parent unmounts this component), so it never claims to
 * be finished before it is. It also covers the two remaining slow cases — a
 * cold Render instance waking up, and the first query after a restart having to
 * build the MetricFlow engine (~20 s) if the startup pre-warm has not finished.
 */
import { useState, useEffect } from 'react';

const CLAY_INSET = `inset 6px 6px 12px rgba(13,148,136,0.10), inset -6px -6px 12px rgba(255,255,255,0.9)`;

// Estimated stage boundaries (seconds), calibrated to observed timings:
// intent ~1-2 s, validation <0.1 s, MetricFlow compile ~0.1 s, DuckDB a few ms,
// narrative summary ~1-2 s. The last stage only shows on a cold gateway.
const STAGES = [
  { at: 0.0, label: 'Extracting intent from your question…' },
  { at: 1.6, label: 'Validating against the certified semantic layer…' },
  { at: 2.0, label: 'Compiling governed SQL with MetricFlow…' },
  { at: 2.4, label: 'Running the query on DuckDB…' },
  { at: 2.8, label: 'Writing the plain-English summary…' },
  { at: 8.0, label: 'Warming up the gateway — first query since it restarted…' },
];

export default function QueryProgress() {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const start = Date.now();
    const id = setInterval(() => setElapsed((Date.now() - start) / 1000), 100);
    return () => clearInterval(id);
  }, []);

  // Optimistic fill: quick to ~40 % (intent + validation), then eases toward ~92 %.
  // The 5 s time constant matches the ~3-5 s typical round trip; it still creeps
  // rather than stalls if the gateway happens to be cold.
  const pct =
    elapsed < 2
      ? elapsed * 20
      : Math.min(92, 40 + (1 - Math.exp(-(elapsed - 2) / 5)) * 52);

  const stage = [...STAGES].reverse().find((s) => elapsed >= s.at) ?? STAGES[0];
  const showWhy = elapsed > 7;

  return (
    <div className="flex flex-col gap-3 px-4 py-4 animate-fade-in">
      {/* stage label + elapsed timer */}
      <div className="flex items-center gap-3">
        <span className="w-5 h-5 rounded-full border-[2.5px] border-[#0D9488]/30 border-t-[#0D9488] animate-spin shrink-0" />
        <span
          className="text-sm font-medium text-[#1A3A38]"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          {stage.label}
        </span>
        <span
          className="text-xs text-[#4A7B76] tabular-nums ml-auto shrink-0"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          {elapsed.toFixed(1)}s
        </span>
      </div>

      {/* progress track */}
      <div
        className="h-2.5 w-full rounded-full overflow-hidden"
        style={{ background: '#E6F7F6', boxShadow: CLAY_INSET }}
        role="progressbar"
        aria-label="Query progress"
        aria-valuetext={stage.label}
      >
        <div
          className="h-full rounded-full"
          style={{
            width: `${pct}%`,
            background: 'linear-gradient(90deg, #2DD4BF, #0D9488)',
            transition: 'width 0.3s ease-out',
          }}
        />
      </div>

      {/* why-it-takes-time explainer — appears once the query is clearly slower than usual */}
      {showWhy && (
        <p
          className="text-xs leading-relaxed text-[#4A7B76] animate-fade-in"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          Taking longer than usual — the gateway is likely waking from sleep, or building its{' '}
          <span className="font-semibold text-[#0D9488]">in-process MetricFlow engine</span> for the
          first query since a restart. Queries after this one land in a few seconds, and a repeat of
          the same question is served from cache.
        </p>
      )}
    </div>
  );
}
