/**
 * Tests for the brief trigger + watchdog Worker.
 *
 * Run with `npm test` (node --test, TypeScript stripped natively by Node >= 23).
 * No test framework and no Cloudflare test harness: the Worker's surface is one
 * `scheduled()` entry point whose every side effect is an outbound fetch, so a
 * fetch stub observes everything that matters and the real handler runs.
 *
 * What action ran is inferred from the URLs hit, not from internals:
 *   dispatch -> POST .../dispatches      watchdog -> GET .../contents/...
 *   skip     -> no fetch at all
 */

import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import worker from "../src/index.ts";

const ENV = {
  GITHUB_PAT: "pat-for-tests",
  GITHUB_REPO: "owner/repo",
  GITHUB_API_BASE: "https://api.github.com",
  DISPATCH_EVENT_TYPE: "morning-brief",
  TARGET_TZ: "America/Los_Angeles",
  TARGET_LOCAL_HOUR: "7",
  WATCHDOG_LOCAL_HOUR: "10",
  DELIVERY_MARKER_PATH: "briefs/.delivery.json",
  DISCORD_BOT_TOKEN: "bot-token-for-tests",
  DISCORD_TARGET: "user:123456789012345678",
};

const MARKER_URL = "/contents/briefs/.delivery.json";
const DISPATCH_URL = "/dispatches";
const DM_OPEN_URL = "/users/@me/channels";
const MESSAGES_URL = "/messages";

/** A ScheduledController is only ever read for `scheduledTime` here. */
function controller(iso: string) {
  return { scheduledTime: new Date(iso).getTime(), cron: "", noRetry() {} };
}

let calls: Array<{ url: string; method: string; init: any }>;
let logs: string[];
let realFetch: typeof globalThis.fetch;
let realSetTimeout: typeof globalThis.setTimeout;
let realLog: typeof console.log;
let realWarn: typeof console.warn;
let realError: typeof console.error;

/**
 * Route stubbed responses by URL fragment. Anything unrouted throws, so a test
 * can never accidentally pass by silently reaching a URL it did not expect.
 */
function stubFetch(routes: Array<[string, (init: any) => Response]>) {
  globalThis.fetch = (async (input: any, init: any = {}) => {
    const url = typeof input === "string" ? input : input.url;
    calls.push({ url, method: init.method ?? "GET", init });
    for (const [fragment, respond] of routes) {
      if (url.includes(fragment)) return respond(init);
    }
    throw new Error(`unexpected fetch to ${url}`);
  }) as any;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });

const hits = (fragment: string) => calls.filter((c) => c.url.includes(fragment));

/** The single message body the watchdog posts to Discord. */
function alertBody(): string {
  const post = hits(MESSAGES_URL)[0];
  assert.ok(post, "expected a Discord message to be posted");
  return JSON.parse(post.init.body).content;
}

beforeEach(() => {
  calls = [];
  logs = [];
  realFetch = globalThis.fetch;
  realSetTimeout = globalThis.setTimeout;
  realLog = console.log;
  realWarn = console.warn;
  realError = console.error;
  const capture =
    (...a: unknown[]) => void logs.push(a.map(String).join(" "));
  console.log = capture;
  console.warn = capture;
  console.error = capture;
  // Collapse the dispatch retry backoff so the retry test is not a 6s sleep.
  globalThis.setTimeout = ((fn: any) => realSetTimeout(fn, 0)) as any;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  globalThis.setTimeout = realSetTimeout;
  console.log = realLog;
  console.warn = realWarn;
  console.error = realError;
});

// ---------------------------------------------------------------------------
// Cron routing. Four UTC crons, two DST pairs, and the handler decides purely
// on the local Pacific hour. The property that must hold every day of the year:
// exactly one dispatch and exactly one watchdog, at 07:55 and 10:25 local.
// ---------------------------------------------------------------------------
describe("cron routing across DST", () => {
  const CRONS = ["T14:55:00Z", "T15:55:00Z", "T17:25:00Z", "T18:25:00Z"];
  const DAYS: Array<[string, string]> = [
    ["summer PDT", "2026-08-06"],
    ["winter PST", "2026-01-15"],
    ["spring-forward day", "2026-03-08"],
    ["fall-back day", "2026-11-01"],
  ];

  for (const [label, day] of DAYS) {
    test(`${label} fires exactly one dispatch and one watchdog`, async () => {
      const actions: string[] = [];

      for (const time of CRONS) {
        calls = [];
        stubFetch([
          [DISPATCH_URL, () => new Response(null, { status: 204 })],
          [MARKER_URL, () => json({ scheduled_date: day })],
        ]);
        await worker.scheduled(controller(`${day}${time}`) as any, ENV as any);

        if (hits(DISPATCH_URL).length) actions.push("dispatch");
        else if (hits(MARKER_URL).length) actions.push("watchdog");
        else actions.push("skip");
      }

      assert.equal(
        actions.filter((a) => a === "dispatch").length,
        1,
        `expected 1 dispatch, got ${JSON.stringify(actions)}`,
      );
      assert.equal(
        actions.filter((a) => a === "watchdog").length,
        1,
        `expected 1 watchdog, got ${JSON.stringify(actions)}`,
      );
    });

    test(`${label} runs dispatch at 07:55 and watchdog at 10:25 local`, async () => {
      const localTimeOf = (time: string) =>
        new Intl.DateTimeFormat("en-US", {
          timeZone: ENV.TARGET_TZ,
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        }).format(new Date(`${day}${time}`));

      for (const time of CRONS) {
        calls = [];
        stubFetch([
          [DISPATCH_URL, () => new Response(null, { status: 204 })],
          [MARKER_URL, () => json({ scheduled_date: day })],
        ]);
        await worker.scheduled(controller(`${day}${time}`) as any, ENV as any);

        if (hits(DISPATCH_URL).length) assert.equal(localTimeOf(time), "07:55");
        if (hits(MARKER_URL).length) assert.equal(localTimeOf(time), "10:25");
      }
    });
  }

  test("an off-target hour does nothing at all", async () => {
    stubFetch([]);
    // 20:00Z = 13:00 PDT: neither 7 nor 10.
    await worker.scheduled(controller("2026-08-06T20:00:00Z") as any, ENV as any);
    assert.equal(calls.length, 0, "an off-target cron must not make any request");
    assert.match(logs.join("\n"), /Skipping/);
  });

  test("the watchdog slot never dispatches a brief", async () => {
    stubFetch([[MARKER_URL, () => json({ scheduled_date: "2026-08-06" })]]);
    await worker.scheduled(controller("2026-08-06T17:25:00Z") as any, ENV as any);
    assert.equal(hits(DISPATCH_URL).length, 0, "watchdog must never trigger a run");
  });
});

// ---------------------------------------------------------------------------
// Watchdog
// ---------------------------------------------------------------------------
describe("watchdog", () => {
  const AT_1025_PDT = "2026-08-06T17:25:00Z"; // 10:25 local, Pacific date 2026-08-06

  test("stays silent when today's brief was delivered", async () => {
    stubFetch([[MARKER_URL, () => json({ scheduled_date: "2026-08-06" })]]);
    await worker.scheduled(controller(AT_1025_PDT) as any, ENV as any);

    assert.equal(hits(MESSAGES_URL).length, 0, "must not DM on a delivered day");
    assert.equal(hits(DM_OPEN_URL).length, 0, "must not even open a DM channel");
    assert.match(logs.join("\n"), /was delivered — nothing to alert/);
  });

  test("alerts when the marker is stale", async () => {
    stubFetch([
      [MARKER_URL, () => json({ scheduled_date: "2026-08-05" })],
      [DM_OPEN_URL, () => json({ id: "chan-1" })],
      [MESSAGES_URL, () => json({ id: "msg-1" })],
    ]);
    await assert.rejects(
      worker.scheduled(controller(AT_1025_PDT) as any, ENV as any),
      /Alerted: no brief delivered for 2026-08-06/,
    );

    const body = alertBody();
    assert.match(body, /No morning brief for 2026-08-06/);
    assert.match(body, /last delivered: 2026-08-05/);
    assert.match(body, /github\.com\/owner\/repo\/actions/);
  });

  test("alerts with 'never' when the marker does not exist", async () => {
    stubFetch([
      [MARKER_URL, () => new Response("Not Found", { status: 404 })],
      [DM_OPEN_URL, () => json({ id: "chan-1" })],
      [MESSAGES_URL, () => json({ id: "msg-1" })],
    ]);
    await assert.rejects(worker.scheduled(controller(AT_1025_PDT) as any, ENV as any));
    assert.match(alertBody(), /last delivered: never/);
  });

  test("treats a corrupt marker as undelivered rather than delivered", async () => {
    // Fail loud: a marker we cannot parse must never be read as "all good".
    stubFetch([
      [MARKER_URL, () => new Response("{ this is not json", { status: 200 })],
      [DM_OPEN_URL, () => json({ id: "chan-1" })],
      [MESSAGES_URL, () => json({ id: "msg-1" })],
    ]);
    await assert.rejects(worker.scheduled(controller(AT_1025_PDT) as any, ENV as any));
    assert.equal(hits(MESSAGES_URL).length, 1, "a corrupt marker must still alert");
  });

  test("a marker with no scheduled_date key alerts", async () => {
    stubFetch([
      [MARKER_URL, () => json({ something_else: "2026-08-06" })],
      [DM_OPEN_URL, () => json({ id: "chan-1" })],
      [MESSAGES_URL, () => json({ id: "msg-1" })],
    ]);
    await assert.rejects(worker.scheduled(controller(AT_1025_PDT) as any, ENV as any));
    assert.match(alertBody(), /last delivered: never/);
  });

  test("a GitHub error is raised, never silently read as delivered", async () => {
    // The dangerous failure: a 500 swallowed into "delivered" would disable the
    // alarm exactly when GitHub is unhealthy, which is when it is needed.
    stubFetch([[MARKER_URL, () => new Response("boom", { status: 500 })]]);
    await assert.rejects(
      worker.scheduled(controller(AT_1025_PDT) as any, ENV as any),
      /Could not read briefs\/\.delivery\.json \(HTTP 500\)/,
    );
    assert.equal(hits(MESSAGES_URL).length, 0, "must not claim a miss it cannot prove");
  });

  test("reads the marker uncached", async () => {
    // A cached marker would report yesterday's delivery as today's.
    stubFetch([[MARKER_URL, () => json({ scheduled_date: "2026-08-06" })]]);
    await worker.scheduled(controller(AT_1025_PDT) as any, ENV as any);
    assert.equal(hits(MARKER_URL)[0].init.cache, "no-store");
  });

  test("authenticates the marker read against the private repo", async () => {
    stubFetch([[MARKER_URL, () => json({ scheduled_date: "2026-08-06" })]]);
    await worker.scheduled(controller(AT_1025_PDT) as any, ENV as any);

    const headers = hits(MARKER_URL)[0].init.headers;
    assert.equal(headers.Authorization, `Bearer ${ENV.GITHUB_PAT}`);
    assert.equal(headers.Accept, "application/vnd.github.raw");
    assert.ok(headers["User-Agent"], "GitHub rejects requests with no User-Agent");
  });

  test("uses the Pacific date, not the UTC date", async () => {
    // Guards the date helper against a UTC/local mismatch: if the watchdog ever
    // moves to a slot where the two dates differ, this is what catches it.
    const env = { ...ENV, WATCHDOG_LOCAL_HOUR: "17" };
    stubFetch([
      // 2026-08-07T00:25Z is still 2026-08-06 (17:25) in Pacific.
      [MARKER_URL, () => json({ scheduled_date: "2026-08-06" })],
    ]);
    await worker.scheduled(controller("2026-08-07T00:25:00Z") as any, env as any);
    assert.equal(hits(MESSAGES_URL).length, 0, "must compare against the Pacific date");
  });

  test("opens a DM for a user: target", async () => {
    stubFetch([
      [MARKER_URL, () => json({ scheduled_date: "2026-08-05" })],
      [DM_OPEN_URL, () => json({ id: "chan-1" })],
      [MESSAGES_URL, () => json({ id: "msg-1" })],
    ]);
    await assert.rejects(worker.scheduled(controller(AT_1025_PDT) as any, ENV as any));

    const open = hits(DM_OPEN_URL)[0];
    assert.equal(JSON.parse(open.init.body).recipient_id, "123456789012345678");
    assert.match(hits(MESSAGES_URL)[0].url, /channels\/chan-1\/messages/);
  });

  test("posts straight to a channel: target without opening a DM", async () => {
    const env = { ...ENV, DISCORD_TARGET: "channel:999" };
    stubFetch([
      [MARKER_URL, () => json({ scheduled_date: "2026-08-05" })],
      [MESSAGES_URL, () => json({ id: "msg-1" })],
    ]);
    await assert.rejects(worker.scheduled(controller(AT_1025_PDT) as any, env as any));

    assert.equal(hits(DM_OPEN_URL).length, 0);
    assert.match(hits(MESSAGES_URL)[0].url, /channels\/999\/messages/);
  });

  test("rejects a malformed DISCORD_TARGET", async () => {
    const env = { ...ENV, DISCORD_TARGET: "123456789012345678" }; // missing prefix
    stubFetch([[MARKER_URL, () => json({ scheduled_date: "2026-08-05" })]]);
    await assert.rejects(
      worker.scheduled(controller(AT_1025_PDT) as any, env as any),
      /must start with 'user:' or 'channel:'/,
    );
  });

  test("fails loudly when a miss cannot be reported", async () => {
    const env = { ...ENV, DISCORD_BOT_TOKEN: "", DISCORD_TARGET: "" };
    stubFetch([[MARKER_URL, () => json({ scheduled_date: "2026-08-05" })]]);
    await assert.rejects(
      worker.scheduled(controller(AT_1025_PDT) as any, env as any),
      /is MISSING and DISCORD_BOT_TOKEN\/DISCORD_TARGET are unset/,
    );
  });

  test("surfaces a rejected Discord send instead of swallowing it", async () => {
    stubFetch([
      [MARKER_URL, () => json({ scheduled_date: "2026-08-05" })],
      [DM_OPEN_URL, () => json({ id: "chan-1" })],
      [MESSAGES_URL, () => new Response("unauthorized", { status: 401 })],
    ]);
    await assert.rejects(
      worker.scheduled(controller(AT_1025_PDT) as any, ENV as any),
      /alert send failed \(HTTP 401\)/,
    );
  });
});

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------
describe("dispatch", () => {
  const AT_0755_PDT = "2026-08-06T14:55:00Z";

  test("posts the configured event type once on success", async () => {
    stubFetch([[DISPATCH_URL, () => new Response(null, { status: 204 })]]);
    await worker.scheduled(controller(AT_0755_PDT) as any, ENV as any);

    const post = hits(DISPATCH_URL)[0];
    assert.equal(hits(DISPATCH_URL).length, 1);
    assert.equal(post.method, "POST");
    assert.equal(post.url, "https://api.github.com/repos/owner/repo/dispatches");
    assert.equal(JSON.parse(post.init.body).event_type, "morning-brief");
  });

  test("does not retry a credential failure", async () => {
    // Retrying a bad PAT cannot fix it and just burns the same wrong token.
    for (const status of [401, 403, 404]) {
      calls = [];
      stubFetch([[DISPATCH_URL, () => new Response("nope", { status })]]);
      await assert.rejects(
        worker.scheduled(controller(AT_0755_PDT) as any, ENV as any),
        /not retrying/,
      );
      assert.equal(hits(DISPATCH_URL).length, 1, `HTTP ${status} must not retry`);
    }
  });

  test("retries a transient failure three times, then gives up loudly", async () => {
    stubFetch([[DISPATCH_URL, () => new Response("upstream", { status: 500 })]]);
    await assert.rejects(
      worker.scheduled(controller(AT_0755_PDT) as any, ENV as any),
      /Dispatch failed after 3 attempts/,
    );
    assert.equal(hits(DISPATCH_URL).length, 3);
  });

  test("stops retrying as soon as one attempt succeeds", async () => {
    let n = 0;
    stubFetch([
      [
        DISPATCH_URL,
        () => (++n === 1 ? new Response("flaky", { status: 502 }) : new Response(null, { status: 204 })),
      ],
    ]);
    await worker.scheduled(controller(AT_0755_PDT) as any, ENV as any);
    assert.equal(hits(DISPATCH_URL).length, 2);
  });

  test("does not dispatch, or throw, when the PAT is missing", async () => {
    // Deploy-before-secret. The GitHub backstop crons still cover delivery, so
    // this logs and returns rather than erroring every morning.
    const env = { ...ENV, GITHUB_PAT: "" };
    stubFetch([]);
    await worker.scheduled(controller(AT_0755_PDT) as any, env as any);

    assert.equal(calls.length, 0);
    assert.match(logs.join("\n"), /GITHUB_PAT is not set/);
  });
});

// ---------------------------------------------------------------------------
// Status endpoint
// ---------------------------------------------------------------------------
describe("fetch handler", () => {
  test("reports config and can never trigger anything", async () => {
    stubFetch([]);
    const resp = await worker.fetch(new Request("https://example.com/"), ENV as any);
    const body = await resp.text();

    assert.equal(resp.status, 200);
    assert.match(body, /owner\/repo/);
    assert.match(body, /7:55/);
    assert.match(body, /10:25/);
    assert.equal(calls.length, 0, "the status endpoint must not make requests");
    assert.doesNotMatch(body, /pat-for-tests|bot-token-for-tests/, "must not leak secrets");
  });
});
