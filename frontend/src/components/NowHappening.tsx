import { useState } from "react";
import type { Step } from "../lib/types";
import { Redacted } from "./Captions";

const ICON: Record<Step["status"], string> = { active: "…", ok: "✓", error: "✕", info: "" };

/** One clear line for what is happening right now; earlier steps collapse underneath. */
export function NowHappening({ steps }: { steps: Step[] }) {
  const [open, setOpen] = useState(false);
  const current = steps[steps.length - 1];
  const earlier = steps.slice(0, -1).reverse();
  return (
    <section className="now" aria-live="polite" aria-label="Now happening">
      <div className="now-main">
        <span className="now-tag">Now</span>
        {current ? <StepLine s={current} big /> : <span className="now-empty">Waiting for a call</span>}
        {earlier.length > 0 && (
          <button className="now-more" onClick={() => setOpen(!open)} aria-expanded={open}>
            {open ? "Hide" : `Earlier steps (${earlier.length})`}
          </button>
        )}
      </div>
      {open && (
        <ol className="now-earlier">
          {earlier.map((s) => <li key={s.id}><StepLine s={s} /></li>)}
        </ol>
      )}
    </section>
  );
}

function StepLine({ s, big }: { s: Step; big?: boolean }) {
  return (
    <span className={`step step--${s.status}${big ? " step--big" : ""}`}>
      <span className="step-path">
        {s.path.map((p, i) => (
          <span key={i}>{i > 0 && <i className="step-arrow">→</i>}<b>{p}</b></span>
        ))}
      </span>
      <span className="step-text"><i className="step-sep">·</i><Redacted text={s.text} />{s.detail ? <em> · {s.detail}</em> : null}</span>
      {ICON[s.status] && <span className="step-icon" aria-hidden>{ICON[s.status]}</span>}
    </span>
  );
}
