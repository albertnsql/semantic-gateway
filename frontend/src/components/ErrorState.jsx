/** components/ErrorState.jsx — clay-theme error card. */
import { AlertCircle } from 'lucide-react';

export default function ErrorState({ message = 'An error occurred.', detail = null }) {
  return (
    <div
      className="p-5 flex gap-4 items-start rounded-[24px] backdrop-blur-xl"
      style={{
        background: 'rgba(244,63,94,0.06)',
        boxShadow: '8px 8px 20px rgba(244,63,94,0.08), -6px -6px 16px rgba(255,255,255,0.85)',
      }}
    >
      <AlertCircle className="text-[#F43F5E] shrink-0 mt-0.5" size={18} />
      <div>
        <p
          className="text-[#F43F5E] font-bold text-sm"
          style={{ fontFamily: 'DM Sans, sans-serif' }}
        >
          {message}
        </p>
        {detail && (
          <pre className="mt-2 text-xs text-[#F43F5E]/70 font-mono whitespace-pre-wrap break-words">
            {typeof detail === 'string' ? detail : JSON.stringify(detail, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}
