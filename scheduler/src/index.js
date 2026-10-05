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
  "0 14 * * *": ["dc-record-campaigns.yml", "dc-record-officials.yml", "dc-record-feds.yml", "dc-record-congress.yml"], // AM (~7:00am PDT)
  "45 22 * * *": ["dc-record-campaigns.yml", "dc-record-officials.yml", "dc-record-feds.yml", "dc-record-congress.yml"], // PM
  // Usage-window pings, ~4h ahead of each batch (see dc-record-window-ping.yml)
  "0 10 * * *": ["dc-record-window-ping.yml"], // ahead of AM
  "45 18 * * *": ["dc-record-window-ping.yml"], // ahead of PM
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
// subscribers, for a pure content-only smoke test. Congress and the window
// ping map to null because they never email either way, and their
// workflow_dispatch declares no inputs at all (passing any would 422).
const MANUAL_SKIP_INPUT = {
  "dc-record-campaigns.yml": { skip_email: "true" },
  "dc-record-officials.yml": { site_refresh_only: "true" },
  "dc-record-feds.yml": { site_refresh_only: "true" },
  "dc-record-congress.yml": null,
  "dc-record-window-ping.yml": null,
};

function inputsFor(cron, workflowFile, { dry }) {
  if (dry) return MANUAL_SKIP_INPUT[workflowFile] || undefined; // manual test: never risk a real send
  const mode = CRON_MODE[cron];
  if (!mode || workflowFile === "dc-record-congress.yml") return undefined; // Congress/ping take no inputs
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

async function fireForCron(cron, env, { dry = false } = {}) {
  const workflows = CRON_TO_WORKFLOWS[cron];
  if (!workflows) return { cron, error: "no workflow mapping for this cron string" };

  const results = await Promise.allSettled(
    workflows.map((wf) => dispatchWithRetry(wf, inputsFor(cron, wf, { dry }), env))
  );

  const failures = results.map((r, i) => (r.status === "rejected" ? `${workflows[i]}: ${String(r.reason)}` : null)).filter(Boolean);
  if (failures.length) await alertFailure(failures);

  return {
    cron,
    dry,
    results: results.map((r, i) => ({
      workflow: workflows[i],
      ok: r.status === "fulfilled",
      error: r.status === "rejected" ? String(r.reason) : undefined,
    })),
  };
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(
      fireForCron(controller.cron, env).then((r) => console.log(JSON.stringify(r)))
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
