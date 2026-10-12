import { useState } from "react";
import type { Handover } from "../lib/types";
import { Redacted } from "./Captions";

const DISPOSITION: Record<string, string> = {
  automated: "Automated", deflected: "Deflected to self-service", transferred_to_advisor: "Transferred to an advisor",
  partial_automation: "Partly automated, then transferred", partial_deflection: "Partly deflected, then transferred",
  abandoned: "Abandoned", failed_idv: "Identity not verified",
};

/** Warm-transfer note for the colleague (reason, summary, transcript), or the logged call summary. */
export function HandoverCard({ h, onClose }: { h: Handover; onClose: () => void }) {
  const [showTranscript, setShowTranscript] = useState(false);
  const isHandover = h.kind === "handover";
  return (
    <aside className="handover" role="dialog" aria-label={isHandover ? "Handover note" : "Call summary"}>
      <div className="handover-card">
        <header>
          <span className="handover-kind">{isHandover ? "Warm transfer · note for the colleague" : "Call outcome · logged to CRM"}</span>
          <button onClick={onClose} aria-label="Close">×</button>
        </header>
        {isHandover ? (
          <>
            <h3>Why: {h.reason}</h3>
            <p className="handover-summary">{h.summary}</p>
            <dl>
              <div><dt>Verified</dt><dd>{h.verified}</dd></div>
              <div><dt>Mood</dt><dd>{h.customer_mood}</dd></div>
              <div><dt>Queue</dt><dd>{h.queue}</dd></div>
            </dl>
            {h.transcript?.length ? (
              <>
                <button className="link" onClick={() => setShowTranscript(!showTranscript)}>
                  {showTranscript ? "Hide transcript" : `Show transcript (${h.transcript.length} lines)`}
                </button>
                {showTranscript && <ol className="handover-transcript">{h.transcript.map((l, i) => <li key={i}><Redacted text={l} /></li>)}</ol>}
              </>
            ) : null}
          </>
        ) : (
          <>
            <h3><span className={`disp disp--${h.disposition}`}>{DISPOSITION[h.disposition ?? ""] ?? h.disposition}</span></h3>
            <p className="handover-summary">{h.summary}</p>
            <dl>
              <div><dt>Verified</dt><dd>{h.verified}</dd></div>
              <div><dt>Mood</dt><dd>{h.customer_mood}</dd></div>
              <div><dt>Ended by</dt><dd>{(h.ended_by ?? "").replace(/_/g, " ")}</dd></div>
            </dl>
            {h.transfer_reason && <p className="next"><span>Transfer reason</span>{h.transfer_reason}</p>}
          </>
        )}
      </div>
    </aside>
  );
}
