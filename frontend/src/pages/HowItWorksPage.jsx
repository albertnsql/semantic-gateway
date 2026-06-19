/**
 * pages/HowItWorksPage.jsx — Step-by-step query journey page.
 * Claymorphism theme, matching existing design system.
 */
import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  MessageSquare, Brain, Map, Code2, ShieldCheck, Zap, GitBranch,
  ChevronDown, ChevronUp, ArrowRight, Database, Server, Layers,
  Shield, Cpu, Monitor, AlertTriangle,
} from 'lucide-react';
import TopBar from '../components/TopBar';

// ── Constants ──────────────────────────────────────────────────────────────

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_SHADOW_HOVER = `20px 20px 40px rgba(13,148,136,0.18), -12px -12px 28px rgba(255,255,255,0.95), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_BTN_SHADOW = `12px 12px 24px rgba(13,148,136,0.30), -8px -8px 16px rgba(255,255,255,0.4), inset 4px 4px 8px rgba(255,255,255,0.4), inset -4px -4px 8px rgba(0,0,0,0.08)`;

const STEPS = [
  {
    num: 1,
    icon: MessageSquare,
    gradient: 'from-sky-400 to-sky-600',
    title: 'Ask in plain English',
    summary: 'You type a question like "What\'s churn by plan type this quarter?" — no SQL, no schema knowledge required.',
    detail: `The question is sent as a raw string to the gateway's intent classifier endpoint. Gemini 1.5 Flash acts as the first-stage router, embedding your question and comparing it against the known metric vocabulary to decide which handler pipeline to invoke. No SQL is generated at this stage.`,
  },
  {
    num: 2,
    icon: Brain,
    gradient: 'from-violet-400 to-violet-600',
    title: 'Classify intent',
    summary: 'The gateway decides if this is a metric question, a schema question, or out of scope — before touching any data.',
    detail: `A two-stage classification pipeline routes to one of three intents: metric_query, schema_question, or out_of_scope. Out-of-scope rejection is a deliberate guardrail — it prevents the LLM from falling back to arbitrary SQL generation when the question doesn't map to a certified metric. This is the primary defence against prompt-injection and data-exfiltration attempts via natural language.`,
  },
  {
    num: 3,
    icon: Map,
    gradient: 'from-amber-400 to-amber-600',
    title: 'Resolve entities & dimensions',
    summary: 'Plan type, country, cohort — the gateway maps your words to certified fields in the semantic model.',
    detail: `Entity-prefixed dimension resolution translates user-facing terms (e.g. "plan type") to fully-qualified MetricFlow field names (e.g. subscriber__plan_type). MetricRegistry.load() is called to resolve the allowed_joins set across entities, ensuring that only valid cross-entity relationships are considered. Unresolvable terms are surfaced as an error rather than silently dropped.`,
  },
  {
    num: 4,
    icon: Code2,
    gradient: 'from-teal-400 to-teal-600',
    title: 'Generate governed SQL',
    summary: 'MetricFlow compiles SQL from certified metric definitions — not a freeform LLM guess.',
    detail: `The MetricFlow CLI (metricflow query) receives the resolved metric name, dimensions, and time grain. It generates SQL that is guaranteed to respect the semantic model's grain constraints and join topology. This eliminates the category of errors where an LLM invents a join path that looks plausible but produces a fanout multiplication. The generated SQL is deterministic for a given metric + dimension combination.`,
  },
  {
    num: 5,
    icon: ShieldCheck,
    gradient: 'from-emerald-400 to-emerald-600',
    title: 'Speculative review',
    summary: 'Before anything runs, a review pass checks the SQL for invalid columns or logic errors.',
    detail: `A speculative review stage sends the MetricFlow-generated SQL back through a Gemini call that acts as a critic, looking specifically for hallucinated column names, incorrect aggregation functions, or grain mismatches the static compiler might not catch. This stage was added after observing LLM-hallucinated column names slipping through in edge-case dimension combinations. The reviewer runs against the warehouse schema cache — not live Snowflake — keeping latency low.`,
  },
  {
    num: 6,
    icon: Zap,
    gradient: 'from-orange-400 to-orange-600',
    title: 'Cache check & execute',
    summary: 'Seen this question before? Instant answer. Otherwise it runs against Snowflake.',
    detail: `A two-layer cache is checked: SQLTemplateCache first (parameterized SQL template + dimension set), then a result cache keyed by (metric, dimensions, date_range). The {start_date}/{end_date} parameterization fix was critical — naive caching keyed on the full rendered SQL string meant date-range queries were never cache-hitting. With parameterized templates, queries for "last quarter" at different calendar dates reuse the same template and only diff the bound parameters. On a cache miss, the validated SQL is dispatched to Snowflake via the configured connector.`,
  },
  {
    num: 7,
    icon: GitBranch,
    gradient: 'from-cyan-400 to-cyan-600',
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
  { icon: Database, label: 'Raw SaaS Data',              gradient: 'from-sky-400 to-sky-600',     step: null },
  { icon: Server,   label: 'Snowflake DWH',              gradient: 'from-cyan-400 to-cyan-600',   step: null },
  { icon: Code2,    label: 'dbt Models',                 gradient: 'from-amber-400 to-amber-600', step: null },
  { icon: Layers,   label: 'MetricFlow\nSemantic Layer', gradient: 'from-teal-400 to-teal-600',   step: '4' },
  { icon: Shield,   label: 'FastAPI\nGateway',           gradient: 'from-emerald-400 to-emerald-600', step: '2–3' },
  { icon: Cpu,      label: 'Gemini 1.5\nLLM',               gradient: 'from-teal-400 to-teal-700',   step: '1' },
  { icon: Monitor,  label: 'React\nFrontend',            gradient: 'from-cyan-400 to-teal-600',   step: '7' },
];

// ── Step Card ───────────────────────────────────────────────────────────────

function StepCard({ step, index }) {
  const [open, setOpen] = useState(false);
  const Icon = step.icon;

  return (
    <div
      className="rounded-[32px] p-8 flex flex-col gap-5 transition-all duration-500 backdrop-blur-xl"
      style={{
        background: 'rgba(255,255,255,0.65)',
        boxShadow: open ? CLAY_SHADOW_HOVER : CLAY_SHADOW,
      }}
    >
      {/* ── Top row ── */}
      <div className="flex items-start gap-5">
        {/* Step number badge */}
        <div className="flex flex-col items-center gap-2 shrink-0">
          <div
            className="w-8 h-8 rounded-full flex items-center justify-center text-white text-xs font-black shrink-0"
            style={{
              background: 'linear-gradient(135deg, #2DD4BF, #0D9488)',
              boxShadow: CLAY_BTN_SHADOW,
              fontFamily: 'Nunito, sans-serif',
            }}
          >
            {step.num}
          </div>
        </div>

        {/* Icon orb */}
        <div
          className={`w-12 h-12 rounded-full flex items-center justify-center shrink-0 bg-gradient-to-br ${step.gradient}`}
          style={{ boxShadow: CLAY_BTN_SHADOW }}
        >
          <Icon size={22} className="text-white" />
        </div>

        {/* Text */}
        <div className="flex-1 min-w-0">
          <h3
            className="font-black text-[#1A3A38] text-lg leading-tight mb-1.5"
            style={{ fontFamily: 'Nunito, sans-serif' }}
          >
            {step.title}
          </h3>
          <p
            className="text-[#4A7B76] text-sm leading-relaxed"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            {step.summary}
          </p>
        </div>
      </div>

      {/* ── Toggle button ── */}
      <button
        onClick={() => setOpen(!open)}
        className="self-start flex items-center gap-2 px-4 py-2 rounded-[16px] text-xs font-bold text-[#0D9488] transition-all duration-200 hover:-translate-y-0.5"
        style={{
          background: 'rgba(255,255,255,0.70)',
          boxShadow: open
            ? 'inset 6px 6px 12px rgba(13,148,136,0.08), inset -6px -6px 12px rgba(255,255,255,0.9)'
            : '8px 8px 16px rgba(13,148,136,0.10), -6px -6px 12px rgba(255,255,255,0.9)',
          fontFamily: 'DM Sans, sans-serif',
        }}
        aria-expanded={open}
      >
        {open ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        {open ? 'Hide technical detail' : 'Show technical detail'}
      </button>

      {/* ── Accordion body ── */}
      {open && (
        <div
          className="rounded-[20px] p-5 animate-slide-in"
          style={{
            background: 'rgba(240,253,250,0.7)',
            boxShadow: 'inset 6px 6px 12px rgba(13,148,136,0.06), inset -6px -6px 12px rgba(255,255,255,0.85)',
            fontFamily: 'DM Sans, sans-serif',
          }}
        >
          <p className="text-sm text-[#1A3A38] leading-relaxed">{step.detail}</p>
        </div>
      )}
    </div>
  );
}

// ── Page ────────────────────────────────────────────────────────────────────

export default function HowItWorksPage() {
  return (
    <div className="max-w-4xl mx-auto flex flex-col gap-14 animate-fade-in relative z-10">

      {/* ── Header band ── */}
      <section className="flex flex-col items-center text-center gap-6 pt-2">
        {/* Pill tag */}
        <div
          className="inline-flex items-center gap-2 rounded-full px-5 py-2 backdrop-blur-sm"
          style={{ background: 'rgba(255,255,255,0.70)', boxShadow: CLAY_SHADOW }}
        >
          <span className="h-2 w-2 rounded-full bg-[#0D9488] animate-clay-breathe" />
          <span
            className="text-xs font-bold tracking-widest text-[#0D9488] uppercase"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            Architecture Deep-Dive · 7 Steps
          </span>
        </div>

        <h1
          className="text-5xl sm:text-6xl font-black tracking-tight leading-[1.1] text-[#1A3A38]"
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
          className="max-w-2xl text-lg font-medium leading-relaxed text-[#4A7B76]"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          From a plain-English question to a governed, lineage-traced answer —
          every step validated against your{' '}
          <span className="text-[#1A3A38] font-semibold">certified metric definitions.</span>
        </p>
      </section>

      {/* ── Query Journey ── */}
      <section className="flex flex-col gap-4">
        <div className="flex items-center gap-3 mb-2">
          <h2
            className="text-2xl font-black text-[#1A3A38] tracking-tight"
            style={{ fontFamily: 'Nunito, sans-serif' }}
          >
            The Query Journey
          </h2>
          <span
            className="text-xs font-bold text-[#0D9488] px-3 py-1 rounded-full"
            style={{
              background: 'rgba(13,148,136,0.08)',
              boxShadow: 'inset 2px 2px 4px rgba(13,148,136,0.1), inset -2px -2px 4px rgba(255,255,255,0.9)',
              fontFamily: 'DM Sans, sans-serif',
            }}
          >
            7 steps
          </span>
        </div>

        {/* Vertical connector line + cards */}
        <div className="relative flex flex-col gap-4">
          {/* Vertical guide line */}
          <div
            className="absolute left-[19px] top-10 bottom-10 w-0.5 rounded-full"
            style={{ background: 'linear-gradient(to bottom, #2DD4BF, rgba(13,148,136,0.1))' }}
          />
          {STEPS.map((step, idx) => (
            <StepCard key={step.num} step={step} index={idx} />
          ))}
        </div>
      </section>

      {/* ── Why This Is Hard ── */}
      <section
        className="rounded-[40px] p-10 flex flex-col gap-8 backdrop-blur-xl"
        style={{
          background: 'rgba(255,255,255,0.55)',
          boxShadow: `30px 30px 60px rgba(13,148,136,0.08), -30px -30px 60px #ffffff, inset 10px 10px 20px rgba(13,148,136,0.04), inset -10px -10px 20px rgba(255,255,255,0.8)`,
        }}
      >
        <div className="flex items-start gap-4">
          <div
            className="w-12 h-12 rounded-full flex items-center justify-center shrink-0 bg-gradient-to-br from-amber-400 to-orange-500"
            style={{ boxShadow: CLAY_BTN_SHADOW }}
          >
            <AlertTriangle size={22} className="text-white" />
          </div>
          <div>
            <h2
              className="text-2xl font-black text-[#1A3A38] tracking-tight mb-1"
              style={{ fontFamily: 'Nunito, sans-serif' }}
            >
              Why This Is Hard
            </h2>
            <p
              className="text-[#4A7B76] text-sm"
              style={{ fontFamily: 'DM Sans, sans-serif' }}
            >
              The failure modes naive NL-to-SQL systems hit — and how the gateway addresses each one.
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
          {HARD_THINGS.map(({ title, desc }) => (
            <div
              key={title}
              className="rounded-[24px] p-6 flex flex-col gap-3"
              style={{
                background: 'rgba(255,255,255,0.65)',
                boxShadow: CLAY_SHADOW,
              }}
            >
              <div className="flex items-center gap-2">
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{ background: 'linear-gradient(135deg, #2DD4BF, #0D9488)' }}
                />
                <h3
                  className="font-black text-[#1A3A38] text-base"
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

      {/* ── Architecture diagram (reuse of pipeline from landing) ── */}
      <section
        className="flex flex-col gap-8 p-10 rounded-[48px] backdrop-blur-xl"
        style={{
          background: 'rgba(255,255,255,0.40)',
          boxShadow: `30px 30px 60px rgba(13,148,136,0.08), -30px -30px 60px #ffffff, inset 10px 10px 20px rgba(13,148,136,0.04), inset -10px -10px 20px rgba(255,255,255,0.8)`,
        }}
      >
        <div className="text-center">
          <h2
            className="text-3xl font-black text-[#1A3A38] mb-2 tracking-tight"
            style={{ fontFamily: 'Nunito, sans-serif' }}
          >
            Architecture Pipeline
          </h2>
          <p
            className="text-[#4A7B76] text-sm"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            The 7 steps mapped onto the full stack — hover a node to explore
          </p>
        </div>

        <div className="pb-4 pt-2 px-2 w-full">
          <div className="flex items-center justify-between gap-1 sm:gap-2 md:gap-3 w-full">
            {PIPELINE.map(({ icon: Icon, label, gradient, step }, idx) => (
              <div key={label} className="flex items-center gap-1 sm:gap-2 md:gap-3 flex-1 min-w-0">
                <div
                  className={`flex flex-col items-center justify-center gap-2
                               w-full py-4 sm:py-5 px-1 sm:px-2 rounded-[24px]
                               transition-all duration-300 backdrop-blur-sm relative
                               hover:-translate-y-2 cursor-default group`}
                  style={{
                    background: step
                      ? 'linear-gradient(135deg, rgba(240,253,250,0.95), rgba(255,255,255,0.98))'
                      : 'rgba(255,255,255,0.65)',
                    boxShadow: step ? CLAY_SHADOW_HOVER : CLAY_SHADOW,
                  }}
                >
                  {/* Step badge */}
                  {step && (
                    <span
                      className="absolute -top-2 -right-1 text-[8px] font-black text-white px-1.5 py-0.5 rounded-full"
                      style={{
                        background: 'linear-gradient(135deg, #2DD4BF, #0D9488)',
                        boxShadow: CLAY_BTN_SHADOW,
                        fontFamily: 'Nunito, sans-serif',
                      }}
                    >
                      {step}
                    </span>
                  )}

                  {/* Icon orb */}
                  <div
                    className={`w-10 h-10 sm:w-12 sm:h-12 rounded-full flex items-center justify-center shrink-0 bg-gradient-to-br ${gradient}`}
                    style={{ boxShadow: CLAY_BTN_SHADOW }}
                  >
                    <Icon size={20} className="text-white sm:w-6 sm:h-6" />
                  </div>
                  <span
                    className={`text-[10px] sm:text-xs text-center leading-tight whitespace-pre-line font-medium
                                ${step ? 'text-[#0D9488] font-bold' : 'text-[#1A3A38]'}`}
                    style={{ fontFamily: 'DM Sans, sans-serif' }}
                  >
                    {label}
                  </span>
                </div>
                {idx < PIPELINE.length - 1 && (
                  <span className="text-[#4A7B76]/40 text-lg sm:text-xl select-none shrink-0 font-light">→</span>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Legend */}
        <div className="flex items-center justify-center gap-6 flex-wrap">
          <div className="flex items-center gap-2">
            <span
              className="w-4 h-4 rounded-full"
              style={{ background: 'linear-gradient(135deg, rgba(240,253,250,0.95), rgba(255,255,255,0.98))', boxShadow: CLAY_SHADOW_HOVER }}
            />
            <span className="text-xs text-[#4A7B76]" style={{ fontFamily: 'DM Sans, sans-serif' }}>
              Steps mapped to this layer
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span
              className="w-4 h-4 rounded-full"
              style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
            />
            <span className="text-xs text-[#4A7B76]" style={{ fontFamily: 'DM Sans, sans-serif' }}>
              Infrastructure layer
            </span>
          </div>
        </div>
      </section>

      {/* ── CTA footer ── */}
      <section
        className="p-10 flex flex-col sm:flex-row items-center justify-between gap-8 mt-2 rounded-[48px] backdrop-blur-xl mb-8"
        style={{
          background: 'rgba(255,255,255,0.65)',
          boxShadow: CLAY_SHADOW,
        }}
      >
        <div>
          <h3
            className="font-black text-[#1A3A38] text-2xl mb-2 tracking-tight"
            style={{ fontFamily: 'Nunito, sans-serif' }}
          >
            Ready to see it live?
          </h3>
          <p
            className="text-[#4A7B76] text-base"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            Run a real governed query or explore the certified metrics behind every answer.
          </p>
        </div>
        <div className="flex items-center gap-4 flex-wrap">
          <Link to="/query" className="btn-primary text-base shrink-0">
            Try a Query <ArrowRight size={18} />
          </Link>
          <Link to="/metrics" className="btn-outline text-base shrink-0">
            Browse Certified Metrics
          </Link>
        </div>
      </section>
    </div>
  );
}
