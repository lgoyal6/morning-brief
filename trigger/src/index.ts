/**
 * Fires the morning brief on time, and checks that it actually arrived.
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
 *
 * The WATCHDOG lives here too, and for a sharper reason. It used to be a GitHub
 * cron (.github/workflows/brief-watchdog.yml), which made it useless in exactly
 * the case it exists for: on 2026-08-06 GitHub Actions had a multi-hour outage,
 * every queued job died with "not acquired by Runner of type hosted" — and the
 * watchdog, being another GitHub job, died the same way instead of reporting it.
 * An alarm wired to the thing it is watching is not an alarm. Out here it shares
 * no infrastructure with either the brief or GitHub, so an Actions outage now
 * produces a DM rather than silence.
 */

export interface Env {
  /** Fine-grained PAT, repo scope, Contents: read+write. Set via `wrangler secret put`. */
  GITHUB_PAT: string;
  GITHUB_REPO: string;
  GITHUB_API_BASE: string;
  DISPATCH_EVENT_TYPE: string;
  TARGET_TZ: string;
  TARGET_LOCAL_HOUR: string;
  /** Local hour at which the watchdog checks that the brief landed. */
  WATCHDOG_LOCAL_HOUR: string;
  /** Repo-relative path of the committed delivery marker. */
  DELIVERY_MARKER_PATH: string;
  /** Discord bot token for the watchdog alert. Secret. */
  DISCORD_BOT_TOKEN: string;
  /** `user:<id>` or `channel:<id>`, same format as the Python pipeline. Secret. */
  DISCORD_TARGET: string;
}

const DISCORD_API = "https://discord.com/api/v10";

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

/** Calendar date in `tz` as YYYY-MM-DD (en-CA is the locale that formats that way). */
function dateIn(tz: string, date: Date): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function githubHeaders(env: Env, accept: string): HeadersInit {
  return {
    Accept: accept,
    Authorization: `Bearer ${env.GITHUB_PAT}`,
    "X-GitHub-Api-Version": "2022-11-28",
    // GitHub rejects requests with no User-Agent.
    "User-Agent": "morning-brief-trigger",
  };
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
        ...githubHeaders(env, "application/vnd.github+json"),
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

/**
 * Date the last scheduled run claimed the morning-delivery slot, or null if the
 * marker is missing/unreadable. Mirrors `common.scheduled_delivered_date()`.
 *
 * The marker is the single source of truth for "did the brief actually reach
 * Discord", because send_discord.py only writes it after Discord confirms.
 */
async function fetchDeliveredDate(env: Env): Promise<string | null> {
  const url =
    `${env.GITHUB_API_BASE}/repos/${env.GITHUB_REPO}/contents/` +
    `${env.DELIVERY_MARKER_PATH}?ref=main`;

  const resp = await fetch(url, {
    headers: githubHeaders(env, "application/vnd.github.raw"),
    // A cached marker would report yesterday's delivery as today's and silence
    // a real miss — the one error this function must never make.
    cache: "no-store",
  });

  if (resp.status === 404) {
    // No scheduled run has ever recorded a delivery.
    return null;
  }
  if (!resp.ok) {
    throw new Error(
      `Could not read ${env.DELIVERY_MARKER_PATH} (HTTP ${resp.status}): ` +
        `${(await resp.text()).slice(0, 300)}`,
    );
  }

  const raw = await resp.text();
  try {
    return (JSON.parse(raw) as { scheduled_date?: string }).scheduled_date ?? null;
  } catch {
    // Corrupt marker is indistinguishable from no marker, and the safe reading
    // of "I cannot tell" is "assume not delivered" — a false alarm beats silence.
    console.warn(`Delivery marker is not valid JSON: ${raw.slice(0, 200)}`);
    return null;
  }
}

/** Resolve DISCORD_TARGET to a channel id, opening a DM if it names a user. */
async function resolveChannelId(env: Env): Promise<string> {
  const target = env.DISCORD_TARGET.trim();

  if (target.startsWith("channel:")) {
    return target.slice("channel:".length).trim();
  }
  if (!target.startsWith("user:")) {
    throw new Error(
      `DISCORD_TARGET must start with 'user:' or 'channel:' (got: ${target.slice(0, 40)})`,
    );
  }

  const userId = target.slice("user:".length).trim();
  const resp = await fetch(`${DISCORD_API}/users/@me/channels`, {
    method: "POST",
    headers: {
      Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ recipient_id: userId }),
  });

  if (!resp.ok) {
    throw new Error(
      `Discord: failed to open DM with user ${userId} (HTTP ${resp.status}): ` +
        `${(await resp.text()).slice(0, 300)}\n` +
        "Hint: the bot must share a server with you and your DM privacy must allow it.",
    );
  }
  return ((await resp.json()) as { id: string }).id;
}

async function sendDiscordText(env: Env, content: string): Promise<void> {
  const channelId = await resolveChannelId(env);
  const resp = await fetch(`${DISCORD_API}/channels/${channelId}/messages`, {
    method: "POST",
    headers: {
      Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ content }),
  });

  if (!resp.ok) {
    throw new Error(
      `Discord: alert send failed (HTTP ${resp.status}): ${(await resp.text()).slice(0, 500)}`,
    );
  }
  console.log(`Discord: alert sent to channel ${channelId}.`);
}

/**
 * Alert if today's brief never got delivered.
 *
 * Checks the OUTCOME, not any particular run: is today's delivery marker
 * committed? Deliberately dumb — no run inspection, no Actions API. That is what
 * keeps it working when Actions is the thing that is broken.
 */
async function watchdog(env: Env, now: Date): Promise<void> {
  const today = dateIn(env.TARGET_TZ, now);
  const delivered = await fetchDeliveredDate(env);

  if (delivered === today) {
    console.log(`Brief for ${today} was delivered — nothing to alert.`);
    return;
  }

  const localTime = new Intl.DateTimeFormat("en-US", {
    timeZone: env.TARGET_TZ,
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
    hour12: false,
  }).format(now);

  const message = [
    `🚨 **No morning brief for ${today}.**`,
    `It is ${localTime} and no delivery has been recorded ` +
      `(last delivered: ${delivered ?? "never"}).`,
    "",
    "Likely causes, in order of past frequency:",
    "• GitHub Actions is degraded — jobs queue then die with " +
      '"not acquired by Runner of type hosted" (2026-08-06)',
    "• GitHub dropped the scheduled run (it fires nothing and reports nothing)",
    "• every generation attempt failed — check the run log",
    "• the brief generated but Discord rejected the send",
    "",
    `https://github.com/${env.GITHUB_REPO}/actions/workflows/morning-brief.yml`,
  ].join("\n");

  if (!env.DISCORD_BOT_TOKEN || !env.DISCORD_TARGET) {
    // No way to reach you — still fail loudly so the errored cron is the signal.
    throw new Error(
      `Brief for ${today} is MISSING and DISCORD_BOT_TOKEN/DISCORD_TARGET are ` +
        "unset, so no alert could be sent.",
    );
  }

  await sendDiscordText(env, message);
  // Errored on purpose: the Discord DM is the primary alert, a red cron
  // invocation in the Workers dashboard is the backup in case the DM itself is
  // the problem. Cron triggers are not retried, so this cannot double-send.
  throw new Error(`Alerted: no brief delivered for ${today}.`);
}

export default {
  /**
   * Four crons, two jobs. Cloudflare cron is UTC, so each job needs a DST pair;
   * the handler routes purely on the local Pacific hour, which means the cron
   * expressions live in wrangler.jsonc only and are never duplicated here.
   *   14:55Z / 15:55Z -> whichever is 07:55 local -> dispatch the brief
   *   17:25Z / 18:25Z -> whichever is 10:25 local -> check that it arrived
   */
  async scheduled(event: ScheduledController, env: Env): Promise<void> {
    const now = new Date(event.scheduledTime);
    const localHour = hourIn(env.TARGET_TZ, now);

    if (localHour === Number(env.WATCHDOG_LOCAL_HOUR)) {
      console.log(`Local hour ${localHour} is the watchdog slot — checking delivery.`);
      await watchdog(env, now);
      return;
    }

    if (localHour !== Number(env.TARGET_LOCAL_HOUR)) {
      // The other cron in the pair is the live one today. Expected, not an error.
      console.log(
        `Skipping: local hour in ${env.TARGET_TZ} is ${localHour}, targets are ` +
          `${env.TARGET_LOCAL_HOUR} (dispatch) and ${env.WATCHDOG_LOCAL_HOUR} (watchdog) ` +
          "(the other cron in the pair handles this half of the year).",
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
        `repo:     ${env.GITHUB_REPO}`,
        `event:    ${env.DISPATCH_EVENT_TYPE}`,
        `dispatch: ${env.TARGET_LOCAL_HOUR}:55 ${env.TARGET_TZ}`,
        `watchdog: ${env.WATCHDOG_LOCAL_HOUR}:25 ${env.TARGET_TZ}`,
        `now:      ${hourIn(env.TARGET_TZ, now)}:xx local / ${now.toISOString()}`,
        "",
        "Cron-only. This endpoint reports config and never dispatches.",
      ].join("\n"),
      { headers: { "Content-Type": "text/plain; charset=utf-8" } },
    );
  },
};
