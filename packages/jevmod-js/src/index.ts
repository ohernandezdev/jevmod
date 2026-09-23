// jevmod: moderation for communities and apps, powered by Jev (TypeSafe's System One model).
//
//   import { Moderator } from "jevmod";
//   const mod = new Moderator();                         // TYPESAFE_API_KEY in the environment
//   const d = await mod.check("FREE NITRO click discord-gifts.ru");
//   // d.action === "flag", d.category === "scam", d.probability ~ 0.97
//
// `checkMany([...])` judges a batch in one request. Thresholds and actions come from a `Policy` you can pass in.
export { CATEGORIES, CATEGORY_NAMES, CATEGORIES_VERSION, isCategory, type Category, type CategoryName } from "./categories.js";
export { normalize, prefilter, htmlUnescape, countAlnum, isCombining, LINK_RE, type Message } from "./normalize.js";
export {
  Judge,
  Verdict,
  cacheKey,
  pyRepr,
  assemble,
  PAD_TO,
  LEAD_FILLER,
  MAX_CONTEXT_TOKENS,
  CHARS_PER_TOKEN,
  type JudgeOptions,
  type Scores,
} from "./judge.js";
export {
  ACTIONS,
  DEFAULT_ACTIONS,
  EXPERIMENTAL,
  DEFAULT_THRESHOLDS,
  MAX_RULES,
  POLICY_VERSION,
  RULE_THRESHOLD,
  Policy,
  decide,
  decisionToJSON,
  type Action,
  type Decision,
  type DecisionAction,
  type PolicyInit,
  type PolicyJSON,
} from "./policy.js";
export { Moderator, type ModeratorOptions, type CheckOptions, type CheckManyOptions } from "./moderator.js";
export {
  JevmodClient,
  JevmodApiError,
  type JevmodClientOptions,
  type ModerateMessage,
  type ModerateResponse,
  type ApiDecision,
  type PolicyUpdate,
  type HealthResponse,
} from "./client.js";
