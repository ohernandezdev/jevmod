// Stateless convenience wrapper for developers: no storage, no tenants, just judge + policy.
import { Judge, type Message } from "./judge.js";
import { Policy, decide, decisionToJSON, type Decision } from "./policy.js";

export interface ModeratorOptions {
  /** Thresholds, actions and custom rules. Defaults to flag-only with `offtopic` off. */
  policy?: Policy;
  /** TypeSafe API key. Never logged. Falls back to the `TYPESAFE_API_KEY` environment variable. */
  apiKey?: string;
  /** A pre-built Judge, for sharing a cache or a client between moderators. */
  judge?: Judge;
}

export interface CheckOptions {
  /** Not sent to Jev; kept for parity with the Python API. */
  author?: string;
  /** What the channel or thread is about; used by the `offtopic` category. */
  channelTopic?: string;
  /** True skips judgment entirely (moderators, verified staff). */
  authorTrusted?: boolean;
  /**
   * What was said in this channel just before, oldest first. It rides along in the request and is
   * asked the same questions; its answers are discarded and it never becomes a decision.
   *
   * It is worth passing. The same spam message reaches 17.3% recall judged alone and 38.7% in a
   * request of ten (`benchmark/BATCH_EFFECT.md` section 7, 300 real messages), and a caller
   * checking one message at a time is at the bottom of that curve. Ten is where it saturates.
   *
   * Only the text: no author names or ids, the same promise the rest of the package keeps. The
   * Python package fills this from its own conversation buffer; this one is stateless, so the
   * caller keeps the window.
   */
  padding?: readonly string[];
}

export interface CheckManyOptions extends CheckOptions {
  /** Your ids for the messages, echoed back as `message_id`. Defaults to "0", "1", ... */
  ids?: readonly string[];
}

export class Moderator {
  readonly policy: Policy;
  readonly judge: Judge;

  constructor(options: ModeratorOptions = {}) {
    this.policy = options.policy ?? new Policy();
    this.judge = options.judge ?? new Judge(options.apiKey !== undefined ? { apiKey: options.apiKey } : {});
  }

  async check(text: string, options: CheckOptions = {}): Promise<Decision> {
    const [d] = await this.checkMany([text], options);
    return d as Decision;
  }

  /** One Jev request for the whole batch (minus pre-filtered and cached messages). */
  async checkMany(texts: readonly string[], options: CheckManyOptions = {}): Promise<Decision[]> {
    const msgs: Message[] = texts.map((t, i) => ({
      id: options.ids?.[i] ?? String(i),
      text: t,
      ...(options.author !== undefined ? { author: options.author } : {}),
      ...(options.channelTopic !== undefined ? { channelTopic: options.channelTopic } : {}),
      ...(options.authorTrusted !== undefined ? { authorTrusted: options.authorTrusted } : {}),
    }));
    const verdicts = await this.judge.judge(
      msgs, this.policy.enabledCategories(), this.policy.rules, options.padding ?? [],
    );
    return verdicts.map((v) => decisionToJSON(decide(this.policy, v)));
  }
}
