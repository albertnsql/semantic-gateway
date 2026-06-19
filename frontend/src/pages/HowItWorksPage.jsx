/**
 * pages/HowItWorksPage.jsx — Step-by-step query journey page.
 * Proper page layout: two-column timeline, section dividers, document hierarchy.
 */
import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  MessageSquare, Brain, Map, Code2, ShieldCheck, Zap, GitBranch,
  ChevronDown, ChevronUp, ArrowRight, Database, Server, Layers,
  Shield, Cpu, Monitor, AlertTriangle, BookOpen,
} from 'lucide-react';

// ── Design tokens (mirror landing page) ────────────────────────────────────

const CLAY_SHADOW   = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_SHADOW_H = `20px 20px 40px rgba(13,148,136,0.18), -12px -12px 28px rgba(255,255,255,0.95), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_BTN     = `12px 12px 24px rgba(13,148,136,0.30), -8px -8px 16px rgba(255,255,255,0.4), inset 4px 4px 8px rgba(255,255,255,0.4), inset -4px -4px 8px rgba(0,0,0,0.08)`;
const INSET_SHADOW  = `inset 10px 10px 20px rgba(13,148,136,0.06), inset -10px -10px 20px rgba(255,255,255,0.85)`;
const SECTION_BG    = `30px 30px 60px rgba(13,148,136,0.08), -30px -30px 60px #ffffff, inset 10px 10px 20px rgba(13,148,136,0.04), inset -10px -10px 20px rgba(255,255,255,0.8)`;

// ── Data ────────────────────────────────────────────────────────────────────

const STEPS = [
  {
    num: 1,
    icon: MessageSquare,
    gradient: 'from-sky-400 to-sky-600',
    accentColor: '#0EA5E9',
    tag: 'Input',
    title: 'Ask in plain English',
    summary: 'You type a question like "What\'s churn by plan type this quarter?" — no SQL, no schema knowledge required.',
    detail: `The question is sent as a raw string to the gateway's intent classifier endpoint. Gemini 1.5 Flash acts as the first-stage router, embedding your question and comparing it against the known metric vocabulary to decide which handler pipeline to invoke. No SQL is generated at this stage.`,
  },
  {
    num: 2,
    icon: Brain,
    gradient: 'from-violet-400 to-violet-600',
    accentColor: '#7C3AED',
    tag: 'Routing',
    title: 'Classify intent',
    summary: 'The gateway decides if this is a metric question, a schema question, or out of scope — before touching any data.',
    detail: `A two-stage classification pipeline routes to one of three intents: metric_query, schema_question, or out_of_scope. Out-of-scope rejection is a deliberate guardrail — it prevents the LLM from falling back to arbitrary SQL generation when the question doesn't map to a certified metric. This is the primary defence against prompt-injection and data-exfiltration attempts via natural language.`,
  },
  {
    num: 3,
    icon: Map,
    gradient: 'from-amber-400 to-amber-600',
    accentColor: '#D97706',
    tag: 'Resolution',
    title: 'Resolve entities & dimensions',
    summary: 'Plan type, country, cohort — the gateway maps your words to certified fields in the semantic model.',
    detail: `Entity-prefixed dimension resolution translates user-facing terms (e.g. "plan type") to fully-qualified MetricFlow field names (e.g. subscriber__plan_type). MetricRegistry.load() is called to resolve the allowed_joins set across entities, ensuring that only valid cross-entity relationships are considered. Unresolvable terms are surfaced as an error rather than silently dropped.`,
  },
  {
    num: 4,
    icon: Code2,
    gradient: 'from-teal-400 to-teal-600',
    accentColor: '#0D9488',
    tag: 'Compilation',
    title: 'Generate governed SQL',
    summary: 'MetricFlow compiles SQL from certified metric definitions — not a freeform LLM guess.',
    detail: `The MetricFlow CLI (metricflow query) receives the resolved metric name, dimensions, and time grain. It generates SQL that is guaranteed to respect the semantic model's grain constraints and join topology. This eliminates the category of errors where an LLM invents a join path that looks plausible but produces a fanout multiplication. The generated SQL is deterministic for a given metric + dimension combination.`,
  },
  {
    num: 5,
    icon: ShieldCheck,
    gradient: 'from-emerald-400 to-emerald-600',
    accentColor: '#059669',
    tag: 'Validation',
    title: 'Speculative review',
    summary: 'Before anything runs, a review pass checks the SQL for invalid columns or logic errors.',
    detail: `A speculative review stage sends the MetricFlow-generated SQL back through a Gemini call that acts as a critic, looking specifically for hallucinated column names, incorrect aggregation functions, or grain mismatches the static compiler might not catch. This stage was added after observing LLM-hallucinated column names slipping through in edge-case dimension combinations. The reviewer runs against the warehouse schema cache — not live Snowflake — keeping latency low.`,
  },
  {
    num: 6,
    icon: Zap,
    gradient: 'from-orange-400 to-orange-600',
    accentColor: '#EA580C',
    tag: 'Execution',
    title: 'Cache check & execute',
    summary: 'Seen this question before? Instant answer. Otherwise it runs against Snowflake.',
    detail: `A two-layer cache is checked: SQLTemplateCache first (parameterized SQL template + dimension set), then a result cache keyed by (metric, dimensions, date_range). The {start_date}/{end_date} parameterization fix was critical — naive caching keyed on the full rendered SQL string meant date-range queries were never cache-hitting. With parameterized templates, queries for "last quarter" at different calendar dates reuse the same template and only diff the bound parameters. On a cache miss, the validated SQL is dispatched to Snowflake via the configured connector.`,
  },
  {
    num: 7,
    icon: GitBranch,
    gradient: 'from-cyan-400 to-cyan-600',
    accentColor: '#0891B2',
    tag: 'Output',
    title: 'Return result + lineage',
    summary: 'You get your answer, plus a traceable path back to the raw data.',
    detail: `The response payload includes the query result, the rendered SQL, and a lineage graph that maps from the returned metric back through the dbt semantic model nodes to the raw source tables in Snowflake. This lineage is queryable in the Lineage Explorer — every node in the graph is a certified dbt model or source, so auditors can verify the derivation chain without touching code.`,
  },
];

const HARD_THINGS = [
  {
    title: 'Grain violations',
    desc: 'Mixing MRR (subscription+month grain) with session-level data produces silent row count multiplication. MetricFlow\'s grain constraints catch this before SQL is executed.',
  },
  {
    title: 'Join fanout on naïve joins',
    desc: 'Direct SQL agents pick the nearest foreign key and join — producing 10× row counts that look right until you compare totals. MetricFlow\'s allowed_joins topology eliminates this class of error.',
  },
  {
    title: 'Ambiguous metric phrasing',
    desc: '"Revenue" means five different things depending on whether it\'s prorated, annualized, or recognized. Certified metric definitions enforce the canonical calculation.',
  },
  {
    title: 'LLM hallucination on schema',
    desc: 'LLMs confidently generate column names that don\'t exist. The speculative review stage and entity-prefixed resolution catch these before a query reaches the warehouse.',
  },
];

const PIPELINE = [
  { icon: Database, label: 'Raw SaaS Data',             gradient: 'from-sky-400 to-sky-600',          step: null },
  { icon: Server,   label: 'Snowflake DWH',             gradient: 'from-cyan-400 to-cyan-600',        step: null },
  { icon: Code2,    label: 'dbt Models',                gradient: 'from-amber-400 to-amber-600',      step: null },
  { icon: Layers,   label: 'MetricFlow\nSemantic Layer',gradient: 'from-teal-400 to-teal-600',        step: '4'   },
  { icon: Shield,   label: 'FastAPI\nGateway',          gradient: 'from-emerald-400 to-emerald-600',  step: '2–3' },
  { icon: Cpu,      label: 'Gemini 1.5\nLLM',           gradient: 'from-teal-400 to-teal-700',        step: '1'   },
  { icon: Monitor,  label: 'React\nFrontend',           gradient: 'from-cyan-400 to-teal-600',        step: '7'   },
];

// ── Timeline Step Row ────────────────────────────────────────────────────────

function StepRow({ step, isLast }) {
  const [open, setOpen] = useState(false);
  const Icon = step.icon;

  return (
    <div className="flex gap-0">
      {/* ── Left rail: number + line ── */}
      <div className="flex flex-col items-center" style={{ width: '72px', flexShrink: 0 }}>
        {/* Big step circle */}
        <div
          className="w-11 h-11 rounded-full flex items-center justify-center text-white font-black text-lg shrink-0 z-10"
          style={{
            background: `linear-gradient(135deg, ${step.accentColor}cc, ${step.accentColor})`,
            boxShadow: CLAY_BTN,
            fontFamily: 'Nunito, sans-serif',
          }}
        >
          {step.num}
        </div>
        {/* Connector line */}
        {!isLast && (
          <div
            className="flex-1 w-0.5 mt-2"
            style={{
              background: `linear-gradient(to bottom, ${step.accentColor}60, rgba(13,148,136,0.10))`,
              minHeight: '32px',
            }}
          />
        )}
      </div>

      {/* ── Right content panel ── */}
      <div className="flex-1 min-w-0 pb-10">
        {/* Header row */}
        <div className="flex items-start gap-4 mb-4">
          {/* Icon */}
          <div
            className={`w-11 h-11 rounded-[16px] flex items-center justify-center shrink-0 bg-gradient-to-br ${step.gradient}`}
            style={{ boxShadow: CLAY_BTN }}
          >
            <Icon size={20} className="text-white" />
          </div>

          <div className="flex-1 min-w-0 pt-0.5">
            {/* Tag chip */}
            <span
              className="inline-block text-[10px] font-black tracking-widest uppercase mb-1 px-2 py-0.5 rounded-full"
              style={{
                color: step.accentColor,
                background: `${step.accentColor}18`,
                fontFamily: 'DM Sans, sans-serif',
              }}
            >
              {step.tag}
            </span>
            <h3
              className="text-xl font-black text-[#1A3A38] leading-tight"
              style={{ fontFamily: 'Nunito, sans-serif' }}
            >
              {step.title}
            </h3>
          </div>
        </div>

        {/* Summary text */}
        <p
          className="text-[#4A7B76] text-base leading-relaxed mb-4"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          {step.summary}
        </p>

        {/* Toggle + accordion */}
        <button
          onClick={() => setOpen(!open)}
          className="flex items-center gap-2 text-xs font-bold text-[#0D9488] transition-all duration-200 hover:gap-3"
          style={{ fontFamily: 'DM Sans, sans-serif', background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}
          aria-expanded={open}
        >
          {open ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          {open ? 'Hide technical detail' : 'Show technical detail'}
        </button>

        {open && (
          <div
            className="mt-4 rounded-[20px] p-6 animate-slide-in"
            style={{
              background: 'rgba(240,253,250,0.8)',
              boxShadow: INSET_SHADOW,
              fontFamily: 'DM Sans, sans-serif',
            }}
          >
            {/* Mono-style "impl" label */}
            <div className="flex items-center gap-2 mb-3">
              <span
                className="text-[9px] font-black tracking-widest uppercase px-2 py-0.5 rounded"
                style={{
                  background: `${step.accentColor}20`,
                  color: step.accentColor,
                  fontFamily: 'JetBrains Mono, monospace',
                }}
              >
                Implementation
              </span>
            </div>
            <p className="text-sm text-[#1A3A38] leading-relaxed">{step.detail}</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function HowItWorksPage() {
  return (
    <div className="max-w-5xl mx-auto flex flex-col gap-0 animate-fade-in relative z-10 pb-10">

      {/* ══════════════════════════════════════════════════════════════════════
          HERO HEADER
      ══════════════════════════════════════════════════════════════════════ */}
      <section
        className="rounded-[40px] p-12 mb-10 flex flex-col gap-6 backdrop-blur-xl"
        style={{
          background: 'rgba(255,255,255,0.55)',
          boxShadow: SECTION_BG,
        }}
      >
        {/* Breadcrumb-style label row */}
        <div className="flex items-center gap-3 flex-wrap">
          <div
            className="inline-flex items-center gap-2 rounded-full px-4 py-1.5"
            style={{ background: 'rgba(13,148,136,0.08)', fontFamily: 'DM Sans, sans-serif' }}
          >
            <span className="h-1.5 w-1.5 rounded-full bg-[#0D9488] animate-clay-breathe" />
            <span className="text-[11px] font-black tracking-widest text-[#0D9488] uppercase">
              Architecture Deep-Dive
            </span>
          </div>
          <span className="text-[#4A7B76]/40 text-sm">·</span>
          <span
            className="text-[11px] font-bold text-[#4A7B76] tracking-wide"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            7 steps · MetricFlow · Gemini 1.5 · Snowflake
          </span>
        </div>

        {/* H1 */}
        <div>
          <h1
            className="text-5xl sm:text-6xl font-black tracking-tight leading-[1.05] text-[#1A3A38] mb-4"
            style={{ fontFamily: 'Nunito, sans-serif' }}
          >
            How It{' '}
            <span
              className="bg-clip-text text-transparent"
              style={{ backgroundImage: 'linear-gradient(135deg, #0D9488, #2DD4BF, #0891B2)' }}
            >
              Works
            </span>
          </h1>
          <p
            className="text-lg font-medium leading-relaxed text-[#4A7B76] max-w-2xl"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            From a plain-English question to a governed, lineage-traced answer —
            every step validated against your{' '}
            <span className="text-[#1A3A38] font-semibold">certified metric definitions.</span>
          </p>
        </div>

        {/* Quick-stat row */}
        <div className="flex items-center gap-6 flex-wrap pt-2">
          {[
            { label: 'Pipeline steps',     value: '7'         },
            { label: 'Intent classes',      value: '3'         },
            { label: 'Cache layers',        value: '2'         },
            { label: 'Certified metrics',   value: '10'        },
          ].map(({ label, value }) => (
            <div key={label} className="flex flex-col gap-0.5">
              <span
                className="text-2xl font-black text-[#0D9488]"
                style={{ fontFamily: 'Nunito, sans-serif' }}
              >
                {value}
              </span>
              <span
                className="text-xs text-[#4A7B76]"
                style={{ fontFamily: 'DM Sans, sans-serif' }}
              >
                {label}
              </span>
            </div>
          ))}
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════════════════
          SECTION LABEL: THE QUERY JOURNEY
      ══════════════════════════════════════════════════════════════════════ */}
      <div className="flex items-center gap-4 mb-8 px-1">
        <div className="flex-1 h-px" style={{ background: 'linear-gradient(to right, rgba(13,148,136,0.25), transparent)' }} />
        <span
          className="text-xs font-black tracking-[0.2em] text-[#0D9488] uppercase"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          The Query Journey
        </span>
        <div className="flex-1 h-px" style={{ background: 'linear-gradient(to left, rgba(13,148,136,0.25), transparent)' }} />
      </div>

      {/* ══════════════════════════════════════════════════════════════════════
          TIMELINE — 7 STEPS (two-column: number rail + wide content)
      ══════════════════════════════════════════════════════════════════════ */}
      <section className="mb-6 px-2">
        {STEPS.map((step, idx) => (
          <StepRow key={step.num} step={step} isLast={idx === STEPS.length - 1} />
        ))}
      </section>

      {/* ══════════════════════════════════════════════════════════════════════
          SECTION LABEL: WHY THIS IS HARD
      ══════════════════════════════════════════════════════════════════════ */}
      <div className="flex items-center gap-4 mb-8 px-1">
        <div className="flex-1 h-px" style={{ background: 'linear-gradient(to right, rgba(245,158,11,0.30), transparent)' }} />
        <span
          className="text-xs font-black tracking-[0.2em] text-[#D97706] uppercase"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          Why This Is Hard
        </span>
        <div className="flex-1 h-px" style={{ background: 'linear-gradient(to left, rgba(245,158,11,0.30), transparent)' }} />
      </div>

      {/* ══════════════════════════════════════════════════════════════════════
          WHY THIS IS HARD — 2×2 grid
      ══════════════════════════════════════════════════════════════════════ */}
      <section className="mb-12">
        {/* Intro callout */}
        <div
          className="rounded-[28px] px-8 py-5 mb-6 flex items-start gap-4"
          style={{
            background: 'rgba(254,243,199,0.60)',
            boxShadow: '12px 12px 24px rgba(245,158,11,0.08), -8px -8px 16px rgba(255,255,255,0.9)',
          }}
        >
          <div
            className="w-10 h-10 rounded-full flex items-center justify-center shrink-0 bg-gradient-to-br from-amber-400 to-orange-500 mt-0.5"
            style={{ boxShadow: CLAY_BTN }}
          >
            <AlertTriangle size={18} className="text-white" />
          </div>
          <p
            className="text-sm text-[#92400E] leading-relaxed"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            These are the failure modes naive NL-to-SQL hits in production — silent wrong answers, not
            loud errors. The gateway addresses each one structurally, not with prompt engineering.
          </p>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
          {HARD_THINGS.map(({ title, desc }, i) => (
            <div
              key={title}
              className="rounded-[28px] p-7 flex flex-col gap-3 backdrop-blur-xl"
              style={{
                background: 'rgba(255,255,255,0.65)',
                boxShadow: CLAY_SHADOW,
              }}
            >
              {/* Number + title row */}
              <div className="flex items-center gap-3">
                <span
                  className="w-7 h-7 rounded-full flex items-center justify-center text-white text-xs font-black shrink-0"
                  style={{
                    background: 'linear-gradient(135deg, #F59E0B, #D97706)',
                    boxShadow: '6px 6px 12px rgba(245,158,11,0.25), -4px -4px 8px rgba(255,255,255,0.4)',
                    fontFamily: 'Nunito, sans-serif',
                  }}
                >
                  {i + 1}
                </span>
                <h3
                  className="font-black text-[#1A3A38] text-base leading-tight"
                  style={{ fontFamily: 'Nunito, sans-serif' }}
                >
                  {title}
                </h3>
              </div>
              <p
                className="text-[#4A7B76] text-sm leading-relaxed"
                style={{ fontFamily: 'DM Sans, sans-serif' }}
              >
                {desc}
              </p>
            </div>
          ))}
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════════════════
          SECTION LABEL: ARCHITECTURE PIPELINE
      ══════════════════════════════════════════════════════════════════════ */}
      <div className="flex items-center gap-4 mb-8 px-1">
        <div className="flex-1 h-px" style={{ background: 'linear-gradient(to right, rgba(13,148,136,0.25), transparent)' }} />
        <span
          className="text-xs font-black tracking-[0.2em] text-[#0D9488] uppercase"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          Architecture Pipeline
        </span>
        <div className="flex-1 h-px" style={{ background: 'linear-gradient(to left, rgba(13,148,136,0.25), transparent)' }} />
      </div>

      {/* ══════════════════════════════════════════════════════════════════════
          PIPELINE DIAGRAM
      ══════════════════════════════════════════════════════════════════════ */}
      <section
        className="rounded-[40px] p-10 mb-12 flex flex-col gap-8 backdrop-blur-xl"
        style={{
          background: 'rgba(255,255,255,0.40)',
          boxShadow: SECTION_BG,
        }}
      >
        <div className="flex flex-col gap-1">
          <p
            className="text-[#4A7B76] text-sm max-w-xl"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            Each node in the stack maps to one or more of the 7 steps above.
            Highlighted nodes are where the gateway's core logic lives.
          </p>
        </div>

        <div className="pb-2 pt-1 px-1 w-full overflow-x-auto">
          <div className="flex items-end justify-between gap-2 min-w-[560px]">
            {PIPELINE.map(({ icon: Icon, label, gradient, step }, idx) => (
              <div key={label} className="flex items-center gap-2 flex-1 min-w-0">
                <div className="flex flex-col items-center gap-2 flex-1 min-w-0">
                  {/* Step badge above node */}
                  <div className="h-6 flex items-center justify-center">
                    {step ? (
                      <span
                        className="text-[9px] font-black text-white px-2 py-0.5 rounded-full"
                        style={{
                          background: 'linear-gradient(135deg, #2DD4BF, #0D9488)',
                          fontFamily: 'Nunito, sans-serif',
                        }}
                      >
                        Step {step}
                      </span>
                    ) : (
                      <span className="text-[9px] text-[#4A7B76]/40 font-medium" style={{ fontFamily: 'DM Sans, sans-serif' }}>
                        infra
                      </span>
                    )}
                  </div>

                  {/* Node card */}
                  <div
                    className="flex flex-col items-center justify-center gap-2.5 w-full py-5 px-2 rounded-[20px] transition-all duration-300 hover:-translate-y-1"
                    style={{
                      background: step
                        ? 'linear-gradient(135deg, rgba(240,253,250,0.95), rgba(255,255,255,0.98))'
                        : 'rgba(255,255,255,0.65)',
                      boxShadow: step ? CLAY_SHADOW_H : CLAY_SHADOW,
                    }}
                  >
                    <div
                      className={`w-10 h-10 rounded-full flex items-center justify-center shrink-0 bg-gradient-to-br ${gradient}`}
                      style={{ boxShadow: CLAY_BTN }}
                    >
                      <Icon size={18} className="text-white" />
                    </div>
                    <span
                      className={`text-[10px] text-center leading-tight whitespace-pre-line font-semibold
                                  ${step ? 'text-[#0D9488]' : 'text-[#1A3A38]'}`}
                      style={{ fontFamily: 'DM Sans, sans-serif' }}
                    >
                      {label}
                    </span>
                  </div>
                </div>

                {/* Arrow connector */}
                {idx < PIPELINE.length - 1 && (
                  <span className="text-[#4A7B76]/30 text-xl select-none shrink-0 mb-2">→</span>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Legend */}
        <div className="flex items-center gap-6 flex-wrap border-t border-[#0D9488]/10 pt-5">
          <div className="flex items-center gap-2">
            <span
              className="w-3 h-3 rounded-full"
              style={{ background: 'linear-gradient(135deg, #2DD4BF, #0D9488)' }}
            />
            <span className="text-xs text-[#4A7B76]" style={{ fontFamily: 'DM Sans, sans-serif' }}>
              Gateway logic — steps mapped here
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span
              className="w-3 h-3 rounded-full"
              style={{ background: 'rgba(255,255,255,0.65)', border: '1px solid rgba(13,148,136,0.15)' }}
            />
            <span className="text-xs text-[#4A7B76]" style={{ fontFamily: 'DM Sans, sans-serif' }}>
              Infrastructure layer
            </span>
          </div>
        </div>
      </section>

      {/* ══════════════════════════════════════════════════════════════════════
          CTA FOOTER
      ══════════════════════════════════════════════════════════════════════ */}
      <section
        className="rounded-[40px] overflow-hidden backdrop-blur-xl"
        style={{ boxShadow: CLAY_SHADOW }}
      >
        {/* Top gradient bar */}
        <div
          className="h-1 w-full"
          style={{ background: 'linear-gradient(90deg, #2DD4BF, #0D9488, #0891B2)' }}
        />
        <div
          className="p-10 flex flex-col sm:flex-row items-center justify-between gap-8"
          style={{ background: 'rgba(255,255,255,0.70)' }}
        >
          <div>
            <div className="flex items-center gap-2 mb-2">
              <BookOpen size={16} className="text-[#0D9488]" />
              <span
                className="text-[11px] font-black tracking-widest text-[#0D9488] uppercase"
                style={{ fontFamily: 'DM Sans, sans-serif' }}
              >
                Ready to try it?
              </span>
            </div>
            <h3
              className="font-black text-[#1A3A38] text-2xl mb-1 tracking-tight"
              style={{ fontFamily: 'Nunito, sans-serif' }}
            >
              See the architecture in action
            </h3>
            <p
              className="text-[#4A7B76] text-sm"
              style={{ fontFamily: 'DM Sans, sans-serif' }}
            >
              Run a real governed query or explore the certified metrics behind every answer.
            </p>
          </div>
          <div className="flex items-center gap-4 flex-wrap shrink-0">
            <Link to="/query" className="btn-primary text-base">
              Try a Query <ArrowRight size={18} />
            </Link>
            <Link to="/metrics" className="btn-outline text-base">
              Browse Certified Metrics
            </Link>
          </div>
        </div>
      </section>

    </div>
  );
}
