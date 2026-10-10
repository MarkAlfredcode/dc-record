// DC Record scheduler
//
// Cloudflare Cron Triggers fire this Worker at exact times, and it calls
// GitHub's `workflow_dispatch` API for the right dc-record edition(s).
// This exists because GitHub Actions' own `schedule:` event is best-effort
// and was observed firing this repo's editions ~3.5h late every day.
//
// Keep CRON_TO_WORKFLOWS/CRON_MODE/MANUAL_SKIP_INPUT in sync by hand with
// wrangler.toml's [triggers].crons and with the (disabled) `schedule:`
// comments left in each ../.github/workflows/*.yml for reference.

const REPO = "MarkAlfredcode/dc-record";

const CRON_TO_WORKFLOWS = {
  "0 14 * * *": ["dc-record-campaigns.yml", "dc-record-officials.yml", "dc-record-feds.yml", "dc-record-congress.yml", "dc-record-conflicts.yml"], // AM (~7:00am PDT)
  "45 22 * * *": ["dc-record-campaigns.yml", "dc-record-officials.yml", "dc-record-feds.yml", "dc-record-congress.yml", "dc-record-conflicts.yml"], // PM
  // Usage-window pings moved 2026-10-10 to an hourly schedule in the public
  // MarkAlfredcode/cron-utils repo (free Actions minutes there).
};

// Which subscriber group (see markalfred.news/dc-record-recipients) a real
// dispatch of each cron slot should email -- the 3 emailing editions'
// `mode` workflow_dispatch input, added 2026-09-16. Before this, the PM
// slot always forced a no-email site-refresh; now it really sends, but only
// to whoever opted into the PM edition (see MANUAL_SKIP_INPUT below for the
// one case where a run still shouldn't try to send at all). Not used for
// Congress, which never emails either way.
const CRON_MODE = {
  "0 14 * * *": "am",
  "45 22 * * *": "pm",
};

// Manual override, used ONLY for a dry test dispatch (never a real cron
// fire) -- forces a run to skip sending email entirely regardless of live
// subscribers, for a pure content-only smoke test. Congress and Conflicts
// map to null because they never email either way, and their
// workflow_dispatch declares no inputs at all (passing any would 422).
const MANUAL_SKIP_INPUT = {
  "dc-record-campaigns.yml": { skip_email: "true" },
  "dc-record-officials.yml": { site_refresh_only: "true" },
  "dc-record-feds.yml": { site_refresh_only: "true" },
  "dc-record-congress.yml": null,
  "dc-record-conflicts.yml": null,
};

function inputsFor(cron, workflowFile, { dry }) {
  if (dry) return MANUAL_SKIP_INPUT[workflowFile] || undefined; // manual test: never risk a real send
  const mode = CRON_MODE[cron];
  if (!mode || workflowFile === "dc-record-congress.yml" || workflowFile === "dc-record-conflicts.yml") return undefined; // Congress/Conflicts/ping take no inputs
  return { mode };
}

async function dispatch(workflowFile, inputs, env) {
  const body = { ref: "main" };
  if (inputs) body.inputs = inputs;

  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflowFile}/dispatches`,
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
      `https://api.github.com/repos/${REPO}/actions/workflows/${workflowFile}/runs?event=workflow_dispatch&per_page=1&created=${encodeURIComponent(">=" + sinceIso)}`,
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

async function fireForCron(cron, env, { dry = false, scheduledTime = null } = {}) {
  const workflows = CRON_TO_WORKFLOWS[cron];
  if (!workflows) return { cron, error: "no workflow mapping for this cron string" };

  // Manual test hits (no scheduledTime) are never deduped.
  const sinceIso = scheduledTime
    ? new Date(Math.floor(scheduledTime / 60000) * 60000).toISOString().replace(/\.\d{3}Z$/, "Z")
    : null;

  const results = await Promise.allSettled(
    workflows.map(async (wf) => {
      if (sinceIso && (await alreadyDispatched(wf, sinceIso, env))) return "skipped (already dispatched this slot)";
      await dispatchWithRetry(wf, inputsFor(cron, wf, { dry }), env);
      return "dispatched";
    })
  );

  const failures = results.map((r, i) => (r.status === "rejected" ? `${workflows[i]}: ${String(r.reason)}` : null)).filter(Boolean);
  if (failures.length) await alertFailure(failures);

  return {
    cron,
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
    ctx.waitUntil(
      fireForCron(controller.cron, env, { scheduledTime: controller.scheduledTime }).then((r) => console.log(JSON.stringify(r)))
    );
  },

  // Manual test hook: GET /?key=<TRIGGER_SECRET>&cron=<exact cron string>[&dry=true]
  // `dry=true` forces the safe/no-email input for every workflow that has
  // one, so a manual test never risks a real duplicate send.
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!env.TRIGGER_SECRET || url.searchParams.get("key") !== env.TRIGGER_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const cron = url.searchParams.get("cron");
    if (!cron || !CRON_TO_WORKFLOWS[cron]) {
      return new Response(
        `usage: ?key=...&cron=<one of: ${Object.keys(CRON_TO_WORKFLOWS).join(" | ")}>[&dry=true]`,
        { status: 400 }
      );
    }
    const dry = url.searchParams.get("dry") === "true";
    const result = await fireForCron(cron, env, { dry });
    return new Response(JSON.stringify(result, null, 2), {
      headers: { "content-type": "application/json" },
    });
  },
};
