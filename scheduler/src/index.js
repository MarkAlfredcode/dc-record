// DC Record scheduler
//
// Cloudflare Cron Triggers fire this Worker at exact times, and it calls
// GitHub's `workflow_dispatch` API for the right dc-record edition(s).
// This exists because GitHub Actions' own `schedule:` event is best-effort
// and was observed firing this repo's editions ~3.5h late every day.
//
// Keep CRON_TO_WORKFLOWS/PM_CRONS/SAFE_INPUT in sync by hand with
// wrangler.toml's [triggers].crons and with the (disabled) `schedule:`
// comments left in each ../.github/workflows/*.yml for reference.

const REPO = "MarkAlfredcode/dc-record";

const CRON_TO_WORKFLOWS = {
  "0 10 * * *": ["dc-record-fec.yml", "dc-record-lobbying.yml", "dc-record-contracts.yml"], // AM
  "45 22 * * *": ["dc-record-fec.yml", "dc-record-lobbying.yml", "dc-record-contracts.yml"], // PM site-refresh
  "25 10 * * *": ["dc-record-courts.yml"], // AM
  "10 23 * * *": ["dc-record-courts.yml"], // PM site-refresh
};

// Crons that are the "3:45pm-style" site-refresh-only slot -- these need the
// safe/no-email input passed below. Courts' PM slot (10 23 * * *) isn't
// listed here because that edition never emails either way, so it needs no
// input at all (and its workflow_dispatch declares none -- passing any would
// 422).
const PM_CRONS = new Set(["45 22 * * *"]);

// The workflow_dispatch input that makes each edition generate content
// without sending email -- what the old `github.event.schedule == '45 22 *
// * *'` check used to gate on, before schedule: was disabled in favor of
// this scheduler. fec already had a `skip_email` input for manual test runs;
// lobbying/contracts got a new `site_refresh_only` input added to match.
const SAFE_INPUT = {
  "dc-record-fec.yml": { skip_email: "true" },
  "dc-record-lobbying.yml": { site_refresh_only: "true" },
  "dc-record-contracts.yml": { site_refresh_only: "true" },
  "dc-record-courts.yml": null,
};

function inputsFor(cron, workflowFile, { dry }) {
  const safe = SAFE_INPUT[workflowFile];
  if (dry) return safe || undefined; // manual test: never risk a real send
  if (PM_CRONS.has(cron)) return safe || undefined; // real PM run: site-refresh only
  return undefined; // real AM run: normal send, no special input
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

async function fireForCron(cron, env, { dry = false } = {}) {
  const workflows = CRON_TO_WORKFLOWS[cron];
  if (!workflows) return { cron, error: "no workflow mapping for this cron string" };

  const results = await Promise.allSettled(
    workflows.map((wf) => dispatch(wf, inputsFor(cron, wf, { dry }), env))
  );

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
