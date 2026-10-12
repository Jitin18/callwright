import type { TurnView } from "../lib/types";
import { Redacted } from "./Captions";

// Voice Live measures from the end of the customer's speech to the first audio of the reply.
const SEGMENTS = [["tools", "Subagents & systems"]] as const;

/** Each turn is a card; older turns recede into the deck. Bars show where the time went. */
export function TurnDeck({ turns }: { turns: TurnView[] }) {
  const recent = turns.slice(-4).reverse();
  return (
    <section className="turns panel" aria-label="Turn timing">
      <header className="panel-head"><h2>Turns</h2><span className="legend">
        {SEGMENTS.map(([k, n]) => <span key={k} className={`lg lg--${k}`}>{n}</span>)}
      </span></header>
      <div className="deck">
        {recent.length === 0 && <p className="empty">Latency for every turn lands here.</p>}
        {recent.map((t, i) => {
          const L = t.latency ?? {};
          const total = Math.max(L.first_audio ?? L.tools ?? 1, 1);
          return (
            <article key={t.id} className="turn-card" style={{ ["--i" as string]: i }} aria-hidden={i > 0}>
              <div className="turn-top">
                <span className="turn-id">Turn {t.id.slice(1)}</span>
                <span className="turn-text"><Redacted text={t.text} /></span>
                {L.first_audio != null && <strong className="turn-total" title="speech end → first reply audio">{(L.first_audio / 1000).toFixed(2)} s</strong>}
              </div>
              <div className="turn-intents">
                {t.blocked && <span className="chip chip--block">Blocked by guardrail</span>}
                {t.intents.map((x) => <span key={x} className="chip">{x}</span>)}
                {t.sentiment && t.sentiment !== "neutral" && <span className="chip chip--mood">{t.sentiment}</span>}
              </div>
              <div className="bars">
                {SEGMENTS.map(([k]) => {
                  const v = (L as Record<string, number | null | undefined>)[k];
                  return v ? <span key={k} className={`bar bar--${k}`} style={{ flexGrow: v / total }} title={`${k}: ${v} ms`}><b>{v}</b></span> : null;
                })}
                {L.first_audio != null && (
                  <span className="first-audio" style={{ left: `${Math.min(100, (L.first_audio / total) * 100)}%` }}>
                    first audio {L.first_audio} ms
                  </span>
                )}
              </div>
              {L.tools ? <p className="turn-tools">Systems {L.tools} ms{L.post_ack_delay_ms ? ` · pause after acknowledgement ${L.post_ack_delay_ms} ms` : ""}</p> : null}
            </article>
          );
        })}
      </div>
    </section>
  );
}
