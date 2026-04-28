import base64
import json
import os
import time

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

SIDECAR = os.getenv("SIDECAR_URL", "http://localhost:5000")
AGENT_APP_ID = os.getenv("AGENT_APP_ID", "")
BLUEPRINT_APP_ID = os.getenv("BLUEPRINT_APP_ID", "")
TENANT_ID = os.getenv("TENANT_ID", "")
# Foreign Agent: an Agent Identity App ID parented by a DIFFERENT Blueprint.
# Used in Step 5 to prove Entra rejects cross-Blueprint impersonation attempts.
FOREIGN_AGENT_APP_ID = os.getenv("FOREIGN_AGENT_APP_ID", "")

app = FastAPI()

# Captured at app startup so the sidecar's MSAL cache (which only keys on
# client_id + scope + tenant, not AgentIdentity) doesn't pollute the demo.
SNAPSHOT = {"blueprint": None, "agent": None, "captured_at": None}


def decode_jwt(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {"_error": "not a JWT"}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception as e:
        return {"_error": str(e)}


def fmt_exp(claims: dict) -> str:
    exp = claims.get("exp")
    if not exp:
        return ""
    delta = int(exp) - int(time.time())
    if delta < 0:
        return f"(expired {-delta}s ago)"
    return f"(expires in {delta // 60}m {delta % 60}s)"


def fetch_token(as_agent: bool, correlation_id: str | None = None, force_refresh: bool = False, agent_id: str | None = None) -> dict:
    qs = {}
    effective_agent = agent_id if agent_id else AGENT_APP_ID
    if as_agent:
        qs["AgentIdentity"] = effective_agent
    if correlation_id:
        qs["optionsOverride.AcquireTokenOptions.CorrelationId"] = correlation_id
    if force_refresh:
        qs["optionsOverride.AcquireTokenOptions.ForceRefresh"] = "true"
    r = httpx.get(f"{SIDECAR}/AuthorizationHeaderUnauthenticated/graph",
                  params=qs, timeout=20)
    if r.status_code >= 400:
        # Surface sidecar's error verbatim so we can see the AADSTS message
        try:
            err_body = r.json()
        except Exception:
            err_body = {"_raw": r.text}
        return {
            "asAgent": as_agent,
            "agentIdUsed": effective_agent if as_agent else None,
            "error": True,
            "sidecarHttp": r.status_code,
            "sidecarBody": err_body,
            "correlationId": correlation_id,
        }
    bearer = r.json().get("authorizationHeader", "").replace("Bearer ", "")
    claims = decode_jwt(bearer)
    return {
        "asAgent": as_agent,
        "agentIdUsed": effective_agent if as_agent else None,
        "bearer": bearer,
        "bearer_short": bearer[:60] + "..." + bearer[-20:],
        "claims": claims,
        "appid_is_agent": claims.get("appid") == effective_agent,
        "appid_is_blueprint": claims.get("appid") == BLUEPRINT_APP_ID,
        "snapshot": False,
        "uti": claims.get("uti"),
        "appid": claims.get("appid"),
        "iat": claims.get("iat"),
        "app_displayname": claims.get("app_displayname"),
        "correlationId": correlation_id,
        "forceRefresh": force_refresh,
        "exp_human": fmt_exp(claims),
    }


@app.on_event("startup")
def warmup():
    # Wait for sidecar to be ready, but no longer pre-fetch tokens —
    # each click now gets a live token with a unique CorrelationId so the
    # user can find it in Entra Sign-in logs as "Request ID".
    for attempt in range(15):
        try:
            httpx.get(f"{SIDECAR}/healthz", timeout=3)
            break
        except Exception:
            time.sleep(2)
    SNAPSHOT["captured_at"] = int(time.time())


@app.get("/api/token")
def api_token(asAgent: bool = True, scenario: str = "valid", agentId: str | None = None):
    """scenario: 'valid' = use AGENT_APP_ID (parented by our Blueprint).
                 'foreign' = use FOREIGN_AGENT_APP_ID, OR the agentId query param if provided.
       Foreign call should fail at Entra with an AADSTS error proving Entra enforces
       Blueprint-Agent parentage server-side."""
    import uuid
    cid = str(uuid.uuid4())
    override = None
    if scenario == "foreign":
        candidate = (agentId or FOREIGN_AGENT_APP_ID or "").strip()
        if not candidate:
            return JSONResponse({"error": "Provide an Agent App ID, or set FOREIGN_AGENT_APP_ID env var."}, status_code=400)
        override = candidate
    try:
        return fetch_token(asAgent, correlation_id=cid, force_refresh=True, agent_id=override)
    except Exception as e:
        return JSONResponse({"error": str(e), "correlationId": cid}, status_code=502)


@app.get("/api/graph-users")
def api_graph_users(asAgent: bool = True, scenario: str = "valid", agentId: str | None = None):
    import uuid
    cid = str(uuid.uuid4())
    effective_agent = AGENT_APP_ID
    if scenario == "foreign":
        candidate = (agentId or FOREIGN_AGENT_APP_ID or "").strip()
        if not candidate:
            return JSONResponse({"error": "Provide an Agent App ID, or set FOREIGN_AGENT_APP_ID env var."}, status_code=400)
        effective_agent = candidate
    qs = {
        "optionsOverride.RelativePath": "users?$top=3&$select=displayName,userPrincipalName,id",
        "optionsOverride.HttpMethod": "Get",
        "optionsOverride.AcquireTokenOptions.CorrelationId": cid,
        "optionsOverride.AcquireTokenOptions.ForceRefresh": "true",
    }
    if asAgent:
        qs["AgentIdentity"] = effective_agent
    try:
        r = httpx.post(f"{SIDECAR}/DownstreamApiUnauthenticated/graph",
                       params=qs, timeout=25)
    except Exception as e:
        return JSONResponse({"error": str(e), "correlationId": cid}, status_code=502)
    # If sidecar itself returned an error (e.g., Entra rejected the AgentIdentity),
    # show its body verbatim so the AADSTS code is visible.
    if r.status_code >= 400:
        try:
            err_body = r.json()
        except Exception:
            err_body = {"_raw": r.text}
        return {
            "sidecarHttp": r.status_code,
            "graphHttp": None,
            "sidecarBody": err_body,
            "asAgent": asAgent,
            "scenario": scenario,
            "agentIdUsed": effective_agent if asAgent else None,
            "correlationId": cid,
            "error": True,
        }
    wrapper = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    inner_status = wrapper.get("statusCode")
    inner_body = wrapper.get("content", "")
    try:
        parsed = json.loads(inner_body) if isinstance(inner_body, str) else inner_body
    except Exception:
        parsed = {"_raw": inner_body}
    try:
        tok = fetch_token(asAgent, correlation_id=cid, force_refresh=False, agent_id=(effective_agent if asAgent else None))
        tclaims = (tok.get("claims") or {}) if not tok.get("error") else {}
    except Exception:
        tclaims = {}
    return {
        "sidecarHttp": r.status_code,
        "graphHttp": inner_status,
        "graphBody": parsed,
        "asAgent": asAgent,
        "scenario": scenario,
        "agentIdUsed": effective_agent if asAgent else None,
        "correlationId": cid,
        "tokenUti": tclaims.get("uti"),
        "tokenAppid": tclaims.get("appid"),
        "tokenIat": tclaims.get("iat"),
        "tokenAppDisplayName": tclaims.get("app_displayname"),
        "note": ("Both Blueprint and Agent have User.Read.All, so both calls succeed. "
                 "The discriminator is the appid claim in the token, not the HTTP status."),
    }


@app.get("/api/info")
def api_info():
    return {
        "tenantId": TENANT_ID,
        "blueprintAppId": BLUEPRINT_APP_ID,
        "agentAppId": AGENT_APP_ID,
        "foreignAgentAppId": FOREIGN_AGENT_APP_ID or None,
        "sidecar": SIDECAR,
        "snapshotCapturedAt": SNAPSHOT["captured_at"],
    }


PAGE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>Entra Agent Identity — Live Demo</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  body { background:#fafafa; }
  .step { background:#fff; border:1px solid #e5e7eb; border-radius:14px; padding:1.4rem 1.6rem; margin-bottom:1.2rem; }
  .step h3 { font-size:1.15rem; margin-bottom:.4rem; }
  .step .why  { color:#475569; font-size:.95rem; margin-bottom:.9rem; }
  pre  { background:#0f172a; color:#e2e8f0; padding:.9rem; border-radius:10px; max-height:24rem; overflow:auto; font-size:.82rem; }
  .ok    { color:#059669; font-weight:600; }
  .bad   { color:#b91c1c; font-weight:600; }
  .pill  { display:inline-block; padding:.2rem .55rem; background:#eef2ff; color:#3730a3; border-radius:999px; font-size:.78rem; font-family:monospace; }
  .arrow{ font-size:1.4rem; color:#94a3b8; }
  .flow { display:flex; align-items:center; gap:.6rem; flex-wrap:wrap; margin:.6rem 0 1.2rem; }
  .box  { padding:.45rem .8rem; background:#1e293b; color:#fff; border-radius:8px; font-size:.85rem; }
  .box.h{ background:#2563eb; }
  details summary { cursor:pointer; color:#2563eb; }
  table.claims td { padding:.2rem .6rem; vertical-align:top; }
  table.claims td:first-child { font-family:monospace; color:#475569; white-space:nowrap; }
  .snap-banner { background:#fef3c7; color:#78350f; padding:.45rem .7rem; border-radius:8px; font-size:.82rem; margin-bottom:.6rem; }
  .ids-box { background:#f0f9ff; border:1px solid #bae6fd; border-radius:10px; padding:.7rem .9rem; margin-top:.9rem; }
  .ids-title { font-weight:600; color:#075985; margin-bottom:.4rem; }
  .ids-table { width:100%; font-size:.85rem; }
  .ids-table td { padding:.3rem .5rem; vertical-align:top; }
  .ids-label { font-family:monospace; color:#475569; white-space:nowrap; }
  .ids-hint  { color:#475569; font-size:.8rem; }
  .copy-btn { font-size:.72rem; padding:.05rem .45rem; border:1px solid #cbd5e1; background:#fff; border-radius:6px; cursor:pointer; margin-left:.3rem; }
  .copy-btn:hover { background:#f1f5f9; }
</style>
</head>
<body>
<div class="container py-4" style="max-width:920px">

<h1 class="mb-1">🪪 Entra Agent Identity — live demo</h1>
<p class="text-muted">Click each step in order. The page calls real Entra + Microsoft Graph endpoints through the auth sidecar running next to this app.</p>

<div class="flow">
  <span class="box h">You (browser)</span><span class="arrow">→</span>
  <span class="box">agent container<br><small>this app</small></span><span class="arrow">→</span>
  <span class="box">sidecar :5000<br><small>auth SDK</small></span><span class="arrow">→</span>
  <span class="box">login.microsoftonline.com</span><span class="arrow">→</span>
  <span class="box">graph.microsoft.com</span>
</div>

<div id="info" class="step"><b>Identifiers in this demo</b><div id="info-body" class="mt-2 small"></div></div>

<div class="step">
  <h3>Step 1 — Get a Bearer token AS the Agent Identity</h3>
  <p class="why">The agent has <b>no credentials</b>. It asks the sidecar for a token. The sidecar uses the <b>Blueprint</b>'s client secret to authenticate to Entra, plus an <code>AgentIdentity</code> hint so Entra mints the token <b>as the Agent</b>.</p>
  <div class="small text-muted mb-2" style="background:#f0f9ff; border:1px solid #bae6fd; border-radius:8px; padding:.6rem .8rem">
    <b>Where does the Agent Identity actually live?</b><br>
    The Agent Identity is <b>not</b> in this container's code or environment — it is a real <b>app registration in your Entra tenant</b> (App ID <span class="pill" id="agent-pill"></span>), <b>parented by the Blueprint</b> <span class="pill" id="bp-pill"></span>. Entra stores the parent ↔ child relationship server-side as a <i>federated identity credential</i> on the Blueprint. The agent code only knows the Agent's App ID and passes it as the <code>AgentIdentity</code> query parameter; Entra does the actual lookup and parentage check before issuing the token.
  </div>
  <button class="btn btn-primary" onclick="getToken(true)">Get Agent Token</button>
  <div id="t1" class="mt-3"></div>
</div>

<div class="step">
  <h3>Step 2 — Decode &amp; validate the token</h3>
  <p class="why">A JWT is <code>header.payload.signature</code>. We decode the payload (no secret needed; it's not encrypted, just signed) and inspect the claims. The proof is in <span class="pill">appid</span>, <span class="pill">xms_par_app_azp</span>, and <span class="pill">roles</span>.</p>
  <button class="btn btn-primary" onclick="decodeLast()" id="btnDecode" disabled>Decode last token</button>
  <div id="t2" class="mt-3"></div>
</div>

<div class="step">
  <h3>Step 3 — Call Microsoft Graph as the Agent</h3>
  <p class="why">The sidecar attaches the token to <code>GET https://graph.microsoft.com/v1.0/users?$top=3</code>. <b>Microsoft Graph independently validates</b> the JWT (signature, issuer, audience, expiry, role) and returns real users from your tenant only if everything checks out.</p>
  <button class="btn btn-primary" onclick="callGraph(true)">Call Graph /users (as Agent)</button>
  <div id="t3" class="mt-3"></div>
</div>

<div class="step">
  <h3>Step 4 — Negative comparison: what if we skip the AgentIdentity?</h3>
  <p class="why">Same call without the <code>AgentIdentity</code> hint. The sidecar still authenticates with the Blueprint's secret, but now the resulting token is <b>for the Blueprint</b>. Look at how <span class="pill">appid</span> changes — that's the difference the feature makes.</p>
  <button class="btn btn-secondary" onclick="getToken(false)">Get Blueprint Token (no AgentIdentity)</button>
  <button class="btn btn-secondary" onclick="callGraph(false)">Call Graph as Blueprint</button>
  <div id="t4" class="mt-3"></div>
</div>

<div class="step">
  <h3>Step 5 — Test ANY Agent App ID against this sidecar</h3>
  <p class="why">This sidecar holds <b>our</b> Blueprint's secret (<code>a90a55dc-…</code>). Paste any Agent App ID below and we'll ask Entra to mint a token for it. Entra will <b>succeed</b> if the Agent is parented by our Blueprint via a federated identity credential, and <b>reject</b> with <code>AADSTS700213</code> otherwise.</p>
  <div id="foreign-info" class="small text-muted mb-2"></div>
  <div class="mb-2">
    <label class="small text-muted" for="foreignInput">Agent App ID to test (paste any GUID):</label>
    <div class="d-flex gap-2 mt-1" style="flex-wrap:wrap">
      <input id="foreignInput" type="text" class="form-control form-control-sm" style="font-family:monospace; max-width:340px"
             placeholder="00000000-0000-0000-0000-000000000000" />
      <button type="button" class="btn btn-outline-secondary btn-sm" onclick="resetForeign()">use configured default</button>
    </div>
  </div>
  <button class="btn btn-primary" onclick="getToken(true,'foreign')">Try to get token for entered Agent</button>
  <button class="btn btn-primary" onclick="callGraph(true,'foreign')">Call Graph as entered Agent</button>
  <div id="t5" class="mt-3"></div>
</div>

<details class="text-muted small mt-3"><summary>How the demo handles the sidecar's MSAL token cache</summary>
<p class="mt-2">The auth sidecar's MSAL token cache is keyed on <code>(client_id, scope, tenant)</code> &mdash; the <code>AgentIdentity</code> parameter is <em>not</em> part of the cache key. Without intervention, once the sidecar mints an Agent token for <code>graph/.default</code>, every later call returns the same cached token regardless of <code>AgentIdentity</code>.</p>
<p>This demo solves it by sending <code>optionsOverride.AcquireTokenOptions.ForceRefresh=true</code> on every click, so each button always triggers a fresh acquisition from Entra. Combined with a per-click <code>CorrelationId</code> GUID, every click produces a brand new Sign-in log entry that you can locate by Request ID.</p>
</details>

</div>

<script>
let lastToken = null;

async function getToken(asAgent, scenario){
  scenario = scenario || "valid";
  const target = scenario === "foreign" ? "t5" : (asAgent ? "t1" : "t4");
  setBusy(target, "Loading...");
  let url = "/api/token?asAgent=" + asAgent + "&scenario=" + scenario;
  if (scenario === "foreign"){
    const v = (document.getElementById("foreignInput")||{}).value;
    if (v && v.trim()) url += "&agentId=" + encodeURIComponent(v.trim());
  }
  const r = await fetch(url);
  const j = await r.json();
  if (scenario === "foreign"){
    const idTested = (document.getElementById("foreignInput")||{}).value || j.agentIdUsed || '';
    if (j.error || j.sidecarBody){
      const msg = (j.sidecarBody && (j.sidecarBody.error_description || j.sidecarBody.error || JSON.stringify(j.sidecarBody))) || j.error || "rejected";
      setHTML(target,
        `<div class="bad">✗ Entra REJECTED this Agent</div>
         <div class="small mt-2">Agent <span class="pill">${escapeHtml(idTested)}</span> is <b>not</b> parented by our Blueprint (<span class="pill">${BLUEPRINT_APP_ID_JS}</span>) — or it doesn't exist. Entra refused to mint a token.</div>
         <details class="mt-2" open><summary>Entra / sidecar error</summary><pre>${escapeHtml(typeof msg === 'string' ? msg : JSON.stringify(msg,null,2))}</pre></details>
         <details class="mt-2"><summary>full response</summary><pre>${escapeHtml(JSON.stringify(j,null,2))}</pre></details>`
      );
    } else {
      const appid = (j.claims && j.claims.appid) || '?';
      setHTML(target,
        `<div class="ok">✓ Entra MINTED a token for this Agent</div>
         <div class="small mt-2">Agent <span class="pill">${escapeHtml(idTested)}</span> <b>is</b> parented by our Blueprint (<span class="pill">${BLUEPRINT_APP_ID_JS}</span>). The token's <code>appid</code> claim = <span class="pill">${escapeHtml(appid)}</span>, expiring ${escapeHtml(j.exp_human||'')}.</div>
         <details class="mt-2"><summary>show raw bearer (truncated)</summary><pre>${escapeHtml(j.bearer_short||'')}</pre></details>
         <details class="mt-2"><summary>full response</summary><pre>${escapeHtml(JSON.stringify(j,null,2))}</pre></details>`
      );
    }
    return;
  }
  if (j.error){ setHTML(target, errBox(j.error)); return; }
  lastToken = j;
  document.getElementById("btnDecode").disabled = false;
  const ok = asAgent ? j.appid_is_agent : j.appid_is_blueprint;
  setHTML(target,
    `<div class="${ok?'ok':'bad'}">${ok?'✓':'✗'} sidecar returned a token ${j.exp_human}</div>
     <div class="small text-muted">appid in token: <span class="pill">${j.claims.appid||'?'}</span> ${ok?'(matches expected '+(asAgent?'Agent':'Blueprint')+')':''}</div>` +
    corrBlock(j.uti, j.appid, j.iat, j.app_displayname, j.correlationId) +
    `<details class="mt-2"><summary>show raw bearer (truncated)</summary><pre>${escapeHtml(j.bearer_short)}</pre></details>`
  );
}

function decodeLast(){
  if (!lastToken){ setHTML("t2", errBox("Run step 1 first")); return; }
  const c = lastToken.claims;
  const rows = [
    ["aud", c.aud, c.aud === "https://graph.microsoft.com" ? "ok" : "bad", "audience &mdash; the API this token is for"],
    ["iss", c.iss, (c.iss||"").includes("/" + (c.tid||"") + "/") ? "ok" : "bad", "issuer &mdash; Entra signed it"],
    ["tid", c.tid, "ok", "tenant id"],
    ["appid", c.appid, lastToken.appid_is_agent ? "ok" : (lastToken.appid_is_blueprint ? "bad" : "bad"),
        lastToken.appid_is_agent ? "Agent Identity ✅" : (lastToken.appid_is_blueprint ? "Blueprint (not the agent!)" : "?")],
    ["app_displayname", c.app_displayname, "ok", "human-readable app name"],
    ["xms_par_app_azp", c.xms_par_app_azp, c.xms_par_app_azp ? "ok" : "bad",
        c.xms_par_app_azp ? "parent app (Blueprint) &mdash; the credential holder" : "(missing &rarr; not minted via Agent ID flow)"],
    ["roles", (c.roles||[]).join(", "), (c.roles||[]).length?"ok":"bad", "permissions in this app-only token"],
    ["idtyp", c.idtyp, c.idtyp==="app"?"ok":"bad", "app-only token (no user)"],
    ["exp", c.exp, "ok", "expiry"],
  ];
  const html = `<table class="claims"><tbody>${
    rows.map(r =>
      `<tr><td>${r[0]}</td><td><span class="${r[2]}">${r[2]==="ok"?"✓":"✗"}</span> ${escapeHtml(String(r[1]||""))}</td><td class="text-muted small">${r[3]}</td></tr>`
    ).join("")
  }</tbody></table>
  <details class="mt-2"><summary>show full decoded JWT</summary><pre>${escapeHtml(JSON.stringify(c,null,2))}</pre></details>`;
  setHTML("t2", html);
}

async function callGraph(asAgent, scenario){
  scenario = scenario || "valid";
  const target = scenario === "foreign" ? "t5" : (asAgent ? "t3" : "t4");
  setBusy(target, "agent &rarr; sidecar &rarr; Graph...");
  let url = "/api/graph-users?asAgent=" + asAgent + "&scenario=" + scenario;
  if (scenario === "foreign"){
    const v = (document.getElementById("foreignInput")||{}).value;
    if (v && v.trim()) url += "&agentId=" + encodeURIComponent(v.trim());
  }
  const r = await fetch(url);
  const j = await r.json();
  if (scenario === "foreign"){
    const idTested = (document.getElementById("foreignInput")||{}).value || j.agentIdUsed || '';
    if (j.error || j.sidecarBody){
      const msg = (j.sidecarBody && (j.sidecarBody.error_description || j.sidecarBody.error || JSON.stringify(j.sidecarBody))) || j.error || "rejected";
      setHTML(target,
        `<div class="bad">✗ Entra REJECTED this Agent — Graph call never happened</div>
         <div class="small mt-2">Agent <span class="pill">${escapeHtml(idTested)}</span> is not parented by our Blueprint, so token acquisition failed before reaching Graph.</div>
         <details class="mt-2" open><summary>Entra / sidecar error</summary><pre>${escapeHtml(typeof msg === 'string' ? msg : JSON.stringify(msg,null,2))}</pre></details>
         <details class="mt-2"><summary>full response</summary><pre>${escapeHtml(JSON.stringify(j,null,2))}</pre></details>`
      );
    } else {
      const users = (j.value || []).slice(0, 5);
      setHTML(target,
        `<div class="ok">✓ Graph call SUCCEEDED as Agent <span class="pill">${escapeHtml(idTested)}</span></div>
         <div class="small mt-2">This Agent is parented by our Blueprint, Entra minted a token, and Graph returned ${ (j.value||[]).length } users.</div>
         <details class="mt-2"><summary>first ${users.length} users</summary><pre>${escapeHtml(JSON.stringify(users,null,2))}</pre></details>`
      );
    }
    return;
  }
  }
  if (j.error){ setHTML(target, errBox(j.error)); return; }
  const ok = j.graphHttp === 200;
  let listHtml = "";
  if (ok && j.graphBody && j.graphBody.value){
    listHtml = "<ul class='mt-2 mb-0'>" +
      j.graphBody.value.map(u =>
        `<li><b>${escapeHtml(u.displayName||"")}</b> &mdash; <span class="text-muted small">${escapeHtml(u.userPrincipalName||"")}</span> &mdash; <span class="pill">${escapeHtml(u.id||"")}</span></li>`
      ).join("") + "</ul>";
  } else if (j.graphBody && j.graphBody.error){
    listHtml = `<div class="bad mt-2">${escapeHtml(j.graphBody.error.code||"")} &mdash; ${escapeHtml(j.graphBody.error.message||"")}</div>`;
  }
  const idsHtml = corrBlock(j.tokenUti, j.tokenAppid, j.tokenIat, j.tokenAppDisplayName, j.correlationId);
  setHTML(target,
    `<div class="${ok?'ok':'bad'}">${ok?'✓':'✗'} Graph responded HTTP ${j.graphHttp} (sidecar HTTP ${j.sidecarHttp})</div>
     ${listHtml}
     ${idsHtml}
     <details class="mt-2"><summary>show raw response</summary><pre>${escapeHtml(JSON.stringify(j.graphBody,null,2))}</pre></details>`
  );
}

function copyText(t, btn){
  navigator.clipboard.writeText(t||"").then(()=>{ const o=btn.innerText; btn.innerText="✓"; setTimeout(()=>btn.innerText=o,1200); });
}

function corrBlock(uti, appid, iat, appname, requestId){
  const iatStr = iat ? new Date(iat*1000).toLocaleString() : "";
  const reqRow = requestId ? `
        <tr>
          <td class="ids-label">Request ID</td>
          <td><span class="pill">${escapeHtml(requestId)}</span> <button class="copy-btn" onclick="copyText('${escapeHtml(requestId)}',this)">copy</button></td>
          <td class="ids-hint"><b>This is the GUID Entra logged for this token request.</b> In Sign-in logs, it appears in the <i>Request ID</i> column. This ID was generated by the demo and passed to the sidecar as <code>CorrelationId</code>.</td>
        </tr>` : "";
  return `
    <div class="ids-box">
      <div class="ids-title">🔎 Cross-check this token request in Entra Sign-in logs</div>
      <table class="ids-table">
        ${reqRow}
        <tr>
          <td class="ids-label">uti</td>
          <td><span class="pill">${escapeHtml(uti||'?')}</span> <button class="copy-btn" onclick="copyText('${escapeHtml(uti||'')}',this)">copy</button></td>
          <td class="ids-hint">Entra's <i>Unique token identifier</i>, found in the row's details pane.</td>
        </tr>
        <tr>
          <td class="ids-label">appid</td>
          <td><span class="pill">${escapeHtml(appid||'?')}</span> <button class="copy-btn" onclick="copyText('${escapeHtml(appid||'')}',this)">copy</button></td>
          <td class="ids-hint">${escapeHtml(appname||'')} &mdash; filter the <i>Application</i> column.</td>
        </tr>
        <tr>
          <td class="ids-label">iat</td>
          <td><span class="pill">${escapeHtml(iatStr)}</span></td>
          <td class="ids-hint">Issue time &mdash; narrow the time window in Sign-in logs.</td>
        </tr>
      </table>
      <div class="ids-hint mt-2"><b>Where:</b> Entra admin center → <b>Identity → Monitoring &amp; health → Sign-in logs</b> → tab <b>Service principal sign-ins</b>. Filter by Application + time window. Click the row whose <i>Request ID</i> equals the GUID above.</div>
    </div>`;
}

function setBusy(id, msg){ setHTML(id, `<div class="text-muted"><span class="spinner-border spinner-border-sm"></span> ${msg}</div>`); }
function setHTML(id, html){ document.getElementById(id).innerHTML = html; }
function errBox(m){ return `<div class="bad">${escapeHtml(m)}</div>`; }
function escapeHtml(s){ return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

let CONFIGURED_FOREIGN_DEFAULT = "";
let BLUEPRINT_APP_ID_JS = "";
function resetForeign(){
  const el = document.getElementById("foreignInput");
  if (el) el.value = CONFIGURED_FOREIGN_DEFAULT;
}

(async () => {
  const r = await fetch("/api/info"); const j = await r.json();
  document.getElementById("info-body").innerHTML =
    `<div>Tenant: <span class="pill">${j.tenantId}</span></div>
     <div>Blueprint app: <span class="pill">${j.blueprintAppId}</span></div>
     <div>Agent Identity app: <span class="pill">${j.agentAppId}</span></div>
     <div>Foreign Agent Identity (different Blueprint): <span class="pill">${j.foreignAgentAppId || '(not configured)'}</span></div>
     <div>Sidecar URL (in-pod): <span class="pill">${j.sidecar}</span></div>
     <div class="text-muted mt-1">Snapshot captured at: ${j.snapshotCapturedAt ? new Date(j.snapshotCapturedAt*1000).toLocaleString() : 'pending'}</div>`;
  // Inline pills inside Step 1's "where does it live" callout
  const ap = document.getElementById("agent-pill"); if (ap) ap.textContent = j.agentAppId || '?';
  const bp = document.getElementById("bp-pill");    if (bp) bp.textContent = j.blueprintAppId || '?';
  const fi = document.getElementById("foreign-info");
  if (fi){
    const valid = j.agentAppId ? `<div>Try our valid Agent: <span class="pill" style="cursor:pointer" onclick="document.getElementById('foreignInput').value='${j.agentAppId}'">${j.agentAppId}</span> (click to use)</div>` : '';
    const foreign = j.foreignAgentAppId ? `<div>Try a foreign Agent (different Blueprint): <span class="pill" style="cursor:pointer" onclick="document.getElementById('foreignInput').value='${j.foreignAgentAppId}'">${j.foreignAgentAppId}</span> (click to use)</div>` : '';
    fi.innerHTML = valid + foreign + `<div class="text-muted mt-1">Or paste any GUID. We'll send <code>?AgentIdentity=&lt;your value&gt;</code> to the sidecar.</div>`;
  }
  CONFIGURED_FOREIGN_DEFAULT = j.foreignAgentAppId || "";
  BLUEPRINT_APP_ID_JS = j.blueprintAppId || "";
  const inp = document.getElementById("foreignInput");
  if (inp && !inp.value) inp.value = CONFIGURED_FOREIGN_DEFAULT;
})();
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/healthz")
def healthz():
    return {"ok": True}
