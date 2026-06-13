/**
 * components/SqlViewer.jsx — clay-theme SQL code block with copy button.
 */
import { useState } from 'react';
import { Copy, Check } from 'lucide-react';

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { /* clipboard not available */ }
  };

  return (
    <button
      onClick={handleCopy}
      className="flex items-center gap-1.5 px-3 py-1 rounded-[16px] text-xs
                 text-[#4A7B76] hover:text-[#0D9488]
                 transition-all duration-150 hover:-translate-y-0.5 backdrop-blur-xl"
      style={{
        background: 'rgba(255,255,255,0.80)',
        boxShadow: '6px 6px 12px rgba(13,148,136,0.08), -4px -4px 8px rgba(255,255,255,0.9)',
        fontFamily: 'DM Sans, sans-serif',
      }}
    >
      {copied ? <Check size={12} className="text-emerald-500" /> : <Copy size={12} />}
      {copied ? 'Copied!' : 'Copy'}
    </button>
  );
}

function CodeBlock({ label, code }) {
  if (!code) return null;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <span
          className="text-xs font-bold text-[#4A7B76] uppercase tracking-widest"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          {label}
        </span>
        <CopyButton text={code} />
      </div>
      <pre
        className="rounded-[24px] p-5 overflow-x-auto text-xs leading-relaxed whitespace-pre"
        style={{
          background: '#0F1E1C',
          color: '#7DD3C8',
          fontFamily: 'JetBrains Mono, monospace',
          boxShadow: 'inset 8px 8px 20px rgba(0,0,0,0.4), inset -8px -8px 20px rgba(13,148,136,0.05)',
        }}
      >
        {code}
      </pre>
    </div>
  );
}

export default function SqlViewer({ generatedSql, metricflowQuery, sql }) {
  const sqlToShow = generatedSql ?? sql ?? null;
  const mqToShow  = metricflowQuery ?? null;

  return (
    <div className="flex flex-col gap-6">
      <CodeBlock label="Generated SQL"            code={sqlToShow} />
      <CodeBlock label="MetricFlow Query Command"  code={mqToShow} />
      {!sqlToShow && !mqToShow && (
        <p
          className="text-[#4A7B76]/50 text-sm text-center py-8"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          No SQL available.
        </p>
      )}
    </div>
  );
}
