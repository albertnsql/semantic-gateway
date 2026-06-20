/**
 * pages/HowItWorksPage.jsx
 * Premium product-page layout: full-width alternating step cards,
 * all content always visible, no accordion clicks required.
 */
import { Link } from 'react-router-dom';
import {
  MessageSquare, Brain, Map, Code2, ShieldCheck, Zap, GitBranch,
  ArrowRight, Database, Server, Layers, Shield, Cpu, Monitor,
  AlertTriangle, BookOpen, Terminal,
} from 'lucide-react';

// ── Design tokens ───────────────────────────────────────────────────────────
const S  = `16px 16px 32px rgba(13,148,136,0.12),-10px -10px 24px rgba(255,255,255,0.9),inset 6px 6px 12px rgba(13,148,136,0.04),inset -6px -6px 12px rgba(255,255,255,1)`;
const SH = `20px 20px 40px rgba(13,148,136,0.18),-12px -12px 28px rgba(255,255,255,0.95),inset 6px 6px 12px rgba(13,148,136,0.04),inset -6px -6px 12px rgba(255,255,255,1)`;
const B  = `12px 12px 24px rgba(13,148,136,0.30),-8px -8px 16px rgba(255,255,255,0.4),inset 4px 4px 8px rgba(255,255,255,0.4),inset -4px -4px 8px rgba(0,0,0,0.08)`;
const BG = `30px 30px 60px rgba(13,148,136,0.08),-30px -30px 60px #ffffff,inset 10px 10px 20px rgba(13,148,136,0.04),inset -10px -10px 20px rgba(255,255,255,0.8)`;

// ── Data ────────────────────────────────────────────────────────────────────
const STEPS = [
  {
    num: '01', icon: MessageSquare,
    gradient: 'from-sky-400 to-sky-600', accent: '#0EA5E9',
    tag: 'Input', title: 'Ask in plain English',
    summary: 'You type a question like "What\'s churn by plan type this quarter?" — no SQL knowledge, no schema memorization needed.',
    detail: 'The question is sent as a raw string to the gateway\'s intent classifier endpoint. Gemini 1.5 Flash acts as the first-stage router, embedding your question and comparing it against the known metric vocabulary to decide which handler pipeline to invoke. No SQL is generated at this stage.',
    badge: 'Entry point',
  },
  {
    num: '02', icon: Brain,
    gradient: 'from-violet-400 to-violet-600', accent: '#7C3AED',
    tag: 'Routing', title: 'Classify intent',
    summary: 'The gateway decides if this is a metric question, a schema question, or completely out of scope — before touching any data.',
    detail: 'A two-stage classification pipeline routes to one of three intents: metric_query, schema_question, or out_of_scope. Out-of-scope rejection is a deliberate guardrail — it prevents the LLM from falling back to arbitrary SQL generation when the question doesn\'t map to a certified metric. This is the primary defence against prompt-injection attempts.',
    badge: 'Guardrail',
  },
  {
    num: '03', icon: Map,
    gradient: 'from-amber-400 to-amber-600', accent: '#D97706',
    tag: 'Resolution', title: 'Resolve entities & dimensions',
    summary: 'Plan type, country, cohort — the gateway maps your words to certified fields in the semantic model.',
    detail: 'Entity-prefixed dimension resolution translates user-facing terms (e.g. "plan type") to fully-qualified MetricFlow field names (e.g. subscriber__plan_type). MetricRegistry.load() resolves the allowed_joins set across entities. Unresolvable terms are surfaced as an error rather than silently dropped.',
    badge: 'Semantic mapping',
  },
  {
    num: '04', icon: Code2,
    gradient: 'from-teal-400 to-teal-600', accent: '#0D9488',
    tag: 'Compilation', title: 'Generate governed SQL',
    summary: 'MetricFlow compiles SQL from certified metric definitions — not a freeform LLM guess at your schema.',
    detail: 'The MetricFlow CLI receives the resolved metric name, dimensions, and time grain. It generates SQL that is guaranteed to respect the semantic model\'s grain constraints and join topology. The generated SQL is deterministic for a given metric + dimension combination — no hallucinated joins, no invented aggregations.',
    badge: 'Core guarantee',
  },
  {
    num: '05', icon: ShieldCheck,
    gradient: 'from-emerald-400 to-emerald-600', accent: '#059669',
    tag: 'Validation', title: 'Speculative review',
    summary: 'Before anything runs, a critic pass checks the SQL for hallucinated column names or logic errors.',
    detail: 'A speculative review stage sends the MetricFlow-generated SQL back through Gemini acting as a critic — looking for hallucinated column names, incorrect aggregation functions, or grain mismatches the static compiler might not catch. The reviewer runs against the warehouse schema cache, not live Snowflake, keeping latency low.',
    badge: 'Safety net',
  },
  {
    num: '06', icon: Zap,
    gradient: 'from-orange-400 to-orange-600', accent: '#EA580C',
    tag: 'Execution', title: 'Cache check & execute',
    summary: 'Seen this exact query before? Instant answer. Otherwise the validated SQL runs against Snowflake.',
    detail: 'A two-layer cache is checked: SQLTemplateCache first (parameterized SQL template + dimension set), then a result cache keyed by (metric, dimensions, date_range). The {start_date}/{end_date} parameterization fix was critical — naive caching on full SQL strings meant date-range queries never cache-hit. Parameterized templates reuse across calendar dates.',
    badge: 'Performance',
  },
  {
    num: '07', icon: GitBranch,
    gradient: 'from-cyan-400 to-cyan-600', accent: '#0891B2',
    tag: 'Output', title: 'Return result + lineage',
    summary: 'You get your answer, the SQL that produced it, and a traceable path back to the raw source tables.',
    detail: 'The response payload includes the query result, the rendered SQL, and a lineage graph from the metric back through the dbt semantic model nodes to the raw Snowflake sources. Every node is a certified dbt model or source — auditors can verify the derivation chain without touching code. Queryable in the Lineage Explorer.',
    badge: 'Full traceability',
  },
];

const HARD = [
  { title: 'Grain violations', desc: 'Mixing MRR (subscription+month grain) with session-level data produces silent row count multiplication. MetricFlow\'s grain constraints catch this before SQL executes.' },
  { title: 'Join fanout', desc: 'Direct SQL agents pick the nearest foreign key and join — producing 10× row counts that look right until you compare totals. allowed_joins topology eliminates this.' },
  { title: 'Ambiguous metric phrasing', desc: '"Revenue" means five different things depending on proration, annualization, or recognition method. Certified definitions enforce the canonical calculation.' },
  { title: 'LLM hallucination on schema', desc: 'LLMs confidently generate column names that don\'t exist. The speculative review stage and entity-prefixed resolution catch these before the warehouse sees them.' },
];

const PIPELINE = [
  { icon: Database, label: 'Raw SaaS\nData',            gradient: 'from-sky-400 to-sky-600',          step: null  },
  { icon: Server,   label: 'Snowflake\nDWH',            gradient: 'from-cyan-400 to-cyan-600',        step: null  },
  { icon: Code2,    label: 'dbt\nModels',               gradient: 'from-amber-400 to-amber-600',      step: null  },
  { icon: Layers,   label: 'MetricFlow\nSemantic',      gradient: 'from-teal-400 to-teal-600',        step: '04'  },
  { icon: Shield,   label: 'FastAPI\nGateway',          gradient: 'from-emerald-400 to-emerald-600',  step: '02–03' },
  { icon: Cpu,      label: 'Gemini 1.5\nLLM',           gradient: 'from-teal-400 to-teal-700',        step: '01'  },
  { icon: Monitor,  label: 'React\nFrontend',           gradient: 'from-cyan-400 to-teal-600',        step: '07'  },
];

// ── Divider ─────────────────────────────────────────────────────────────────
function SectionDivider({ label, color = '#0D9488' }) {
  return (
    <div className="flex items-center gap-4 my-2">
      <div className="flex-1 h-px" style={{ background: `linear-gradient(to right, ${color}35, transparent)` }} />
      <span className="text-[10px] font-black tracking-[0.22em] uppercase px-3 py-1 rounded-full"
        style={{ color, background: `${color}12`, fontFamily: 'DM Sans, sans-serif' }}>
        {label}
      </span>
      <div className="flex-1 h-px" style={{ background: `linear-gradient(to left, ${color}35, transparent)` }} />
    </div>
  );
}

// ── Step Card (alternating layout) ───────────────────────────────────────────
function StepCard({ step, flip }) {
  const Icon = step.icon;
  const isFlip = flip;

  const Identity = (
    <div
      className="flex flex-col gap-6 justify-center px-10 py-10 rounded-[36px] relative overflow-hidden h-full min-h-[260px]"
      style={{
        background: `linear-gradient(135deg, ${step.accent}18, ${step.accent}06)`,
        boxShadow: `inset 6px 6px 16px ${step.accent}10, inset -6px -6px 16px rgba(255,255,255,0.7)`,
      }}
    >
      {/* Big watermark number */}
      <span
        className="absolute right-4 bottom-2 font-black leading-none select-none pointer-events-none"
        style={{
          fontSize: '9rem',
          color: `${step.accent}10`,
          fontFamily: 'Nunito, sans-serif',
          lineHeight: 1,
        }}
      >
        {step.num}
      </span>

      {/* Tag chip */}
      <span
        className="self-start text-[10px] font-black tracking-widest uppercase px-3 py-1 rounded-full"
        style={{ color: step.accent, background: `${step.accent}18`, fontFamily: 'DM Sans, sans-serif' }}
      >
        {step.tag}
      </span>

      {/* Icon + step number */}
      <div className="flex items-center gap-4">
        <div
          className={`w-14 h-14 rounded-[20px] flex items-center justify-center shrink-0 bg-gradient-to-br ${step.gradient}`}
          style={{ boxShadow: B }}
        >
          <Icon size={26} className="text-white" />
        </div>
        <span
          className="text-4xl font-black"
          style={{ color: `${step.accent}60`, fontFamily: 'Nunito, sans-serif' }}
        >
          {step.num}
        </span>
      </div>

      {/* Title */}
      <h3
        className="text-2xl font-black text-[#1A3A38] leading-tight"
        style={{ fontFamily: 'Nunito, sans-serif', zIndex: 1 }}
      >
        {step.title}
      </h3>

      {/* Badge */}
      <span
        className="self-start text-[11px] font-bold px-3 py-1 rounded-full"
        style={{ color: '#4A7B76', background: 'rgba(255,255,255,0.65)', boxShadow: S, fontFamily: 'DM Sans, sans-serif' }}
      >
        {step.badge}
      </span>
    </div>
  );

  const Content = (
    <div className="flex flex-col gap-5 justify-center px-2 py-2 h-full">
      {/* Summary */}
      <p
        className="text-[#1A3A38] text-base leading-relaxed font-medium"
        style={{ fontFamily: 'DM Sans, sans-serif' }}
      >
        {step.summary}
      </p>

      {/* Technical detail — always visible */}
      <div
        className="rounded-[20px] p-5 flex flex-col gap-3"
        style={{
          background: 'rgba(240,253,250,0.75)',
          boxShadow: `inset 6px 6px 14px ${step.accent}0d, inset -6px -6px 14px rgba(255,255,255,0.85)`,
        }}
      >
        <div className="flex items-center gap-2">
          <Terminal size={12} style={{ color: step.accent }} />
          <span
            className="text-[9px] font-black tracking-[0.18em] uppercase"
            style={{ color: step.accent, fontFamily: 'JetBrains Mono, monospace' }}
          >
            Implementation detail
          </span>
        </div>
        <p
          className="text-sm text-[#1A3A38] leading-relaxed"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          {step.detail}
        </p>
      </div>
    </div>
  );

  return (
    <div
      className="grid grid-cols-[5fr_7fr] gap-0 rounded-[40px] overflow-hidden transition-all duration-500"
      style={{ background: 'rgba(255,255,255,0.65)', boxShadow: S }}
    >
      {isFlip ? (
        <>
          <div className="p-2">{Content}</div>
          <div>{Identity}</div>
        </>
      ) : (
        <>
          <div>{Identity}</div>
          <div className="p-2">{Content}</div>
        </>
      )}
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────
export default function HowItWorksPage() {
  return (
    <div className="max-w-5xl mx-auto flex flex-col gap-10 animate-fade-in relative z-10 pb-12">

      {/* ══ HERO ══════════════════════════════════════════════════════════ */}
      <section
        className="rounded-[44px] p-12 flex flex-col gap-8 backdrop-blur-xl"
        style={{ background: 'rgba(255,255,255,0.55)', boxShadow: BG }}
      >
        {/* Top label row */}
        <div className="flex items-center gap-3 flex-wrap">
          <div className="inline-flex items-center gap-2 rounded-full px-4 py-1.5"
            style={{ background: 'rgba(13,148,136,0.09)' }}>
            <span className="h-1.5 w-1.5 rounded-full bg-[#0D9488] animate-clay-breathe" />
            <span className="text-[11px] font-black tracking-widest text-[#0D9488] uppercase"
              style={{ fontFamily: 'DM Sans, sans-serif' }}>Architecture Deep-Dive</span>
          </div>
          <span className="text-[#4A7B76]/35">·</span>
          <span className="text-xs text-[#4A7B76] font-medium" style={{ fontFamily: 'DM Sans, sans-serif' }}>
            7 steps · MetricFlow · Gemini 1.5 · Snowflake
          </span>
        </div>

        {/* H1 */}
        <div>
          <h1 className="text-5xl sm:text-6xl font-black tracking-tight leading-[1.06] text-[#1A3A38] mb-4"
            style={{ fontFamily: 'Nunito, sans-serif' }}>
            How It{' '}
            <span className="bg-clip-text text-transparent"
              style={{ backgroundImage: 'linear-gradient(135deg,#0D9488,#2DD4BF,#0891B2)' }}>
              Works
            </span>
          </h1>
          <p className="text-lg font-medium leading-relaxed text-[#4A7B76] max-w-2xl"
            style={{ fontFamily: 'DM Sans, sans-serif' }}>
            From a plain-English question to a governed, lineage-traced answer —
            every step validated against your{' '}
            <span className="text-[#1A3A38] font-semibold">certified metric definitions.</span>
          </p>
        </div>

        {/* Stats row */}
        <div
          className="grid grid-cols-4 gap-0 rounded-[28px] overflow-hidden"
          style={{ background: 'rgba(255,255,255,0.50)', boxShadow: S }}
        >
          {[
            { v: '7',  l: 'Pipeline steps' },
            { v: '3',  l: 'Intent classes' },
            { v: '2',  l: 'Cache layers'   },
            { v: '10', l: 'Certified metrics' },
          ].map(({ v, l }, i) => (
            <div key={l}
              className={`flex flex-col items-center gap-1 py-5 ${i < 3 ? 'border-r border-[#0D9488]/10' : ''}`}>
              <span className="text-3xl font-black text-[#0D9488]"
                style={{ fontFamily: 'Nunito, sans-serif' }}>{v}</span>
              <span className="text-xs text-[#4A7B76] text-center"
                style={{ fontFamily: 'DM Sans, sans-serif' }}>{l}</span>
            </div>
          ))}
        </div>
      </section>

      {/* ══ QUERY JOURNEY ════════════════════════════════════════════════ */}
      <SectionDivider label="The Query Journey" />

      <section className="flex flex-col gap-5">
        {STEPS.map((step, i) => (
          <StepCard key={step.num} step={step} flip={i % 2 !== 0} />
        ))}
      </section>

      {/* ══ WHY THIS IS HARD ══════════════════════════════════════════════ */}
      <SectionDivider label="Why This Is Hard" color="#D97706" />

      <section
        className="rounded-[40px] p-10 backdrop-blur-xl"
        style={{ background: 'rgba(255,255,255,0.55)', boxShadow: BG }}
      >
        {/* Callout */}
        <div
          className="rounded-[24px] px-7 py-5 mb-7 flex items-start gap-4"
          style={{ background: 'rgba(254,243,199,0.65)',
            boxShadow: '10px 10px 22px rgba(245,158,11,0.08),-8px -8px 16px rgba(255,255,255,0.9)' }}
        >
          <div className="w-10 h-10 rounded-full flex items-center justify-center shrink-0 bg-gradient-to-br from-amber-400 to-orange-500 mt-0.5"
            style={{ boxShadow: B }}>
            <AlertTriangle size={18} className="text-white" />
          </div>
          <p className="text-sm text-[#92400E] leading-relaxed"
            style={{ fontFamily: 'DM Sans, sans-serif' }}>
            These are the failure modes naive NL-to-SQL hits in production — silent wrong answers,
            not loud errors. The gateway addresses each one structurally, not with prompt engineering.
          </p>
        </div>

        {/* 2×2 grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
          {HARD.map(({ title, desc }, i) => (
            <div key={title}
              className="rounded-[28px] p-7 flex flex-col gap-3 backdrop-blur-xl"
              style={{ background: 'rgba(255,255,255,0.70)', boxShadow: S }}>
              <div className="flex items-center gap-3">
                <span className="w-7 h-7 rounded-full flex items-center justify-center text-white text-xs font-black shrink-0"
                  style={{
                    background: 'linear-gradient(135deg,#F59E0B,#D97706)',
                    boxShadow: '6px 6px 12px rgba(245,158,11,0.25),-4px -4px 8px rgba(255,255,255,0.4)',
                    fontFamily: 'Nunito, sans-serif',
                  }}>
                  {i + 1}
                </span>
                <h3 className="font-black text-[#1A3A38] text-base"
                  style={{ fontFamily: 'Nunito, sans-serif' }}>{title}</h3>
              </div>
              <p className="text-[#4A7B76] text-sm leading-relaxed"
                style={{ fontFamily: 'DM Sans, sans-serif' }}>{desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ══ ARCHITECTURE PIPELINE ════════════════════════════════════════ */}
      <SectionDivider label="Architecture Pipeline" />

      <section
        className="rounded-[40px] p-10 backdrop-blur-xl flex flex-col gap-7"
        style={{ background: 'rgba(255,255,255,0.40)', boxShadow: BG }}
      >
        <p className="text-sm text-[#4A7B76] max-w-xl" style={{ fontFamily: 'DM Sans, sans-serif' }}>
          The 7 steps mapped onto the full stack. Highlighted nodes are where the gateway's
          core logic lives — highlighted with the step numbers.
        </p>

        <div className="overflow-x-auto pb-2">
          <div className="flex items-end gap-2 min-w-[560px]">
            {PIPELINE.map(({ icon: Icon, label, gradient, step }, idx) => (
              <div key={label} className="flex items-center gap-2 flex-1 min-w-0">
                <div className="flex flex-col items-center gap-2 flex-1">
                  {/* Badge above */}
                  <div className="h-7 flex items-center justify-center">
                    {step ? (
                      <span className="text-[9px] font-black text-white px-2 py-0.5 rounded-full"
                        style={{ background: 'linear-gradient(135deg,#2DD4BF,#0D9488)', fontFamily: 'Nunito, sans-serif' }}>
                        Step {step}
                      </span>
                    ) : (
                      <span className="text-[9px] text-[#4A7B76]/40 font-medium"
                        style={{ fontFamily: 'DM Sans, sans-serif' }}>infra</span>
                    )}
                  </div>
                  {/* Node */}
                  <div
                    className="flex flex-col items-center gap-2.5 w-full py-5 px-2 rounded-[20px] transition-all duration-300 hover:-translate-y-1"
                    style={{
                      background: step
                        ? 'linear-gradient(135deg,rgba(240,253,250,0.95),rgba(255,255,255,0.98))'
                        : 'rgba(255,255,255,0.65)',
                      boxShadow: step ? SH : S,
                    }}
                  >
                    <div className={`w-10 h-10 rounded-full flex items-center justify-center bg-gradient-to-br ${gradient}`}
                      style={{ boxShadow: B }}>
                      <Icon size={18} className="text-white" />
                    </div>
                    <span
                      className={`text-[10px] text-center leading-tight whitespace-pre-line font-semibold ${step ? 'text-[#0D9488]' : 'text-[#1A3A38]'}`}
                      style={{ fontFamily: 'DM Sans, sans-serif' }}
                    >{label}</span>
                  </div>
                </div>
                {idx < PIPELINE.length - 1 && (
                  <span className="text-[#4A7B76]/30 text-xl select-none shrink-0 mb-2">→</span>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Legend */}
        <div className="flex items-center gap-6 flex-wrap pt-2 border-t border-[#0D9488]/10">
          <div className="flex items-center gap-2">
            <span className="w-3 h-3 rounded-full"
              style={{ background: 'linear-gradient(135deg,#2DD4BF,#0D9488)' }} />
            <span className="text-xs text-[#4A7B76]" style={{ fontFamily: 'DM Sans, sans-serif' }}>
              Gateway logic — steps mapped here
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-3 h-3 rounded-full bg-white/65"
              style={{ border: '1px solid rgba(13,148,136,0.15)' }} />
            <span className="text-xs text-[#4A7B76]" style={{ fontFamily: 'DM Sans, sans-serif' }}>
              Infrastructure layer
            </span>
          </div>
        </div>
      </section>

      {/* ══ CTA FOOTER ════════════════════════════════════════════════════ */}
      <section
        className="rounded-[40px] overflow-hidden backdrop-blur-xl"
        style={{ boxShadow: S }}
      >
        <div className="h-1 w-full"
          style={{ background: 'linear-gradient(90deg,#2DD4BF,#0D9488,#0891B2)' }} />
        <div className="p-10 flex flex-col sm:flex-row items-center justify-between gap-8"
          style={{ background: 'rgba(255,255,255,0.70)' }}>
          <div>
            <div className="flex items-center gap-2 mb-2">
              <BookOpen size={15} className="text-[#0D9488]" />
              <span className="text-[11px] font-black tracking-widest text-[#0D9488] uppercase"
                style={{ fontFamily: 'DM Sans, sans-serif' }}>Ready to try it?</span>
            </div>
            <h3 className="font-black text-[#1A3A38] text-2xl mb-1 tracking-tight"
              style={{ fontFamily: 'Nunito, sans-serif' }}>See the architecture in action</h3>
            <p className="text-[#4A7B76] text-sm" style={{ fontFamily: 'DM Sans, sans-serif' }}>
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
