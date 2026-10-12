import type { GuardState } from "../lib/types";

const INPUT = [["rules", "Injection rules"], ["topic", "On topic"], ["prompt_shield", "Prompt Shields"], ["content", "Content Safety"]] as const;
const OUTPUT = [["grounding", "Numbers grounded"], ["secret_leak", "No secrets"], ["pii_echo", "No PII read-back"],
  ["commanding", "Natural asking"], ["card_digits", "Card digits"]] as const;
const SENTENCE_COVERS = new Set(["grounding", "secret_leak", "pii_echo", "commanding", "card_digits"]);

export function Guardrails({ guards }: { guards: Record<string, GuardState> }) {
  const sentence = guards["sentence"];
  const status = (name: string) => {
    const g = guards[name];
    if (SENTENCE_COVERS.has(name) && sentence && (!g || sentence.at > g.at)) return sentence;   // a clean sentence passes all
    return g;
  };
  const row = (list: readonly (readonly [string, string])[], stage: string) => (
    <div className="guard-row">
      <span className="guard-stage">{stage}</span>
      {list.map(([key, name]) => {
        const g = status(key);
        const st = g?.status ?? "waiting";
        return (
          <span key={key} className={`shield shield--${st}`} title={g?.detail ? `${name}: ${g.detail}` : name}>
            <svg viewBox="0 0 20 22" aria-hidden><path d="M10 1 2 4v6c0 5 3.4 9.3 8 11 4.6-1.7 8-6 8-11V4l-8-3Z" /></svg>
            <span>{name}</span>
            {g && st !== "pass" && <em>{st}</em>}
          </span>
        );
      })}
    </div>
  );
  return (
    <section className="guards" aria-label="Guardrails">
      {row(INPUT, "Input")}
      {row(OUTPUT, "Output")}
    </section>
  );
}
