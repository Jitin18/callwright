import { Fragment, useEffect, useRef } from "react";
import type { Caption } from "../lib/types";

/** Renders ⟨redacted⟩ tokens as lock pills so the PII redaction is visible, not hidden. */
export function Redacted({ text }: { text: string }) {
  const parts = text.split(/(⟨[^⟩]+⟩)/g);
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith("⟨") ? <span key={i} className="pii-pill" title="Redacted before leaving the runtime">{p.slice(1, -1)}</span>
          : <Fragment key={i}>{p}</Fragment>,
      )}
    </>
  );
}

export function Captions({ captions }: { captions: Caption[] }) {
  const end = useRef<HTMLDivElement>(null);
  // Block body on purpose: newer browsers return a Promise from scrollIntoView, and an effect must not return one.
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [captions.length]);
  return (
    <div className="captions" aria-live="polite">
      {captions.length === 0 && <p className="captions-empty">The conversation appears here as it happens.</p>}
      {captions.slice(-14).map((c) => (
        <p key={c.id} className={`cap cap--${c.role}${c.kind && c.kind !== "reply" ? ` cap--${c.kind}` : ""}`}>
          <span className="cap-who">
            {c.role === "customer" ? "Customer" : c.kind === "ack" ? "Assistant · ack" : c.kind === "narration" ? "Assistant · progress" : "Assistant"}
            {c.direction && <span className="tone" title="Vocal direction sent to the voice">{c.direction}</span>}
          </span>
          <span className="cap-text"><Redacted text={c.text} /></span>
        </p>
      ))}
      <div ref={end} />
    </div>
  );
}
