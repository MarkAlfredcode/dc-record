// DC Record scheduler
//
// Cloudflare Cron Triggers fire this Worker at exact times, and it calls
// GitHub's `workflow_dispatch` API for the right dc-record edition(s).
// This exists because GitHub Actions' own `schedule:` event is best-effort
// and was observed firing this repo's editions ~3.5h late every day.
//
// The schedule lives in the constants below (Eastern hours), not in
// wrangler.toml; keep MANUAL_SKIP_INPUT in sync with each workflow's inputs.

const REPO = "MarkAlfredcode/dc-record";
// Workflows that live in a different repo (default is REPO).
const PING_REPO = "MarkAlfredcode/cron-utils";
const PING = "ping.yml";
const repoFor = (wf) => (wf === PING ? PING_REPO : REPO);
const EDITIONS = ["dc-record-campaigns.yml", "dc-record-officials.yml", "dc-record-feds.yml", "dc-record-congress.yml"];

// Slots, not cron strings: the Worker has ONE trigger (`0 * * * *`, hourly,
// UTC) and decides per tick what to fire from the current US Eastern hour
// (America/New_York, so EST/EDT switches automatically -- no March/November
// edits needed). Eastern schedule:
//   am   -- 8:00am   Campaigns / Officials / Feds (mode "am") + Congress
//   pm   -- 3:00pm   same four workflows (mode "pm")
//   ping -- hourly 4am..9pm  usage-window ping (cron-utils/ping.yml)
const AM_HOUR = 8;
const PM_HOUR = 15;
const PING_FIRST_HOUR = 4;
const PING_LAST_HOUR = 21; // 9pm

const SLOT_WORKFLOWS = {
  am: EDITIONS,
  pm: EDITIONS,
  ping: [PING],
};

// Which subscriber group (see markalfred.news/dc-record-recipients) a real
// dispatch of each slot should email -- the 3 emailing editions' `mode`
// workflow_dispatch input. Not used for Congress/ping, which take no input.
const SLOT_MODE = { am: "am", pm: "pm" };

function easternHour(ms) {
  const h = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", hourCycle: "h23" })
    .formatToParts(new Date(ms))
    .find((p) => p.type === "hour").value;
  return Number(h);
}

function slotsAt(ms) {
  const h = easternHour(ms);
  const slots = [];
  if (h === AM_HOUR) slots.push("am");
  if (h === PM_HOUR) slots.push("pm");
  if (h >= PING_FIRST_HOUR && h <= PING_LAST_HOUR) slots.push("ping");
  return slots;
}

// Manual override, used ONLY for a dry test dispatch (never a real cron
// fire) -- forces a run to skip sending email entirely regardless of live
// subscribers, for a pure content-only smoke test. Congress maps to null
// because it never emails either way, and its workflow_dispatch declares no
// inputs at all (passing any would 422).
const MANUAL_SKIP_INPUT = {
  "dc-record-campaigns.yml": { skip_email: "true" },
  "dc-record-officials.yml": { site_refresh_only: "true" },
  "dc-record-feds.yml": { site_refresh_only: "true" },
  "dc-record-congress.yml": null,
  "ping.yml": null,
};

function inputsFor(slot, workflowFile, { dry }) {
  if (dry) return MANUAL_SKIP_INPUT[workflowFile] || undefined; // manual test: never risk a real send
  const mode = SLOT_MODE[slot];
  if (!mode || workflowFile === "dc-record-congress.yml" || workflowFile === PING) return undefined; // Congress/ping take no inputs
  return { mode };
}

async function dispatch(workflowFile, inputs, env) {
  const repo = repoFor(workflowFile);
  const body = { ref: "main" };
  if (inputs) body.inputs = inputs;

  const res = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflowFile}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "dc-record-scheduler",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    }
  );
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`dispatch ${workflowFile} failed: ${res.status} ${text}`);
  }
}

async function dispatchWithRetry(workflowFile, inputs, env) {
  let lastErr;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      return await dispatch(workflowFile, inputs, env);
    } catch (e) {
      lastErr = e;
      await new Promise((r) => setTimeout(r, 2000 * (attempt + 1)));
    }
  }
  throw lastErr;
}

async function alertFailure(failures) {
  await fetch("https://ntfy.sh/markalfreddotnews", {
    method: "POST",
    headers: { Title: "DC Record dispatch FAILED", Tags: "rotating_light" },
    body: failures.join("\n").slice(0, 900),
  }).catch(() => {});
}

// Cloudflare doesn't guarantee one invocation per cron tick: on 2026-10-05
// (right after a redeploy) it fired "45 18" and "45 22" twice each, seconds
// apart, and every PM subscriber got two copies. So a scheduled fire first
// asks GitHub whether this workflow already has a dispatch run created since
// the start of this slot's minute, and skips it if so. Fails open (dispatches
// anyway) if GitHub can't be asked -- a duplicate beats a missed edition, and
// the emailing workflows have their own data/sent/ guard as a backstop.
async function alreadyDispatched(workflowFile, sinceIso, env) {
  try {
    const res = await fetch(
      `https://api.github.com/repos/${repoFor(workflowFile)}/actions/workflows/${workflowFile}/runs?event=workflow_dispatch&per_page=1&created=${encodeURIComponent(">=" + sinceIso)}`,
      {
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "dc-record-scheduler",
          "X-GitHub-Api-Version": "2022-11-28",
        },
      }
    );
    if (!res.ok) return false;
    const data = await res.json();
    return (data.total_count || 0) > 0;
  } catch {
    return false;
  }
}

async function fireForSlot(slot, env, { dry = false, scheduledTime = null } = {}) {
  const workflows = SLOT_WORKFLOWS[slot];
  if (!workflows) return { slot, error: "unknown slot" };

  // Manual test hits (no scheduledTime) are never deduped.
  const sinceIso = scheduledTime
    ? new Date(Math.floor(scheduledTime / 60000) * 60000).toISOString().replace(/\.\d{3}Z$/, "Z")
    : null;

  const results = await Promise.allSettled(
    workflows.map(async (wf) => {
      if (sinceIso && (await alreadyDispatched(wf, sinceIso, env))) return "skipped (already dispatched this slot)";
      await dispatchWithRetry(wf, inputsFor(slot, wf, { dry }), env);
      return "dispatched";
    })
  );

  const failures = results.map((r, i) => (r.status === "rejected" ? `${workflows[i]}: ${String(r.reason)}` : null)).filter(Boolean);
  if (failures.length) await alertFailure(failures);

  return {
    slot,
    dry,
    results: results.map((r, i) => ({
      workflow: workflows[i],
      ok: r.status === "fulfilled",
      outcome: r.status === "fulfilled" ? r.value : undefined,
      error: r.status === "rejected" ? String(r.reason) : undefined,
    })),
  };
}

export default {
  async scheduled(controller, env, ctx) {
    const slots = slotsAt(controller.scheduledTime);
    ctx.waitUntil(
      Promise.all(slots.map((slot) => fireForSlot(slot, env, { scheduledTime: controller.scheduledTime }))).then((r) =>
        console.log(JSON.stringify({ easternHour: easternHour(controller.scheduledTime), slots, results: r }))
      )
    );
  },

  // Manual test hook: GET /?key=<TRIGGER_SECRET>&slot=<am|pm|ping>[&dry=true]
  // `dry=true` forces the safe/no-email input for every workflow that has
  // one, so a manual test never risks a real duplicate send.
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!env.TRIGGER_SECRET || url.searchParams.get("key") !== env.TRIGGER_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const slot = url.searchParams.get("slot");
    if (!slot || !SLOT_WORKFLOWS[slot]) {
      return new Response(`usage: ?key=...&slot=<one of: ${Object.keys(SLOT_WORKFLOWS).join(" | ")}>[&dry=true]`, { status: 400 });
    }
    const dry = url.searchParams.get("dry") === "true";
    const result = await fireForSlot(slot, env, { dry });
    return new Response(JSON.stringify(result, null, 2), {
      headers: { "content-type": "application/json" },
    });
  },
};
