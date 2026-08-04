/**
 * Fires the morning brief on time.
 *
 * GitHub's own scheduler is the reason this exists. Measured on this repo,
 * `schedule`-triggered runs started 45-90 minutes late every day and were
 * sometimes dropped entirely, producing no run and therefore no alert. Runs
 * started by an API dispatch, by contrast, began within ~15 seconds every time.
 * So the trigger moves out of GitHub while the work stays in Actions.
 *
 * The GitHub crons in morning-brief.yml are deliberately left in place as
 * backstops. Whichever trigger arrives first delivers and claims the day's slot;
 * the rest see the committed marker and skip, so redundancy cannot double-send.
 */

export interface Env {
  /** Fine-grained PAT, repo scope, Contents: read+write. Set via `wrangler secret put`. */
  GITHUB_PAT: string;
  GITHUB_REPO: string;
  GITHUB_API_BASE: string;
  DISPATCH_EVENT_TYPE: string;
  TARGET_TZ: string;
  TARGET_LOCAL_HOUR: string;
}

/** Hour (0-23) of `date` in `tz`. */
function hourIn(tz: string, date: Date): number {
  const hour = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    hour: "numeric",
    hour12: false,
  }).format(date);
  // hour12:false can yield "24" for midnight in some ICU versions.
  return Number(hour) % 24;
}

async function dispatch(env: Env): Promise<void> {
  const url = `${env.GITHUB_API_BASE}/repos/${env.GITHUB_REPO}/dispatches`;
  const body = JSON.stringify({
    event_type: env.DISPATCH_EVENT_TYPE,
    client_payload: { source: "cloudflare-cron" },
  });

  let lastError = "";
  for (let attempt = 1; attempt <= 3; attempt++) {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_PAT}`,
        "X-GitHub-Api-Version": "2022-11-28",
        // GitHub rejects requests with no User-Agent.
        "User-Agent": "morning-brief-trigger",
        "Content-Type": "application/json",
      },
      body,
    });

    if (resp.status === 204) {
      console.log(`Dispatched ${env.DISPATCH_EVENT_TYPE} to ${env.GITHUB_REPO}.`);
      return;
    }

    lastError = `HTTP ${resp.status}: ${(await resp.text()).slice(0, 300)}`;
    // 401/403/404 are configuration problems — a bad, expired, or under-scoped
    // PAT. Retrying cannot fix them, and each retry burns the same wrong token.
    if (resp.status === 401 || resp.status === 403 || resp.status === 404) {
      throw new Error(`Dispatch rejected, not retrying — ${lastError}`);
    }
    console.warn(`Dispatch attempt ${attempt}/3 failed — ${lastError}`);
    if (attempt < 3) {
      await new Promise((r) => setTimeout(r, attempt * 2000));
    }
  }
  // Throwing marks the cron invocation as errored, so it shows up in
  // `wrangler tail` and the Workers dashboard rather than failing silently.
  throw new Error(`Dispatch failed after 3 attempts — ${lastError}`);
}

export default {
  async scheduled(event: ScheduledController, env: Env): Promise<void> {
    const now = new Date(event.scheduledTime);
    const localHour = hourIn(env.TARGET_TZ, now);
    const target = Number(env.TARGET_LOCAL_HOUR);

    if (localHour !== target) {
      // The other cron in the pair is the live one today. Expected, not an error.
      console.log(
        `Skipping: local hour in ${env.TARGET_TZ} is ${localHour}, target is ${target} ` +
          `(the other cron handles this half of the year).`,
      );
      return;
    }

    if (!env.GITHUB_PAT) {
      // The Worker is deployed before the secret exists. Say so plainly instead
      // of failing with an opaque 401 — and stay quiet-ish, because the GitHub
      // crons are still delivering the brief meanwhile.
      console.error(
        "GITHUB_PAT is not set — cannot dispatch. Run `wrangler secret put " +
          "GITHUB_PAT` in trigger/. The GitHub crons are still covering delivery.",
      );
      return;
    }

    console.log(`Local hour ${localHour} matches target — dispatching.`);
    await dispatch(env);
  },

  /** Status only. Deliberately cannot trigger anything: a public URL that fired
   *  the brief would let anyone run it, and there is already a button for that. */
  async fetch(_req: Request, env: Env): Promise<Response> {
    const now = new Date();
    return new Response(
      [
        "morning-brief-trigger",
        `repo:   ${env.GITHUB_REPO}`,
        `event:  ${env.DISPATCH_EVENT_TYPE}`,
        `target: ${env.TARGET_LOCAL_HOUR}:55 ${env.TARGET_TZ}`,
        `now:    ${hourIn(env.TARGET_TZ, now)}:xx local / ${now.toISOString()}`,
        "",
        "Cron-only. This endpoint reports config and never dispatches.",
      ].join("\n"),
      { headers: { "Content-Type": "text/plain; charset=utf-8" } },
    );
  },
};
