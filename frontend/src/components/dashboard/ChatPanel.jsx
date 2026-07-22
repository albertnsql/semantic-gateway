import React, { useRef, useEffect, useState } from 'react';
import { X, Send, Sparkles, MessageSquare, ChevronLeft } from 'lucide-react';
import ChatMessage from './ChatMessage';
import QueryProgress from '../QueryProgress';

const CLAY_SHADOW = `16px 16px 32px rgba(13,148,136,0.12), -10px -10px 24px rgba(255,255,255,0.9), inset 6px 6px 12px rgba(13,148,136,0.04), inset -6px -6px 12px rgba(255,255,255,1)`;
const CLAY_INSET  = `inset 8px 8px 16px rgba(13,148,136,0.08), inset -8px -8px 16px rgba(255,255,255,0.9)`;

export default function ChatPanel({
  drawerOpen,
  setDrawerOpen,
  messages,
  isTyping,
  handleSendChat,
  handleClearChat,
  showPrompts,
  suggestedPrompts = [
    "What's our MRR this month?",
    "Show churn by plan type",
    "Which content has the highest engagement?"
  ]
}) {
  const [chatInput, setChatInput] = useState(() => sessionStorage.getItem('dashboard_chat_input') || '');
  const messagesRef = useRef(null);

  useEffect(() => {
    sessionStorage.setItem('dashboard_chat_input', chatInput);
  }, [chatInput]);

  const onSend = (text) => {
    if (!text.trim()) return;
    handleSendChat(text);
    setChatInput('');
  };

  useEffect(() => {
    const list = messagesRef.current;
    if (!list) return;
    list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' });
  }, [messages, isTyping]);

  return (
    <>
      {/* ── Collapsed re-open tab ── */}
      {!drawerOpen && (
        <button
          onClick={() => setDrawerOpen(true)}
          title="Open Gateway Chat"
          className="fixed right-0 top-1/2 -translate-y-1/2 z-50 flex flex-col items-center gap-2 text-white py-4 px-3 rounded-l-[20px] transition-all duration-200 group hover:-translate-x-0.5"
          style={{
            background: 'linear-gradient(135deg, #2DD4BF, #0D9488)',
            boxShadow: '12px 12px 24px rgba(13,148,136,0.30), -8px -8px 16px rgba(255,255,255,0.4)',
          }}
        >
          <MessageSquare size={16} />
          <span
            className="text-[10px] font-bold tracking-widest uppercase"
            style={{ writingMode: 'vertical-rl', transform: 'rotate(180deg)', fontFamily: 'DM Sans, sans-serif' }}
          >
            Gateway Chat
          </span>
          <ChevronLeft size={13} className="opacity-70 group-hover:opacity-100" />
        </button>
      )}

      {/* ── Side panel ── */}
      <div
        className={`
          sticky top-0 h-[calc(100vh-4rem)] z-40
          flex-shrink-0 flex flex-col rounded-[32px] backdrop-blur-xl
          transition-all duration-300 ease-in-out overflow-hidden
          ${drawerOpen ? 'w-[380px]' : 'w-0 opacity-0 pointer-events-none'}
        `}
        style={{ background: 'rgba(255,255,255,0.70)', boxShadow: CLAY_SHADOW }}
      >
        {/* Header */}
        <div
          className="h-[64px] px-5 flex justify-between items-center flex-shrink-0 rounded-t-[32px]"
          style={{
            borderBottom: '1px solid rgba(13,148,136,0.08)',
            background: 'rgba(255,255,255,0.50)',
          }}
        >
          <div className="flex flex-col">
            <div className="flex items-center gap-2">
              <div
                className="w-6 h-6 rounded-full flex items-center justify-center"
                style={{ background: 'linear-gradient(135deg, #2DD4BF, #0D9488)', boxShadow: '4px 4px 8px rgba(13,148,136,0.20)' }}
              >
                <Sparkles size={12} className="text-white" />
              </div>
              <h2
                className="text-sm font-black text-[#1A3A38] whitespace-nowrap"
                style={{ fontFamily: 'Nunito, sans-serif' }}
              >
                Gateway Chat
              </h2>
            </div>
            <span
              className="text-[10px] text-[#0D9488] font-bold tracking-wide mt-0.5 whitespace-nowrap"
              style={{ fontFamily: 'DM Sans, sans-serif' }}
            >
              Governed · Grain-validated
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleClearChat}
              className="text-xs text-[#4A7B76] hover:text-[#0D9488] transition-colors font-medium whitespace-nowrap"
              style={{ fontFamily: 'DM Sans, sans-serif' }}
            >
              Clear
            </button>
            <button
              onClick={() => setDrawerOpen(false)}
              title="Close chat panel"
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-[16px] text-[#0D9488] hover:text-[#0F766E] text-xs font-bold transition-all duration-200 hover:-translate-y-0.5 backdrop-blur-xl"
              style={{ background: 'rgba(255,255,255,0.80)', boxShadow: '6px 6px 12px rgba(13,148,136,0.08), -4px -4px 8px rgba(255,255,255,0.9)', fontFamily: 'DM Sans, sans-serif' }}
            >
              <X size={12} strokeWidth={3} />
              Close
            </button>
          </div>
        </div>

        {/* Messages */}
        <div ref={messagesRef} className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden px-4 py-4 flex flex-col gap-3 overscroll-contain">
          {messages.map((m, i) => (
            <ChatMessage key={i} message={m} onSuggest={handleSendChat} onSuggestPopulate={(text) => { setChatInput(text); }} />
          ))}

          {isTyping && <QueryProgress />}

          {showPrompts && !isTyping && (
            <div className="flex flex-col gap-2 mt-1">
              {suggestedPrompts.map(p => (
                <button
                  key={p}
                  onClick={() => onSend(p)}
                  className="self-start text-left px-4 py-2 rounded-full text-[#0D9488] text-xs font-bold transition-all duration-200 hover:-translate-y-0.5"
                  style={{
                    background: 'rgba(13,148,136,0.08)',
                    boxShadow: '6px 6px 12px rgba(13,148,136,0.10), -4px -4px 8px rgba(255,255,255,0.9)',
                    fontFamily: 'DM Sans, sans-serif',
                  }}
                >
                  {p}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Input */}
        <div
          className="px-4 py-4 flex-shrink-0 rounded-b-[32px]"
          style={{ borderTop: '1px solid rgba(13,148,136,0.08)', background: 'rgba(255,255,255,0.50)' }}
        >
          <div className="relative">
            <textarea
              value={chatInput}
              onChange={e => setChatInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  onSend(chatInput);
                }
              }}
              placeholder="Ask about your streaming data..."
              disabled={isTyping}
              className="w-full rounded-[20px] px-4 py-3 pr-12 text-sm text-[#1A3A38] placeholder:text-[#4A7B76]
                         focus:outline-none focus:ring-4 focus:ring-[#0D9488]/20 focus:bg-white
                         resize-none transition-all duration-200"
              style={{
                background: '#E6F7F6',
                boxShadow: CLAY_INSET,
                border: 'none',
                fontFamily: 'DM Sans, sans-serif',
              }}
              rows={2}
            />
            <button
              onClick={() => onSend(chatInput)}
              disabled={isTyping || !chatInput.trim()}
              className="absolute right-2.5 bottom-2.5 w-8 h-8 rounded-full flex items-center justify-center
                         transition-all duration-200 disabled:opacity-40 hover:-translate-y-0.5"
              style={{
                background: 'linear-gradient(135deg, #2DD4BF, #0D9488)',
                boxShadow: '6px 6px 12px rgba(13,148,136,0.25), -4px -4px 8px rgba(255,255,255,0.4)',
              }}
            >
              <Send size={14} className="text-white" />
            </button>
          </div>
          <p
            className="text-[10px] text-[#4A7B76] text-center mt-2 font-medium"
            style={{ fontFamily: 'DM Sans, sans-serif' }}
          >
            Queries validated by the semantic layer
          </p>
        </div>
      </div>
    </>
  );
}
