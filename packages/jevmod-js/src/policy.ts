// Policy: turn a Verdict (probabilities) into a Decision (what to do). Pure code, offline-testable.
// Ported from jevmod/core/policy.py; the JSON shapes are the same so policies can be shared with the
// Python package and the HTTP API.
import { CATEGORY_NAMES, isCategory, type CategoryName } from "./categories.js";
import type { Verdict } from "./judge.js";

/** Ordered by severity. */
export const ACTIONS = ["off", "flag", "delete", "timeout"] as const;
export type Action = (typeof ACTIONS)[number];
export type DecisionAction = "none" | Exclude<Action, "off">;

export const DEFAULT_THRESHOLDS: Readonly<Record<CategoryName, number>> = {
  spam: 0.85,
  scam: 0.75,
  harassment: 0.75,
  nsfw: 0.8,
  offtopic: 0.9,
  selfharm: 0.5,
  doxxing: 0.8,
  minors: 0.7,
  ai_generated: 0.85,
};
// Experimental categories are never moved by feedback: their measured precision at a realistic rate is too low
// for a single reaction to carry information about where the line belongs. Mirrors EXPERIMENTAL in policy.py.
export const EXPERIMENTAL: readonly string[] = ["ai_generated"];
// Categories that can be off or flag and nothing else, on every path. Mirrors FLAG_ONLY in policy.py, which
// carries the full reasoning: the experimental ones for their precision, selfharm because its line sits at 0.50
// on purpose and that trade only holds if a hit reaches a person instead of removing a message or a member.
export const FLAG_ONLY: readonly string[] = [...EXPERIMENTAL, "selfharm"];
// selfharm is flag-only by design: moderators should reach out, not punish. doxxing/minors flag by default;
// communities that want automatic removal set delete/timeout explicitly.
export const DEFAULT_ACTIONS: Readonly<Record<CategoryName, Action>> = {
  spam: "flag",
  scam: "flag",
  harassment: "flag",
  nsfw: "flag",
  offtopic: "off",
  selfharm: "flag",
  doxxing: "flag",
  minors: "flag",
  ai_generated: "off",
};
export const RULE_THRESHOLD = 0.8;
export const POLICY_VERSION = 1;
export const MAX_RULES = 5;

/** The JSON form of a Policy: same keys as the Python `Policy.to_dict()` and the HTTP API. */
export interface PolicyJSON {
  thresholds: Record<string, number>;
  actions: Record<string, string>;
  rules: Record<string, string>;
  rule_actions: Record<string, string>;
  rule_thresholds: Record<string, number>;
  timeout_minutes: number;
  version: number;
}

export interface PolicyInit {
  thresholds?: Record<string, number>;
  actions?: Record<string, string>;
  rules?: Record<string, string>;
  ruleActions?: Record<string, string>;
  ruleThresholds?: Record<string, number>;
  timeoutMinutes?: number;
}

function isAction(a: string): a is Action {
  return (ACTIONS as readonly string[]).includes(a);
}

export class Policy {
  thresholds: Record<string, number>;
  actions: Record<string, string>;
  /** name -> natural-language rule */
  rules: Record<string, string>;
  /** name -> action (default flag) */
  ruleActions: Record<string, string>;
  ruleThresholds: Record<string, number>;
  timeoutMinutes = 10;

  constructor(init: PolicyInit = {}) {
    this.thresholds = { ...DEFAULT_THRESHOLDS, ...(init.thresholds ?? {}) };
    this.actions = { ...DEFAULT_ACTIONS, ...(init.actions ?? {}) };
    this.rules = { ...(init.rules ?? {}) };
    this.ruleActions = { ...(init.ruleActions ?? {}) };
    this.ruleThresholds = { ...(init.ruleThresholds ?? {}) };
    if (init.timeoutMinutes !== undefined) this.timeoutMinutes = init.timeoutMinutes;
  }

  enabledCategories(): CategoryName[] {
    return CATEGORY_NAMES.filter((c) => (this.actions[c] ?? "off") !== "off");
  }

  active(): boolean {
    return this.enabledCategories().length > 0 || Object.keys(this.rules).length > 0;
  }

  setCategory(category: string, action: string, threshold?: number | null): void {
    if (!isCategory(category)) {
      throw new Error(`unknown category '${category}'; one of ${CATEGORY_NAMES.join(", ")}`);
    }
    if (!isAction(action)) {
      throw new Error(`unknown action '${action}'; one of ${ACTIONS.join(", ")}`);
    }
    if (capped(category, action) !== action) {
      if (EXPERIMENTAL.includes(category)) {
        throw new Error(
          `${category} is experimental and can only be off or flag: its precision on real traffic is too low to ` +
            "remove a message or time a member out",
        );
      }
      throw new Error(
        `${category} can only be off or flag: a hit means somebody may need help, so it goes to a moderator and ` +
          "never removes a message or times a member out",
      );
    }
    this.actions[category] = action;
    if (threshold !== undefined && threshold !== null) this.thresholds[category] = clamp(threshold);
  }

  /** Add or replace a rule; a null or empty text removes it. At most five rules. */
  setRule(name: string, text: string | null | undefined, action: string = "flag", threshold?: number | null): void {
    name = Array.from(name.trim().toLowerCase().replaceAll(" ", "_")).slice(0, 30).join("");
    if (text === null || text === undefined || !text.trim()) {
      delete this.rules[name];
      delete this.ruleActions[name];
      delete this.ruleThresholds[name];
      return;
    }
    if (Object.keys(this.rules).length >= MAX_RULES && !(name in this.rules)) {
      throw new Error(`up to ${MAX_RULES} custom rules`);
    }
    if (!isAction(action)) throw new Error(`unknown action '${action}'`);
    this.rules[name] = Array.from(text.trim()).slice(0, 200).join("");
    this.ruleActions[name] = action;
    if (threshold !== undefined && threshold !== null) this.ruleThresholds[name] = clamp(threshold);
  }

  /** False-positive feedback: raise that category's threshold a notch. Returns the new threshold. */
  nudge(category: string, delta = 0.03): number {
    if (category.startsWith("rule:")) {
      const name = category.slice(5);
      const cur = this.ruleThresholds[name] ?? RULE_THRESHOLD;
      const next = clamp(cur + delta);
      this.ruleThresholds[name] = next;
      return next;
    }
    if (EXPERIMENTAL.includes(category)) return this.thresholds[category] ?? 0.9;
    const cur = this.thresholds[category] ?? 0.9;
    const next = clamp(cur + delta);
    this.thresholds[category] = next;
    return next;
  }

  toJSON(): PolicyJSON {
    return {
      thresholds: this.thresholds,
      actions: this.actions,
      rules: this.rules,
      rule_actions: this.ruleActions,
      rule_thresholds: this.ruleThresholds,
      timeout_minutes: this.timeoutMinutes,
      version: POLICY_VERSION,
    };
  }

  /** Build a Policy from its JSON form. Missing keys keep the defaults, like the Python `from_dict`. */
  static fromJSON(d: Partial<PolicyJSON> | Record<string, unknown>): Policy {
    const p = new Policy();
    const src = d as Record<string, unknown>;
    const merge = (target: Record<string, unknown>, key: string): void => {
      const v = src[key];
      if (v && typeof v === "object" && !Array.isArray(v)) Object.assign(target, v);
    };
    merge(p.thresholds, "thresholds");
    merge(p.actions, "actions");
    // A stored policy does not pass through setCategory. Refusing it here would stop moderation on every
    // message, so a flag-only category above flag is loaded as flag instead, like the Python from_dict.
    for (const c of FLAG_ONLY) {
      const a = p.actions[c];
      if (a !== undefined) p.actions[c] = capped(c, a);
    }
    merge(p.rules, "rules");
    merge(p.ruleActions, "rule_actions");
    merge(p.ruleThresholds, "rule_thresholds");
    if ("timeout_minutes" in src) p.timeoutMinutes = Math.trunc(Number(src["timeout_minutes"]));
    return p;
  }
}

/** Same shape as the Python `Decision.to_dict()`. */
export interface Decision {
  message_id: string;
  action: DecisionAction;
  /** winning category or "rule:<name>" */
  category: string | null;
  probability: number;
  /** everything Jev returned, for the audit log */
  scores: Record<string, number>;
  judged: boolean;
  /** "jev" | "cache" | pre-filter reason | "quota" | "error_open" */
  reason: string;
  policy_version: number;
}

/** The most severe action whose threshold is crossed wins; ties go to the higher probability. */
export function decide(policy: Policy, v: Verdict): Decision {
  const scores: Record<string, number> = { ...v.scores };
  for (const [n, p] of Object.entries(v.custom)) scores[`rule:${n}`] = p;
  // Keys in the same order as the Python to_dict(), so the JSON reads the same across implementations.
  const build = (action: DecisionAction, category: string | null, probability: number, judged: boolean): Decision => ({
    message_id: v.messageId,
    action,
    category,
    probability,
    scores,
    judged,
    reason: v.reason,
    policy_version: POLICY_VERSION,
  });
  if (!v.judged) return build("none", null, 0, false);
  const hits: Array<[string, number, Action]> = [];
  for (const [c, p] of Object.entries(v.scores)) {
    const action = capped(c, policy.actions[c] ?? "off");
    if (action !== "off" && isAction(action) && p >= (policy.thresholds[c] ?? 1.0)) hits.push([c, p, action]);
  }
  for (const [n, p] of Object.entries(v.custom)) {
    const action = policy.ruleActions[n] ?? "flag";
    if (action !== "off" && isAction(action) && p >= (policy.ruleThresholds[n] ?? RULE_THRESHOLD)) {
      hits.push([`rule:${n}`, p, action]);
    }
  }
  if (hits.length === 0) return build("none", null, 0, true);
  // Python's max() keeps the first of equal maxima, so only a strictly better hit replaces the current one.
  let best = hits[0] as [string, number, Action];
  for (const h of hits.slice(1)) {
    const better = ACTIONS.indexOf(h[2]) > ACTIONS.indexOf(best[2]) || (h[2] === best[2] && h[1] > best[1]);
    if (better) best = h;
  }
  const [category, p, action] = best;
  return build(action as DecisionAction, category, p, true);
}

/** Round the numbers like the Python `Decision.to_dict()` (4 decimals). */
export function decisionToJSON(d: Decision): Decision {
  const scores: Record<string, number> = {};
  for (const [k, v] of Object.entries(d.scores)) scores[k] = round4(v);
  return { ...d, probability: round4(d.probability), scores };
}

/** A FLAG_ONLY category can be off or flag and nothing else. setCategory refuses more; fromJSON and
 * decide() cap at flag, because `actions` is a plain object that callers also write to directly. */
function capped(category: string, action: string): string {
  return FLAG_ONLY.includes(category) && action !== "off" ? "flag" : action;
}

function round4(x: number): number {
  return Math.round(x * 10_000) / 10_000;
}

function clamp(x: number): number {
  return Math.max(0.5, Math.min(0.99, Math.round(Number(x) * 100) / 100));
}
