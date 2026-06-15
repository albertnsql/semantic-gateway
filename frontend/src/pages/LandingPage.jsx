/**
 * pages/LandingPage.jsx — Overview / hero page. Claymorphism theme.
 */
import { Link } from 'react-router-dom';
import {
  Database, Server, Code2, Layers, Shield, Cpu, Monitor,
  XCircle, CheckCircle2, ArrowRight,
} from 'lucide-react';
import TopBar from '../components/TopBar';

const PIPELINE = [
  { icon: Database, label: 'Raw SaaS Data',              gradient: 'from-sky-400 to-sky-600',     featured: false },
  { icon: Server,   label: 'Snowflake DWH',              gradient: 'from-cyan-400 to-cyan-600',   featured: false },
  { icon: Code2,    label: 'dbt Models',                 gradient: 'from-amber-400 to-amber-600', featured: false },
  { icon: Layers,   label: 'MetricFlow\nSemantic Layer', gradient: 'from-teal-400 to-teal-600',   featured: true  },
  { icon: Shield,   label: 'FastAPI\nGateway',           gradient: 'from-emerald-400 to-emerald-600', featured: false },
  { icon: Cpu,      label: 'Gemini 1.5\nLLM',               gradient: 'from-teal-400 to-teal-700',   featured: false },
  { icon: Monitor,  label: 'React\nFrontend',            gradient: 'from-cyan-400 to-teal-600',   featured: false },
];

const PREVENTS = [
  {
    title: 'Hallucinated Joins',
    desc:  'Direct SQL agents misuse foreign keys and join at wrong grains, producing silent phantom row counts.',
  },
  {
    title: 'Metric Misuse',
    desc:  'LLMs ignore certified definitions and invent their own calculations — e.g. SUM revenue without prorating.',
  },
  {
    title: 'Grain Violations',
    desc:  'Mixing MRR (subscription+month grain) with session-level data causes fanout multiplication with no error.',
  },
];

const GUARANTEES = [
  {
    title: 'Certified Metrics Only',
    desc:  '10 MetricFlow-certified metrics exposed through the gateway. No ad-hoc column references allowed.',
  },
  {
    title: 'Grain-Aware Validation',
    desc:  'Every query validated against the semantic model grain before execution. Cross-grain joins are blocked.',
  },
  {
    title: 'Full Lineage Tracing',
    desc:  'Every response includes upstream lineage from raw source tables to the certified metric node.',
  },
];

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_SHADOW_HOVER = `20px 20px 40px rgba(13,148,136,0.18), -12px -12px 28px rgba(255,255,255,0.95), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_BTN_SHADOW = `12px 12px 24px rgba(13,148,136,0.30), -8px -8px 16px rgba(255,255,255,0.4), inset 4px 4px 8px rgba(255,255,255,0.4), inset -4px -4px 8px rgba(0,0,0,0.08)`;

export default function LandingPage() {
  return (
    <div className="max-w-5xl mx-auto flex flex-col gap-14 animate-fade-in relative z-10">
      {/* ── Hero ── */}
      <section className="flex flex-col items-center text-center gap-8 pt-2 relative">
        {/* Tag line */}
        <div
          className="inline-flex items-center gap-2 rounded-full px-5 py-2 backdrop-blur-sm"
          style={{
            background: 'rgba(255,255,255,0.70)',
            boxShadow: CLAY_SHADOW,
          }}
        >
          <span
            className="h-2 w-2 rounded-full bg-[#0D9488] animate-clay-breathe"
          />
          <span
            className="text-xs font-bold tracking-widest text-[#0D9488] uppercase"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            Production-Grade · MetricFlow · Gemini 1.5 · Snowflake
          </span>
        </div>

        {/* Main headline */}
        <h1
          className="text-6xl sm:text-7xl font-black tracking-tight leading-[1.1] text-[#1A3A38]"
          style={{ fontFamily: 'Nunito, sans-serif' }}
        >
          AI That Knows
          <br />
          <span
            className="bg-clip-text text-transparent"
            style={{ backgroundImage: 'linear-gradient(135deg, #0D9488, #2DD4BF, #0891B2)' }}
          >
            Its&nbsp;Grain
          </span>
        </h1>

        {/* Subheadline */}
        <p
          className="max-w-2xl text-xl font-medium leading-relaxed text-[#4A7B76]"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          A governed semantic layer between natural language queries and your Snowflake warehouse.
          <br className="hidden sm:block" />
          <span className="text-[#1A3A38] font-semibold"> No hallucinated joins. No metric misuse. No grain violations.</span>
        </p>

        {/* CTA buttons */}
        <div className="flex items-center gap-5 flex-wrap justify-center mt-2">
          <Link to="/query" className="btn-primary text-base">
            Try a Query <ArrowRight size={18} />
          </Link>
          <Link to="/metrics" className="btn-outline text-base">
            Browse Metrics
          </Link>
          <Link
            to="/demo"
            className="flex items-center gap-2 text-[#4A7B76] hover:text-[#0D9488] text-sm font-medium transition-colors mt-2 sm:mt-0 ml-2"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            View Demo Scenarios →
          </Link>
        </div>
      </section>

      {/* ── Architecture pipeline ── */}
      <section
        className="flex flex-col gap-10 mt-4 p-10 rounded-[48px] backdrop-blur-xl"
        style={{
          background: 'rgba(255,255,255,0.40)',
          boxShadow: `30px 30px 60px rgba(13,148,136,0.08), -30px -30px 60px #ffffff, inset 10px 10px 20px rgba(13,148,136,0.04), inset -10px -10px 20px rgba(255,255,255,0.8)`,
        }}
      >
        <div className="text-center">
          <h2
            className="text-4xl font-black text-[#1A3A38] mb-2 tracking-tight"
            style={{ fontFamily: 'Nunito, sans-serif' }}
          >
            How It Works
          </h2>
          <p className="text-[#4A7B76] text-sm" style={{ fontFamily: 'DM Sans, sans-serif' }}>
            Natural language in → governed, lineage-traced results out
          </p>
        </div>

        <div className="pb-4 pt-2 px-2 w-full">
          <div className="flex items-center justify-between gap-1 sm:gap-2 md:gap-3 w-full">
            {PIPELINE.map(({ icon: Icon, label, gradient, featured }, idx) => (
              <div key={label} className="flex items-center gap-1 sm:gap-2 md:gap-3 flex-1 min-w-0">
                <div
                  className={`flex flex-col items-center justify-center gap-3
                               w-full py-4 sm:py-5 px-1 sm:px-2 rounded-[24px]
                               transition-all duration-300 backdrop-blur-sm
                               ${featured ? 'scale-105 animate-clay-breathe' : 'hover:-translate-y-2 cursor-pointer'}`}
                  style={{
                    background: featured
                      ? 'linear-gradient(135deg, rgba(240,253,250,0.95), rgba(255,255,255,0.98))'
                      : 'rgba(255,255,255,0.65)',
                    boxShadow: featured ? CLAY_SHADOW_HOVER : CLAY_SHADOW,
                  }}
                >
                  {/* Icon orb */}
                  <div
                    className={`w-10 h-10 sm:w-12 sm:h-12 rounded-full flex items-center justify-center shrink-0 bg-gradient-to-br ${gradient}`}
                    style={{ boxShadow: CLAY_BTN_SHADOW }}
                  >
                    <Icon size={20} className="text-white sm:w-6 sm:h-6" />
                  </div>
                  <span
                    className={`text-[10px] sm:text-xs text-center leading-tight whitespace-pre-line font-medium
                                ${featured ? 'text-[#0D9488] font-bold' : 'text-[#1A3A38]'}`}
                    style={{ fontFamily: 'DM Sans, sans-serif' }}
                  >
                    {label}
                  </span>
                  {featured && (
                    <span
                      className="text-[8px] sm:text-[9px] font-bold text-[#0D9488] uppercase tracking-widest
                                 px-2 py-0.5 rounded-full mt-auto"
                      style={{
                        background: 'rgba(13,148,136,0.08)',
                        boxShadow: 'inset 2px 2px 4px rgba(13,148,136,0.1), inset -2px -2px 4px rgba(255,255,255,0.9)',
                        fontFamily: 'Nunito, sans-serif',
                      }}
                    >
                      Key Layer
                    </span>
                  )}
                </div>
                {idx < PIPELINE.length - 1 && (
                  <span className="text-[#4A7B76]/40 text-lg sm:text-xl select-none shrink-0 font-light">→</span>
                )}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── What it prevents ── */}
      <section className="flex flex-col gap-10">
        <h2
          className="text-3xl sm:text-4xl font-black text-[#1A3A38] tracking-tight"
          style={{ fontFamily: 'Nunito, sans-serif' }}
        >
          What the Gateway&nbsp;
          <span className="text-[#F43F5E]">Prevents</span>
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-8">
          {PREVENTS.map(({ title, desc }) => (
            <div
              key={title}
              className="card-hover p-8 flex flex-col gap-6 rounded-[32px]"
            >
              <div className="flex items-start gap-5">
                <div
                  className="w-14 h-14 rounded-full flex items-center justify-center shrink-0 bg-gradient-to-br from-rose-400 to-rose-600"
                  style={{ boxShadow: CLAY_BTN_SHADOW }}
                >
                  <XCircle size={26} className="text-white" />
                </div>
                <h3
                  className="font-black text-[#1A3A38] text-xl leading-tight pt-1"
                  style={{ fontFamily: 'Nunito, sans-serif' }}
                >
                  {title}
                </h3>
              </div>
              <p
                className="text-[#4A7B76] text-base leading-relaxed"
                style={{ fontFamily: 'DM Sans, sans-serif' }}
              >
                {desc}
              </p>
            </div>
          ))}
        </div>
      </section>

      {/* ── What it guarantees ── */}
      <section className="flex flex-col gap-10">
        <h2
          className="text-3xl sm:text-4xl font-black text-[#1A3A38] tracking-tight"
          style={{ fontFamily: 'Nunito, sans-serif' }}
        >
          What the Gateway&nbsp;
          <span className="text-emerald-500">Guarantees</span>
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-8">
          {GUARANTEES.map(({ title, desc }) => (
            <div
              key={title}
              className="card-hover p-8 flex flex-col gap-6 rounded-[32px]"
            >
              <div className="flex items-start gap-5">
                <div
                  className="w-14 h-14 rounded-full flex items-center justify-center shrink-0 bg-gradient-to-br from-emerald-400 to-emerald-600"
                  style={{ boxShadow: CLAY_BTN_SHADOW }}
                >
                  <CheckCircle2 size={26} className="text-white" />
                </div>
                <h3
                  className="font-black text-[#1A3A38] text-xl leading-tight pt-1"
                  style={{ fontFamily: 'Nunito, sans-serif' }}
                >
                  {title}
                </h3>
              </div>
              <p
                className="text-[#4A7B76] text-base leading-relaxed"
                style={{ fontFamily: 'DM Sans, sans-serif' }}
              >
                {desc}
              </p>
            </div>
          ))}
        </div>
      </section>

      {/* ── Bottom CTA ── */}
      <section
        className="p-10 flex flex-col sm:flex-row items-center justify-between gap-8 mt-4 rounded-[48px] backdrop-blur-xl"
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
            See it in action
          </h3>
          <p
            className="text-[#4A7B76] text-base"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            Three live scenarios — valid query, grain rejection, lineage trace.
          </p>
        </div>
        <Link to="/demo" className="btn-primary text-base shrink-0">
          Run Demo Scenarios <ArrowRight size={18} />
        </Link>
      </section>
    </div>
  );
}
