/**
 * pages/QueryPage.jsx — Chat-style interactive query interface with Claymorphism.
 */
import { useState, useRef, useEffect } from 'react';
import { Send, RotateCcw, KeyRound, ExternalLink } from 'lucide-react';
import TopBar from '../components/TopBar';
import LoadingSpinner from '../components/LoadingSpinner';
import QueryResultPanel from '../components/QueryResultPanel';
import { postQuery } from '../api/query';

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_INSET  = `inset 8px 8px 16px rgba(13,148,136,0.08), inset -8px -8px 16px rgba(255,255,255,0.9)`;

const EXAMPLE_QUERIES = [
  'What is the MRR by plan type for the last 3 months?',
  'Show me total subscribers by plan type',
  'What is the churn rate this quarter?',
  'Show me LTV by payment method',
];

function isApiKeyError(err) {
  const msg = err?.response?.data?.message ?? err?.message ?? '';
  return (
    msg.toLowerCase().includes('invalid_api_key') ||
    msg.toLowerCase().includes('incorrect api key') ||
    msg.toLowerCase().includes('401') ||
    (err?.response?.status === 400 && msg.toLowerCase().includes('intent_extraction_failed'))
  );
}

function ApiKeyBanner() {
  return (
    <div
      className="flex items-start gap-4 p-6 rounded-[32px] backdrop-blur-xl"
      style={{ background: 'rgba(245,158,11,0.06)', boxShadow: '8px 8px 20px rgba(245,158,11,0.08), -6px -6px 16px rgba(255,255,255,0.85)' }}
    >
      <div
        className="w-10 h-10 rounded-full flex items-center justify-center shrink-0 mt-0.5"
        style={{ background: 'linear-gradient(135deg, #FCD34D, #F59E0B)', boxShadow: CLAY_SHADOW }}
      >
        <KeyRound size={16} className="text-white" />
      </div>
      <div className="flex flex-col gap-1.5">
        <p className="text-[#1A3A38] font-bold text-sm" style={{ fontFamily: 'Nunito, sans-serif' }}>
          LLM API Key Required
        </p>
        <p className="text-[#4A7B76] text-xs leading-relaxed" style={{ fontFamily: 'DM Sans, sans-serif' }}>
          The query interface needs a real LLM API key to extract intent from natural language.
          Add it to <code className="font-mono text-[#F59E0B] px-1 rounded-[8px]" style={{ background: 'rgba(245,158,11,0.12)' }}>gateway/.env</code> then restart the backend.
        </p>
        <div className="flex items-center gap-3 mt-1 flex-wrap">
          <code
            className="text-xs font-mono text-[#4A7B76] px-3 py-1.5 rounded-[16px] whitespace-nowrap backdrop-blur-xl"
            style={{ background: 'rgba(255,255,255,0.80)', boxShadow: CLAY_SHADOW }}
          >
            OPENAI_API_KEY=gsk_your_real_groq_key
          </code>
          <code
            className="text-xs font-mono text-[#4A7B76] px-3 py-1.5 rounded-[16px] whitespace-nowrap backdrop-blur-xl"
            style={{ background: 'rgba(255,255,255,0.80)', boxShadow: CLAY_SHADOW }}
          >
            LLM_BASE_URL=https://api.groq.com/openai/v1
          </code>
          <a
            href="https://console.groq.com/keys"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-xs text-[#F59E0B] hover:text-[#D97706] font-medium transition-colors whitespace-nowrap"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            Get Free Groq Key <ExternalLink size={11} />
          </a>
        </div>
        <p className="text-[#4A7B76] text-xs mt-1" style={{ fontFamily: 'DM Sans, sans-serif' }}>
          Tip: The Metrics Catalog and Lineage Explorer pages work without an API key.
        </p>
      </div>
    </div>
  );
}

export default function QueryPage() {
  const [queryText, setQueryText]           = useState(() => sessionStorage.getItem('query_page_input') || '');
  const [includeSql, setIncludeSql]         = useState(true);
  const [includeLineage, setIncludeLineage] = useState(true);
  const [dryRun, setDryRun]                 = useState(false);
  const [maxRows, setMaxRows]               = useState(100);
  const [loading, setLoading]               = useState(false);
  const [history, setHistory]               = useState(() => {
    try {
      const saved = sessionStorage.getItem('query_page_history');
      if (saved) return JSON.parse(saved);
    } catch (e) {}
    return [];
  });
  
  useEffect(() => {
    sessionStorage.setItem('query_page_input', queryText);
  }, [queryText]);

  useEffect(() => {
    sessionStorage.setItem('query_page_history', JSON.stringify(history));
  }, [history]);
  
  const threadContainerRef = useRef(null);
  const inputRef = useRef(null);

  // Auto-scroll to bottom of the thread area when history changes
  useEffect(() => {
    if (threadContainerRef.current) {
      setTimeout(() => {
        threadContainerRef.current.scrollTo({
          top: threadContainerRef.current.scrollHeight,
          behavior: 'smooth'
        });
      }, 100);
    }
  }, [history]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!queryText.trim() || loading) return;

    const currentQuery = queryText;
    const historyId = Date.now();

    setHistory(prev => [...prev, {
      id: historyId,
      query: currentQuery,
      loading: true,
      response: null,
      error: null,
      showApiKeyHelp: false
    }]);

    setQueryText('');
    setLoading(true);

    try {
      const data = await postQuery(currentQuery, [], {
        include_sql:     includeSql,
        include_lineage: includeLineage,
        dry_run:         dryRun,
        max_rows:        maxRows,
      });
      setHistory(prev => prev.map(item => item.id === historyId ? { ...item, loading: false, response: data } : item));
    } catch (err) {
      if (err.response?.data?.status === 'rejected') {
        setHistory(prev => prev.map(item => item.id === historyId ? { ...item, loading: false, response: err.response.data } : item));
      } else {
        const needsApiKey = isApiKeyError(err);
        setHistory(prev => prev.map(item => item.id === historyId ? { ...item, loading: false, error: err, showApiKeyHelp: needsApiKey } : item));
      }
    } finally {
      setLoading(false);
    }
  };

  const handleReset = () => {
    setHistory([]);
    setQueryText('');
    sessionStorage.removeItem('query_page_history');
    sessionStorage.removeItem('query_page_input');
    inputRef.current?.focus();
  };

  return (
    <div className="absolute inset-0 flex flex-col pointer-events-none">
      
      {/* Top panel (scrollable thread area) */}
      <div className="flex-1 overflow-y-auto px-8 pointer-events-auto" ref={threadContainerRef}>
        <div className="max-w-4xl mx-auto pt-6 pb-12 flex flex-col min-h-full">
          <TopBar title="Query Interface" breadcrumb={['Gateway', 'Query']} />

          <div className="flex-1 flex flex-col mt-8">
            {history.length === 0 && !loading && (
              <div className="m-auto text-center text-neu-muted font-dm max-w-2xl">
                <p className="mb-8 text-[#1A3A38] text-[22px] font-bold tracking-tight" style={{ fontFamily: 'Nunito, sans-serif' }}>
                  Ask a natural language query to begin.
                </p>
                <div className="flex flex-wrap justify-center gap-3 animate-fade-in">
                  {EXAMPLE_QUERIES.map((q) => (
                    <button
                      key={q}
                      type="button"
                      onClick={() => {
                        setQueryText(q);
                        inputRef.current?.focus();
                      }}
                      className="text-sm px-5 py-2.5 rounded-full text-[#4A7B76] 
                                 hover:text-[#0D9488] hover:-translate-y-0.5
                                 font-medium transition-all duration-200 backdrop-blur-xl"
                      style={{ background: 'rgba(255,255,255,0.70)', boxShadow: CLAY_SHADOW, fontFamily: 'DM Sans, sans-serif' }}
                    >
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {history.length > 0 && (
              <div className="flex flex-col">
                {history.map((item, index) => {
                  const isMostRecent = index === history.length - 1;
                  return (
                    <div key={item.id} className={`flex flex-col gap-6 pb-10 ${index !== 0 ? 'pt-10 border-t-[0.5px] border-[#0D9488]/10' : 'pt-2'}`}>
                      
                      {/* User message bubble */}
                      <div 
                        className="self-end max-w-[75%] px-6 py-4 rounded-[32px] rounded-br-[8px]" 
                        style={{ background: 'linear-gradient(135deg, #2DD4BF, #0D9488)', boxShadow: CLAY_SHADOW }}
                      >
                        <p className="text-[15px] leading-relaxed text-white font-medium" style={{ fontFamily: 'DM Sans, sans-serif' }}>
                          {item.query}
                        </p>
                      </div>

                      {/* AI Response */}
                      <div className="self-start w-full">
                        {item.loading ? (
                          <div className="flex items-center gap-3 text-neu-muted text-sm font-dm px-4 py-2">
                            <span className="w-5 h-5 rounded-full border-[2.5px] border-[#0D9488]/30 border-t-[#0D9488] animate-spin" />
                            Processing query through semantic gateway…
                          </div>
                        ) : (
                          <>
                            {item.showApiKeyHelp && <ApiKeyBanner />}
                            {(item.response || (item.error && !item.showApiKeyHelp)) && (
                              <QueryResultPanel
                                response={item.response}
                                error={item.showApiKeyHelp ? null : item.error}
                                defaultTab="Summary"
                                onSuggestionClick={(suggestion) => {
                                  setQueryText(suggestion);
                                  inputRef.current?.focus();
                                }}
                                isMostRecent={isMostRecent}
                              />
                            )}
                          </>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Bottom panel (sticky input bar) */}
      <div className="shrink-0 px-8 pt-4 pb-6 backdrop-blur-2xl bg-[rgba(248,250,252,0.85)] pointer-events-auto z-20" style={{ boxShadow: '0 -20px 40px rgba(248,250,252,0.9)' }}>
        <div className="max-w-4xl mx-auto">
          <form 
            onSubmit={handleSubmit} 
            className="flex flex-col gap-4 p-6 rounded-[32px] transition-all duration-300"
            style={{ background: 'rgba(255,255,255,0.7)', boxShadow: CLAY_SHADOW }}
          >
            <div className="flex items-center justify-between px-2">
              <label
                htmlFor="query-input"
                className="text-[15px] font-bold text-[#1A3A38] tracking-tight"
                style={{ fontFamily: 'Nunito, sans-serif' }}
              >
                Natural Language Query
              </label>
              {history.length > 0 && (
                <button
                  type="button"
                  onClick={handleReset}
                  className="flex items-center gap-1.5 text-xs text-[#4A7B76] hover:text-[#0D9488] font-bold transition-colors"
                  style={{ fontFamily: 'DM Sans, sans-serif' }}
                >
                  <RotateCcw size={12} /> Clear History
                </button>
              )}
            </div>

            <textarea
              id="query-input"
              ref={inputRef}
              rows={2}
              value={queryText}
              onChange={(e) => setQueryText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleSubmit(e);
                }
              }}
              placeholder="e.g. What is the MRR by plan type for the last 3 months?"
              className="w-full rounded-[24px] px-6 py-4 text-[#1A3A38] text-[15px] placeholder:text-[#4A7B76]/70
                         focus:outline-none focus:ring-4 focus:ring-[#0D9488]/20 focus:bg-white
                         transition-all duration-200 resize-none font-dm"
              style={{
                background: '#E6F7F6',
                boxShadow: CLAY_INSET,
                border: 'none'
              }}
            />

            <div className="flex flex-wrap items-center gap-6 px-2 mt-1">
              {[
                { id: 'include-sql',     label: 'Include SQL',     val: includeSql,     set: setIncludeSql },
                { id: 'include-lineage', label: 'Include Lineage', val: includeLineage, set: setIncludeLineage },
                { id: 'dry-run',         label: 'Dry Run',         val: dryRun,         set: setDryRun },
              ].map(({ id, label, val, set }) => (
                <label key={id} htmlFor={id} className="flex items-center gap-2.5 cursor-pointer group">
                  <input
                    type="checkbox"
                    id={id}
                    checked={val}
                    onChange={(e) => set(e.target.checked)}
                    className="w-4 h-4 accent-[#0D9488] cursor-pointer rounded-md"
                  />
                  <span className="text-sm text-[#4A7B76] group-hover:text-[#0D9488] transition-colors select-none font-medium" style={{ fontFamily: 'DM Sans, sans-serif' }}>
                    {label}
                  </span>
                </label>
              ))}

              <label htmlFor="max-rows" className="flex items-center gap-2.5 ml-2">
                <span className="text-sm text-[#4A7B76] font-medium" style={{ fontFamily: 'DM Sans, sans-serif' }}>Max rows</span>
                <select
                  id="max-rows"
                  value={maxRows}
                  onChange={(e) => setMaxRows(Number(e.target.value))}
                  className="rounded-[12px] px-3 py-1.5 text-sm text-[#1A3A38] focus:outline-none focus:ring-2 focus:ring-[#0D9488]/20 cursor-pointer font-dm transition-colors"
                  style={{
                    background: 'rgba(255,255,255,0.80)',
                    boxShadow: CLAY_INSET,
                    border: 'none',
                  }}
                >
                  {[25, 100, 500, 1000].map((n) => (
                    <option key={n} value={n}>{n}</option>
                  ))}
                </select>
              </label>

              <div className="flex-1" />

              <button
                type="submit"
                id="run-query-btn"
                disabled={loading || !queryText.trim()}
                className="btn-primary"
              >
                {loading
                  ? <><span className="w-4 h-4 rounded-full border-2 border-white/30 border-t-white animate-spin" />Running…</>
                  : <><Send size={15} />Run Query</>
                }
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
