// Offline: Policy and decide() are pure code and must match jevmod/core/policy.py.
import { describe, expect, it } from "vitest";
import { DEFAULT_ACTIONS, DEFAULT_THRESHOLDS, EXPERIMENTAL, FLAG_ONLY, POLICY_VERSION, Policy, Verdict, decide, decisionToJSON } from "../src/index.js";

describe("Policy", () => {
  it("starts flag-only with offtopic off", () => {
    const p = new Policy();
    expect(p.enabledCategories()).toEqual(["spam", "scam", "harassment", "nsfw", "selfharm", "doxxing", "minors"]);
    expect(p.actions).toEqual(DEFAULT_ACTIONS);
    expect(p.thresholds).toEqual(DEFAULT_THRESHOLDS);
    expect(p.timeoutMinutes).toBe(10);
    expect(p.active()).toBe(true);
  });

  it("validates categories and actions and clamps thresholds to [0.5, 0.99]", () => {
    const p = new Policy();
    p.setCategory("scam", "delete", 0.3);
    expect(p.actions["scam"]).toBe("delete");
    expect(p.thresholds["scam"]).toBe(0.5);
    p.setCategory("scam", "timeout", 1.5);
    expect(p.thresholds["scam"]).toBe(0.99);
    expect(() => p.setCategory("nope", "flag")).toThrow(/unknown category/);
    expect(() => p.setCategory("spam", "ban")).toThrow(/unknown action/);
  });

  it("normalises rule names, caps at five rules and removes on empty text", () => {
    const p = new Policy();
    p.setRule("  No Politics Please ", "No political discussion. Game news is fine.");
    expect(p.rules).toEqual({ no_politics_please: "No political discussion. Game news is fine." });
    expect(p.ruleActions["no_politics_please"]).toBe("flag");
    for (const n of ["r2", "r3", "r4", "r5"]) p.setRule(n, `rule ${n}`);
    expect(() => p.setRule("r6", "one too many")).toThrow(/up to 5 custom rules/);
    p.setRule("r2", "replacing an existing rule is fine", "delete", 0.9);
    expect(p.ruleActions["r2"]).toBe("delete");
    expect(p.ruleThresholds["r2"]).toBe(0.9);
    p.setRule("r2", "");
    expect(p.rules["r2"]).toBeUndefined();
    expect(p.ruleActions["r2"]).toBeUndefined();
    expect(p.ruleThresholds["r2"]).toBeUndefined();
    p.setRule("long", "x".repeat(300));
    expect(p.rules["long"]?.length).toBe(200);
  });

  it("nudges a category or a rule threshold up a notch", () => {
    const p = new Policy();
    expect(p.nudge("spam")).toBe(0.88);
    expect(p.nudge("spam", 0.5)).toBe(0.99);
    expect(p.nudge("rule:no_politics")).toBe(0.83); // from RULE_THRESHOLD 0.8
    expect(p.nudge("unknown")).toBe(0.93); // from the 0.9 fallback
  });

  it("round-trips through the same JSON shape as the Python package", () => {
    const p = new Policy();
    p.setCategory("harassment", "timeout", 0.7);
    p.setRule("no_politics", "No politics.", "delete", 0.85);
    p.timeoutMinutes = 30;
    const json = JSON.parse(JSON.stringify(p));
    expect(Object.keys(json).sort()).toEqual(
      ["actions", "rule_actions", "rule_thresholds", "rules", "thresholds", "timeout_minutes", "version"].sort(),
    );
    expect(json.version).toBe(POLICY_VERSION);
    const back = Policy.fromJSON(json);
    expect(back.toJSON()).toEqual(p.toJSON());
    // a partial dict from Python keeps the defaults for what it omits
    const partial = Policy.fromJSON({ actions: { spam: "delete" }, timeout_minutes: "15" });
    expect(partial.actions["spam"]).toBe("delete");
    expect(partial.actions["scam"]).toBe("flag");
    expect(partial.timeoutMinutes).toBe(15);
  });
});

describe("decide", () => {
  const scores = { spam: 0.9, scam: 0.95, harassment: 0.1, nsfw: 0.02, selfharm: 0.01, doxxing: 0.01, minors: 0.0 };

  it("returns none for messages a pre-filter skipped", () => {
    const d = decide(new Policy(), new Verdict("1", {}, false, "too short"));
    expect(d).toEqual({ message_id: "1", action: "none", category: null, probability: 0, scores: {}, judged: false, reason: "too short", policy_version: 1 });
  });

  it("picks the most severe action, then the higher probability", () => {
    const p = new Policy();
    let d = decide(p, new Verdict("1", scores, true, "jev"));
    expect([d.action, d.category, d.probability]).toEqual(["flag", "scam", 0.95]); // both flag: higher p wins
    p.setCategory("spam", "delete");
    d = decide(p, new Verdict("1", scores, true, "jev"));
    expect([d.action, d.category]).toEqual(["delete", "spam"]); // delete beats flag despite lower p
  });

  it("applies custom rules with their own thresholds and puts them in scores as rule:<name>", () => {
    const p = new Policy();
    p.setRule("no_politics", "No politics.", "timeout", 0.6);
    const v = new Verdict("1", { spam: 0.1 }, true, "jev", { no_politics: 0.65 });
    const d = decide(p, v);
    expect([d.action, d.category, d.probability]).toEqual(["timeout", "rule:no_politics", 0.65]);
    expect(d.scores).toEqual({ spam: 0.1, "rule:no_politics": 0.65 });
    expect(decide(new Policy(), v).action).toBe("none"); // rule unknown to this policy: default 0.8 not crossed
  });

  it("ignores categories switched off and rounds to four decimals in JSON form", () => {
    const p = new Policy();
    p.setCategory("scam", "off");
    const d = decide(p, new Verdict("1", { scam: 0.99, spam: 0.123456 }, true, "cache"));
    expect(d.action).toBe("none");
    expect(decisionToJSON(d).scores).toEqual({ scam: 0.99, spam: 0.1235 });
  });

  it("keeps the first of equal hits like Python's max()", () => {
    const p = new Policy();
    const d = decide(p, new Verdict("1", { spam: 0.9, scam: 0.9 }, true, "jev"));
    expect(d.category).toBe("spam");
  });
});

describe("experimental categories are flag-only on every path", () => {
  it("refuses delete and timeout through setCategory", () => {
    const p = new Policy();
    for (const category of EXPERIMENTAL) {
      for (const action of ["delete", "timeout"]) expect(() => p.setCategory(category, action)).toThrow(/experimental/);
    }
  });

  it("loads a stored delete or timeout as flag, and leaves other categories alone", () => {
    for (const category of EXPERIMENTAL) {
      for (const stored of ["delete", "timeout"]) {
        const p = Policy.fromJSON({ actions: { [category]: stored, scam: "delete" } });
        expect(p.actions[category]).toBe("flag");
        expect(p.actions["scam"]).toBe("delete");
        const d = decide(p, new Verdict("1", { [category]: 0.99 }, true, "jev"));
        expect([d.action, d.category]).toEqual(["flag", category]);
      }
      expect(Policy.fromJSON({ actions: { [category]: "off" } }).actions[category]).toBe("off");
    }
  });

  it("caps decide() at flag even when the actions map was written directly", () => {
    for (const category of EXPERIMENTAL) {
      const p = new Policy();
      p.actions[category] = "timeout";
      const d = decide(p, new Verdict("1", { [category]: 0.99, spam: 0.1 }, true, "jev"));
      expect([d.action, d.category]).toEqual(["flag", category]);
    }
  });
});

describe("selfharm is flag-only on every path", () => {
  // The site says self harm only ever flags, at any line, and that it is not a setting. It may still be off.
  it("is in FLAG_ONLY with the experimental categories", () => {
    expect([...FLAG_ONLY].sort()).toEqual([...EXPERIMENTAL, "selfharm"].sort());
    expect(EXPERIMENTAL).not.toContain("selfharm");
  });

  it("refuses delete and timeout through setCategory, and still accepts off", () => {
    const p = new Policy();
    for (const action of ["delete", "timeout"]) expect(() => p.setCategory("selfharm", action)).toThrow(/selfharm/);
    expect(p.actions["selfharm"]).toBe("flag");
    p.setCategory("selfharm", "off");
    expect(p.actions["selfharm"]).toBe("off");
  });

  it("loads a stored delete or timeout as flag", () => {
    for (const stored of ["delete", "timeout"]) {
      const p = Policy.fromJSON({ actions: { selfharm: stored, scam: "delete" } });
      expect(p.actions["selfharm"]).toBe("flag");
      expect(p.actions["scam"]).toBe("delete");
      const d = decide(p, new Verdict("1", { selfharm: 0.99 }, true, "jev"));
      expect([d.action, d.category]).toEqual(["flag", "selfharm"]);
    }
    expect(Policy.fromJSON({ actions: { selfharm: "off" } }).actions["selfharm"]).toBe("off");
  });

  it("caps decide() at flag even when the actions map was written directly", () => {
    const p = new Policy();
    p.actions["selfharm"] = "timeout";
    const d = decide(p, new Verdict("1", { selfharm: 0.99, spam: 0.1 }, true, "jev"));
    expect([d.action, d.category]).toEqual(["flag", "selfharm"]);
  });
});
