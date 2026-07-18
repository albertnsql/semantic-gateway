/**
 * components/QueryProgress.jsx — Staged progress indicator for the query pipeline.
 *
 * The gateway answers each query with a SINGLE request (no server-sent stage
 * events), so this is an *estimated*, time-based progression calibrated to the
 * real pipeline:
 *   intent extraction (LLM) → semantic validation → SQL generation
 *   (cached template, in-process fallback, OR ~30 s MetricFlow compile on a
 *   first-time metric) → Snowflake → response.
 *
 * The bar advances optimistically and only reaches 100 % when the actual
 * response arrives (the parent unmounts this component), so it never claims to
 * be finished before it is. It works for both the fast (cached, ~2-4 s) and the
 * slow (cache-miss MetricFlow, ~30 s) cases — the fast case simply unmounts
 * early, before the later stages are reached.
 */
import { useState, useEffect } from 'react';

const CLAY_INSET = `inset 6px 6px 12px rgba(13,148,136,0.10), inset -6px -6px 12px rgba(255,255,255,0.9)`;

// Estimated stage boundaries (seconds), calibrated to observed timings:
// intent ~1 s, validation <0.1 s, MetricFlow ~30 s on a cache miss, Snowflake ~2 s.
const STAGES = [
  { at: 0.0,  label: 'Extracting intent from your question…' },
  { at: 1.8,  label: 'Validating against the certified semantic layer…' },
  { at: 3.0,  label: 'Generating governed SQL…' },
  { at: 8.0,  label: 'Compiling with MetricFlow — first-time metric, no cached template yet…' },
  { at: 28.0, label: 'Running the query on Snowflake…' },
];

export default function QueryProgress() {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const start = Date.now();
    const id = setInterval(() => setElapsed((Date.now() - start) / 1000), 100);
    return () => clearInterval(id);
  }, []);

  // Optimistic fill: quick to ~40 % (intent + validation), then eases toward ~92 %.
  const pct =
    elapsed < 2
      ? elapsed * 20
      : Math.min(92, 40 + (1 - Math.exp(-(elapsed - 2) / 12)) * 52);

  const stage = [...STAGES].reverse().find((s) => elapsed >= s.at) ?? STAGES[0];
  const showWhy = elapsed > 5;

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

      {/* why-it-takes-time explainer — appears once it's clearly a slow (uncached) query */}
      {showWhy && (
        <p
          className="text-xs leading-relaxed text-[#4A7B76] animate-fade-in"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          First-time metric queries compile{' '}
          <span className="font-semibold text-[#0D9488]">governed SQL via MetricFlow</span>, which can
          take ~30 s. The same query afterwards is served from cache in under a second.
        </p>
      )}
    </div>
  );
}
