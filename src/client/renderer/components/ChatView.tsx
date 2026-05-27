/**
 * ChatView — generic chat UI component.
 *
 * Renders message bubbles, input textarea, send/end buttons, typing indicator.
 * Reusable across seeker, moment feedback, chat app, etc.
 */

import { useEffect, useRef, useState } from "react";

export interface ChatViewProps {
  messages: ChatMessage[];
  streaming: boolean;
  active: boolean;
  onSend: (content: string) => void | Promise<void>;
  onEnd?: () => void;
  placeholder?: string;
  workingLabel?: string;
}

export function ChatView({ messages, streaming, active, onSend, onEnd, placeholder, workingLabel }: ChatViewProps) {
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const busy = streaming || sending;
  const statusText = workingLabel ?? "Working...";

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    setSending(true);
    try {
      await onSend(text);
    } catch (err) {
      console.error("[chat] send failed:", err);
    } finally {
      setSending(false);
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    const clamped = Math.min(el.scrollHeight, 200);
    el.style.height = clamped + "px";
    el.style.overflowY = el.scrollHeight > 200 ? "auto" : "hidden";
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSend();
    }
  };

  const workingIndicator = (
    <span className="chat-working">
      <span className="chat-working-spinner" aria-hidden="true" />
      <span>{statusText}</span>
    </span>
  );

  const shouldShowStatusBubble = streaming && (
    messages.length === 0 || Boolean(messages[messages.length - 1]?.content)
  );

  return (
    <div className="chat-container">
      <div className="chat-messages">
        {messages.map((msg, i) => (
          <div key={i} className={`chat-message chat-message--${msg.role}`}>
            <div className="chat-message-bubble">
              {msg.content || (streaming && i === messages.length - 1 ? workingIndicator : null)}
            </div>
          </div>
        ))}
        {shouldShowStatusBubble && (
          <div className="chat-message chat-message--assistant">
            <div className="chat-message-bubble chat-message-bubble--status">
              {workingIndicator}
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>
      {active && (
        <div className="chat-input-area">
          <textarea
            ref={textareaRef}
            className="chat-input"
            value={input}
            onChange={handleInput}
            onKeyDown={handleKeyDown}
            placeholder={placeholder ?? "Type your response..."}
            disabled={busy}
            rows={1}
          />
          <button className="pill-btn pill-start" onClick={handleSend} disabled={busy || !input.trim()}>
            {busy ? statusText : "Send"}
          </button>
          {onEnd && (
            <button className="pill-btn pill-stop" onClick={onEnd} disabled={busy}>
              End
            </button>
          )}
        </div>
      )}
    </div>
  );
}
