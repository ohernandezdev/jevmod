// Offline: the pre-filter and normalisation must behave like jevmod/judge.py, and the questions must be the
// same file as the Python package's.
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  CATEGORIES,
  CATEGORY_NAMES,
  DEFAULT_ACTIONS,
  DEFAULT_THRESHOLDS,
  EXPERIMENTAL,
  Policy,
  cacheKey,
  htmlUnescape,
  isCombining,
  normalize,
  prefilter,
  pyRepr,
} from "../src/index.js";

describe("prefilter", () => {
  it("skips trusted authors, empty and tiny messages, never links", () => {
    expect(prefilter({ id: "a", text: "lol" })).toBe("too short");
    expect(prefilter({ id: "b", text: "buy now http://x.y" })).toBeNull(); // links are never too short
    expect(prefilter({ id: "c", text: "long enough message here", authorTrusted: true })).toBe("trusted author");
    expect(prefilter({ id: "d", text: "   " })).toBe("empty");
  });

  it("counts letters and digits in any script, not words", () => {
    expect(prefilter({ id: "j", text: "こんにちは世界です" })).toBeNull(); // 8 characters, no spaces
    expect(prefilter({ id: "k", text: "こんにちは" })).toBe("too short");
    expect(prefilter({ id: "l", text: "!!! ??? ... ---" })).toBe("too short");
  });

  it("treats bare domains and defanged links as links", () => {
    expect(prefilter({ id: "m", text: "discord.gg/x" })).toBeNull();
    expect(prefilter({ id: "n", text: "go hxxps://bad" })).toBeNull();
    expect(prefilter({ id: "o", text: "x[.]y" })).toBeNull();
  });
});

describe("normalize", () => {
  it("unescapes html, folds fullwidth and ligatures, drops zalgo and zero-width characters", () => {
    // NFKC runs first, so a + U+0301 composes to á and stays (Python does the same); the overlay U+0338 has
    // no composition with z and is dropped, as is the zero-width space.
    expect(normalize("&#39;ＦＲＥＥ&#39; Ｎｉｔｒｏ ﬁ z̸ál​go  &amp; x")).toBe("'FREE' Nitro fi zálgo & x");
    expect(normalize("z̵̶̷a̴l̹g̺o")).toBe("zalgo");
  });

  it("maps the enclosed alphanumeric supplement rows to plain letters", () => {
    expect(normalize("\u{1F130}\u{1F151}\u{1F172}")).toBe("ABC"); // 🄰 🅑 🅲
  });

  it("collapses every kind of whitespace like Python's str.split", () => {
    expect(normalize("a　b\t\nc\x1fd\x85e")).toBe("a b c d e");
    expect(normalize("﻿  spaced   out  ")).toBe("spaced out");
  });

  it("keeps the marks Python keeps (combining class 0) and drops the ones it drops", () => {
    expect(normalize("สวัสดี")).toBe("สวัสดี"); // Thai vowel signs: ccc 0, kept
    expect(normalize("नमस्ते")).toBe("नमसते"); // Devanagari virama U+094D: ccc 9, dropped like Python
    expect(isCombining(0x0e31)).toBe(false); // Thai mai han-akat: Mn but ccc 0, kept by Python
    expect(isCombining(0x094d)).toBe(true); // Devanagari virama: ccc 9, dropped by Python
    expect(isCombining(0x0301)).toBe(true);
  });

  it("handles numeric entities like html.unescape", () => {
    expect(htmlUnescape("&#x27;a&#39;&#150;&#0;")).toBe("'a'–�");
    expect(htmlUnescape("&amp&lt;&unknown;")).toBe("&<&unknown;");
  });
});

describe("cache key and repr match the Python package", () => {
  it("pyRepr quotes like Python's repr()", () => {
    expect(pyRepr("it's fine")).toBe('"it\'s fine"');
    expect(pyRepr('say "hi"')).toBe("'say \"hi\"'");
    expect(pyRepr("both ' and \"\ttab\x01")).toBe("'both \\' and \"\\ttab\\x01'");
  });

  // This used to be a hash somebody computed once in Python and pasted in as a literal. When the
  // Python `_key` changed from a joined string to a JSON payload, the golden value went on passing
  // against a function that no longer existed, so the claim of sameness survived the thing it was
  // claiming. It now runs the Python function, the way the categories.json check below reads the
  // Python file, and skips the same way when there is nothing to compare against.
  const ROOT = resolve(__dirname, "..", "..", "..");
  // `sys.stdin.buffer.read().decode("utf-8")`, not `json.load(sys.stdin)`. On Windows a Python
  // subprocess gets cp1252 on stdin, so the text decoder silently mangled "café ❤" on its way in
  // and the two sides were compared on different inputs. The non-ascii case below is the one that
  // caught it, and it failed as a difference in the key rather than as an encoding error.
  const PY_SRC = "import json,sys;from jevmod.judge import _key;" +
    'a=json.loads(sys.stdin.buffer.read().decode("utf-8"));' +
    "print(_key(a[0],a[1],a[2],dict(a[3]),tuple(a[4])))";
  const PY_EXES = [resolve(ROOT, ".venv", "Scripts", "python.exe"), resolve(ROOT, ".venv", "bin", "python")];

  const pythonKey = (args: unknown[]): string | null => {
    for (const exe of PY_EXES) {
      const r = spawnSync(exe, ["-c", PY_SRC], { cwd: ROOT, input: JSON.stringify(args), encoding: "utf8" });
      if (r.status === 0) return r.stdout.trim();
    }
    return null;
  };

  // Probed once, and the tests below are *skipped* rather than passed when it fails: a published
  // package, a checkout with no venv or one where the Python deps are not installed has nothing to
  // compare against, and a cross-language check that quietly turns into a green tick is the same
  // kind of lie as the golden hash it replaced.
  const PY_AVAILABLE = pythonKey(["probe", "t", [], [], []]) !== null;

  const CASES: Array<[string, unknown[]]> = [
    ["plain", ["Hello World", "gaming", ["spam", "scam"], [], []]],
    ["rules with quotes and a newline", [
      "Hello World", "gaming", ["spam"],
      [["b", "it's \"quoted\"\n"], ["no_politics", "No political discussion."]], [],
    ]],
    ["padding", ["hello", "gaming", ["spam"], [], ["hey there", "sup"]]],
    // The separator bug the JSON payload exists to prevent: a pipe in the topic used to be able to
    // impersonate a field boundary, so these two inputs are the pair that must not collide.
    ["a pipe in the topic", ["a", "gaming|spam", ["spam"], [], []]],
    ["non-ascii and a control character", ["café ❤", "gam\u0001ing", ["spam"], [], ["ñ"]]],
  ];

  for (const [name, args] of CASES) {
    it.skipIf(!PY_AVAILABLE)(`cacheKey is byte-identical to the Python _key: ${name}`, () => {
      const theirs = pythonKey(args);
      const [text, topic, cats, rules, padding] = args as [string, string, string[], [string, string][], string[]];
      expect(cacheKey(text, topic, cats as never, Object.fromEntries(rules), padding)).toBe(theirs);
    });
  }

  it("the padding is part of the key, because the neighbours change the score", () => {
    const k = (padding: string[]) => cacheKey("hello", "gaming", ["spam"], {}, padding);
    expect(k(["a"])).not.toBe(k(["b"]));
    expect(k([])).not.toBe(k(["a"]));
    expect(k(["a", "b"])).toBe(k(["a", "b"]));
  });

  it("the same two categories in either order are one key", () => {
    expect(cacheKey("hi", "t", ["spam", "scam"], {})).toBe(cacheKey("hi", "t", ["scam", "spam"], {}));
  });
});

describe("categories.json", () => {
  it("is byte-identical to the Python package's file when the monorepo is present", () => {
    const ours = readFileSync(resolve(__dirname, "..", "src", "categories.json"), "utf8");
    let theirs: string | null = null;
    try {
      theirs = readFileSync(resolve(__dirname, "..", "..", "..", "jevmod", "categories.json"), "utf8");
    } catch {
      // published package or a checkout without the Python side: nothing to compare against
    }
    if (theirs !== null) expect(ours).toBe(theirs);
    expect(CATEGORY_NAMES).toEqual([
      "spam",
      "scam",
      "harassment",
      "nsfw",
      "offtopic",
      "selfharm",
      "doxxing",
      "minors",
      "ai_generated",
    ]);
    // Every category needs a threshold and an action, or the npm port silently judges what the Python one does
    // not, or vice versa. This is the check that caught ai_generated missing from both defaults.
    for (const c of CATEGORY_NAMES) {
      expect(DEFAULT_THRESHOLDS[c]).toBeGreaterThan(0);
      expect(DEFAULT_ACTIONS[c]).toBeDefined();
    }
    // Experimental categories ship off, and feedback never moves their threshold.
    for (const c of EXPERIMENTAL) {
      expect(DEFAULT_ACTIONS[c as (typeof CATEGORY_NAMES)[number]]).toBe("off");
      const p = new Policy({});
      expect(p.nudge(c, 0.03)).toBe(DEFAULT_THRESHOLDS[c as (typeof CATEGORY_NAMES)[number]]);
    }
    for (const c of CATEGORY_NAMES) {
      expect(CATEGORIES[c].instructions).toContain("{m}");
      expect(CATEGORIES[c].criteria.true.length).toBeGreaterThan(0);
      expect(CATEGORIES[c].criteria.false.length).toBeGreaterThan(0);
    }
  });
});
