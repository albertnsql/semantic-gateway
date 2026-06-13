/**
 * pages/DemoScenariosPage.jsx — Claymorphism demo scenarios page.
 */
import { useState } from 'react';
import { Play, Clock, KeyRound } from 'lucide-react';
import TopBar from '../components/TopBar';
import LoadingSpinner from '../components/LoadingSpinner';
import QueryResultPanel from '../components/QueryResultPanel';
import { postQuery } from '../api/query';

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_BTN   = `12px 12px 24px rgba(13,148,136,0.30), -8px -8px 16px rgba(255,255,255,0.4), inset 4px 4px 8px rgba(255,255,255,0.4), inset -4px -4px 8px rgba(0,0,0,0.08)`;

function isApiKeyError(err) {
  const msg = err?.response?.data?.message ?? err?.message ?? '';
  return (
    msg.toLowerCase().includes('invalid_api_key') ||
    msg.toLowerCase().includes('incorrect api key') ||
    (err?.response?.status === 400 && msg.toLowerCase().includes('intent_extraction_failed'))
  );
}

const SCENARIOS = [
  {
    id: 'valid-query',
    title: 'Scenario 1 — Valid Query',
    query: 'What is the MRR by plan type for the last 3 months?',
    expected: '200 OK — MRR values per plan type with governance block and lineage trace',
    accentColor: '#0D9488',
    accentBg: 'rgba(13,148,136,0.06)',
    badgeGradient: 'from-teal-400 to-teal-600',
    badgeLabel: 'SUCCESS',
    btnVariant: 'primary',
    defaultTab: 'Results',
  },
  {
    id: 'grain-mismatch',
    title: 'Scenario 2 — Grain Mismatch Caught',
    query: 'Show me MRR and average completion rate by subscriber',
    expected: '422 Rejected — MRR grain (subscription+month) cannot combine with engagement_rate grain (session_id)',
    accentColor: '#F43F5E',
    accentBg: 'rgba(244,63,94,0.06)',
    badgeGradient: 'from-rose-400 to-rose-600',
    badgeLabel: 'REJECTED',
    btnVariant: 'outline-red',
    defaultTab: 'Results',
  },
  {
    id: 'ltv-lineage',
    title: 'Scenario 3 — Lineage Trace',
    query: 'What is the lifetime value by acquisition channel?',
    expected: '200 OK (or 422) — Governance block shows lineage: raw.payments → stg_payments → fct_payments → ltv',
    accentColor: '#F59E0B',
    accentBg: 'rgba(245,158,11,0.06)',
    badgeGradient: 'from-amber-400 to-amber-600',
    badgeLabel: 'LINEAGE',
    btnVariant: 'outline-amber',
    defaultTab: 'Lineage',
  },
];

function ScenarioCard({ scenario }) {
  const [loading, setLoading]     = useState(false);
  const [response, setResponse]   = useState(null);
  const [error, setError]         = useState(null);
  const [lastRun, setLastRun]     = useState(null);
  const [apiKeyErr, setApiKeyErr] = useState(false);

  const handleRun = async () => {
    setLoading(true);
    setResponse(null);
    setError(null);
    setApiKeyErr(false);
    try {
      const data = await postQuery(scenario.query, [], {
        include_sql: true, include_lineage: true, dry_run: false, max_rows: 1000,
      });
      setResponse(data);
    } catch (err) {
      if (err.response?.data?.status === 'rejected') {
        setResponse(err.response.data);
      } else {
        if (isApiKeyError(err)) setApiKeyErr(true);
        else setError(err);
      }
    } finally {
      setLoading(false);
      setLastRun(new Date().toLocaleTimeString());
    }
  };

  return (
    <div
      className="rounded-[32px] overflow-hidden backdrop-blur-xl transition-all duration-500 hover:-translate-y-1"
      style={{ background: 'rgba(255,255,255,0.65)', boxShadow: CLAY_SHADOW }}
    >
      {/* Colored accent bar */}
      <div className="h-1.5" style={{ background: `linear-gradient(90deg, ${scenario.accentColor}, transparent)` }} />

      {/* Header */}
      <div className="px-7 py-6 flex flex-col gap-4">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-3">
            {/* Badge orb */}
            <div
              className={`flex items-center justify-center w-8 h-8 rounded-full bg-gradient-to-br ${scenario.badgeGradient}`}
              style={{ boxShadow: CLAY_BTN }}
            >
              <span className="text-[9px] font-black text-white tracking-widest" style={{ fontFamily: 'Nunito, sans-serif' }}>
                {scenario.badgeLabel.slice(0,1)}
              </span>
            </div>
            <h2
              className="font-black text-[#1A3A38] text-lg"
              style={{ fontFamily: 'Nunito, sans-serif' }}
            >
              {scenario.title}
            </h2>
          </div>
          <div className="flex items-center gap-3">
            {lastRun && (
              <span className="flex items-center gap-1.5 text-xs text-[#4A7B76]" style={{ fontFamily: 'DM Sans, sans-serif' }}>
                <Clock size={11} /> Last run: {lastRun}
              </span>
            )}
            <button
              id={`run-scenario-${scenario.id}`}
              onClick={handleRun}
              disabled={loading}
              className={scenario.btnVariant === 'primary' ? 'btn-primary' : scenario.btnVariant === 'outline-red' ? 'btn-outline-red' : 'btn-outline-amber'}
            >
              {loading
                ? <><span className="w-3.5 h-3.5 rounded-full border-2 border-current/30 border-t-current animate-spin" />Running…</>
                : <><Play size={13} />Run Scenario</>
              }
            </button>
          </div>
        </div>

        {/* Query display */}
        <div className="flex flex-col gap-2">
          <span className="text-xs text-[#4A7B76] font-bold uppercase tracking-wide" style={{ fontFamily: 'DM Sans, sans-serif' }}>Query</span>
          <blockquote
            className="px-5 py-4 rounded-[20px] text-[#1A3A38] text-sm italic leading-relaxed backdrop-blur-xl"
            style={{
              background: scenario.accentBg,
              boxShadow: 'inset 4px 4px 8px rgba(13,148,136,0.06), inset -4px -4px 8px rgba(255,255,255,0.9)',
              fontFamily: 'DM Sans, sans-serif',
            }}
          >
            "{scenario.query}"
          </blockquote>
        </div>

        {/* Expected */}
        <div className="flex flex-col gap-1">
          <span className="text-xs text-[#4A7B76] font-bold uppercase tracking-wide" style={{ fontFamily: 'DM Sans, sans-serif' }}>Expected Outcome</span>
          <p className="text-[#4A7B76] text-sm" style={{ fontFamily: 'DM Sans, sans-serif' }}>{scenario.expected}</p>
        </div>
      </div>

      {/* Result area */}
      {(loading || response || error || apiKeyErr) && (
        <div
          className="px-7 py-6"
          style={{ borderTop: '1px solid rgba(13,148,136,0.08)', background: 'rgba(255,255,255,0.30)' }}
        >
          {loading && <LoadingSpinner label="Calling semantic gateway…" />}
          {!loading && apiKeyErr && (
            <div
              className="flex items-start gap-4 p-5 rounded-[24px] backdrop-blur-xl"
              style={{ background: 'rgba(245,158,11,0.06)', boxShadow: '8px 8px 16px rgba(245,158,11,0.08), -6px -6px 12px rgba(255,255,255,0.9)' }}
            >
              <KeyRound size={16} className="text-[#F59E0B] shrink-0 mt-0.5" />
              <div>
                <p className="text-[#1A3A38] font-bold text-sm" style={{ fontFamily: 'Nunito, sans-serif' }}>LLM API Key Required</p>
                <p className="text-[#4A7B76] text-xs mt-1 leading-relaxed" style={{ fontFamily: 'DM Sans, sans-serif' }}>
                  Add a real key to <code className="font-mono text-[#F59E0B] px-1" style={{ background: 'rgba(245,158,11,0.10)', borderRadius: '8px' }}>gateway/.env</code> and restart.
                  <br />
                  <span className="text-[#4A7B76] mt-1 block">
                    Use <code className="px-1.5" style={{ background: 'rgba(255,255,255,0.80)', borderRadius: '8px', boxShadow: CLAY_SHADOW }}>OPENAI_API_KEY=gsk_...</code> and{' '}
                    <code className="px-1.5" style={{ background: 'rgba(255,255,255,0.80)', borderRadius: '8px', boxShadow: CLAY_SHADOW }}>LLM_BASE_URL=https://api.groq.com/openai/v1</code>
                  </span>
                </p>
              </div>
            </div>
          )}
          {!loading && !apiKeyErr && (response || error) && (
            <QueryResultPanel
              response={response}
              error={error}
              defaultTab={scenario.defaultTab}
            />
          )}
        </div>
      )}
    </div>
  );
}

export default function DemoScenariosPage() {
  return (
    <div className="max-w-4xl mx-auto flex flex-col gap-6 animate-fade-in">
      <TopBar title="Demo Scenarios" breadcrumb={['Gateway', 'Demo']} />

      <p className="text-[#4A7B76] text-sm -mt-4" style={{ fontFamily: 'DM Sans, sans-serif' }}>
        Three live scenarios demonstrating the gateway's core value propositions.
        Each scenario calls{' '}
        <code
          className="text-[#0D9488] font-mono text-xs px-2 py-0.5 rounded-[10px]"
          style={{ background: 'rgba(13,148,136,0.08)', boxShadow: 'inset 2px 2px 4px rgba(13,148,136,0.08), inset -2px -2px 4px rgba(255,255,255,0.9)' }}
        >
          POST /api/v1/query
        </code>{' '}
        independently.
      </p>

      {SCENARIOS.map((s) => (
        <ScenarioCard key={s.id} scenario={s} />
      ))}
    </div>
  );
}
