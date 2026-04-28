# Video Script — "How to Publish Agents using Entra SDK"

Total target length: **~6–8 minutes**. Speak conversationally; pause between sections.
Lines marked **[ON SCREEN]** are what should be visible. Lines marked **[CLICK]** are demo actions.

---

## 0. Cold open  *(15 sec)*

> "Hi. In this short demo I'm going to show you how an agent running in Azure can call Microsoft Graph **without ever holding a credential** — using the Microsoft Entra Agent Identity feature and the Entra SDK auth sidecar. By the end of this you'll see exactly which APIs get called, what the token looks like, and how to verify the whole thing in the Entra audit log."

**[ON SCREEN]** Title slide: *How to Publish Agents using Entra SDK* + your name.

---

## 1. The "why" — the big picture  *(45 sec)*

> "First, why does this matter?
>
> Today, when an agent or a service needs to call a Microsoft API like Graph, the standard pattern is: give that agent a client secret or a certificate, and let it authenticate to Entra directly. But in an agentic world, we end up with hundreds of agents — every one of them holding a credential. That's a lot of secrets to rotate, audit, and protect. And every secret is a potential leak.
>
> The Entra Agent Identity feature flips this around. We give the *credential* to one parent app — called a **Blueprint** — and we give the *identity* to a child app — called an **Agent Identity**. The Agent Identity has zero credentials. At runtime, the Blueprint authenticates on behalf of the Agent, and Entra mints a token whose `appid` is the Agent and whose permissions belong to the Agent. Net result: any resource protected by Entra — Graph, Azure ARM, Key Vault, your own custom APIs — can be called by the agent, and the audit trail clearly records *which* agent did it. All without putting a secret on the agent."

**[ON SCREEN]** Diagram: many agents → one shared Blueprint credential → Entra → tokens for each Agent Identity.

---

## 2. The setup — what we deployed  *(60 sec)*

> "Let me show you the runtime. I have **one Azure Container App** running, called `agent-id-demo-arcsmd`. Inside that single Container App there are **two containers** sharing one pod:
>
> The **first container** is the agent itself — a small FastAPI Python app that serves the demo web UI. This is the application code I wrote. It has no client secret, no certificate, no managed identity. The only things it knows are: the tenant ID, its own Agent App ID, and the URL of its sibling sidecar — all passed in as plain environment variables.
>
> The **second container** is the **auth sidecar**, pulled from `mcr.microsoft.com/entra-sdk/auth-sidecar`. This is prepackaged code — I didn't write a line of it. It's the only piece in the system that holds the Blueprint's client secret, mounted as a platform secret. It runs on `localhost:5000`, only reachable from inside the pod, and it speaks a tiny HTTP API.
>
> Why a sidecar? Because every other pattern leaks the credential into the agent's process or forces every agent developer to learn MSAL. With the sidecar, my agent is credential-free and stays credential-free. Same image works in any language."

**[ON SCREEN]** Architecture diagram: one ACA pod with `agent` and `sidecar` boxes, arrow to Entra and Graph.

> "And in Entra, there are two app registrations: a **Blueprint** that holds the secret, and an **Agent Identity** parented by the Blueprint. The Agent has `User.Read.All` on Graph; the Blueprint has nothing — that's deliberate. We'll see why in a moment."

---

## 3. Tour the live UI  *(30 sec)*

**[CLICK]** Open the demo URL in the browser.

> "Here's the live demo. At the top you can see the identifiers — tenant, Blueprint app, Agent Identity app, and the sidecar URL inside the pod. There are five steps. Let's walk through the first four — they're the valid Agent flow. Step 5 is a security boundary test that I'll cover briefly at the end."

**[ON SCREEN]** Browser tab on `https://agent-id-demo-arcsmd…/`.

---

## 4. Step 1 — Get the Agent token  *(120 sec)*

> "Step 1: Get a Bearer token *as* the Agent Identity. Watch closely — there are six hops, and the magic happens in the third one."

**[CLICK]** "Get Agent Token" button.

> "What just happened?
>
> **First**, my browser hit the agent's backend at `/api/token`. The browser doesn't pass any agent ID — the backend already knows who it is, because we set `AGENT_APP_ID` as an environment variable when we deployed the Container App.
>
> **Second**, the agent backend made a single HTTP call to the sidecar over `localhost:5000`. The URL was `GET /AuthorizationHeaderUnauthenticated/graph?AgentIdentity=<our Agent App ID>`. That `AgentIdentity` query parameter is the **lever** — it's the one thing that tells the sidecar 'mint a token for this child Agent, not for the parent Blueprint.'
>
> **Third**, inside the sidecar, MSAL kicked in. It looked up the named downstream API `graph` from its config, built a standard OAuth client-credentials request, and POSTed it to `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`. The body of that POST was: `client_id` = Blueprint, `client_secret` = the Blueprint's secret pulled from the platform secret, `scope` = `graph/.default`, plus an internal extension carrying our `AgentIdentity` hint.
>
> **Fourth**, Entra did three checks: it validated the Blueprint's secret, confirmed that our Agent is parented by that Blueprint via a federated identity credential, and confirmed that the Agent has the requested permissions. Then it minted a JWT — but here's the key — the JWT's `appid` claim is the **Agent**, not the Blueprint. Entra also wrote an audit row to Sign-in logs that says `Is Agent: Yes`, `Agent type: Agent Identity`, `Agent parent ID: <Blueprint>`.
>
> **Fifth**, the sidecar received the token, cached it in memory, wrapped it as `{ authorizationHeader: "Bearer eyJ..." }`, and returned it to the agent over localhost.
>
> **Sixth**, the agent backend stripped the `Bearer ` prefix, base64-decoded the JWT body so the UI can show the claims, and sent the JSON back to my browser. That's what you see on screen now."

**[ON SCREEN]** Highlight the green ✅, the truncated bearer, and the correlation panel.

> "Notice: the agent code never touched a client secret. Never. Everything sensitive happened inside the sidecar."

---

## 5. Step 2 — Decode the token  *(45 sec)*

**[CLICK]** "Decode last token".

> "Step 2 is purely client-side — no network call. The browser already has the decoded claims from Step 1, and it lays them out as a table.
>
> Look at three claims in particular:
> - **`appid`** — this is `beab1093...`, our Agent App ID. ✅
> - **`xms_par_app_azp`** — this is `a90a55dc...`, our Blueprint App ID. This claim only appears on Agent Identity tokens; it tells you *who actually authenticated* on the Agent's behalf. ✅
> - **`roles`** — this is `User.Read.All`, the Agent's permission, not the Blueprint's. ✅
>
> Without these three claims, you couldn't tell an Agent token apart from a regular app-only token. *With* them, you can prove the whole Agent Identity flow worked."

**[ON SCREEN]** Highlight `appid`, `xms_par_app_azp`, `roles` rows.

---

## 6. Step 3 — Call Microsoft Graph as the Agent  *(60 sec)*

**[CLICK]** "Call Graph /users (as Agent)".

> "Step 3 takes that same token and uses it for real work. Same pattern, but instead of calling `/AuthorizationHeader`, the agent calls `/DownstreamApi` on the sidecar. That endpoint says: 'acquire the token AND make the HTTP call to the named API for me.'
>
> The sidecar acquires the Agent token, then issues `GET https://graph.microsoft.com/v1.0/users?$top=3` with the JWT in the `Authorization` header.
>
> Microsoft Graph independently validates the JWT — it checks the signature against Entra's public keys, the issuer, the audience, the expiry, and the `roles`. **Graph does not trust the sidecar's word**; it trusts only Entra-signed tokens. If everything checks out, Graph returns three users from the directory."

**[ON SCREEN]** Three users displayed: Conf Room Adams, Adele Vance, MOD Administrator.

> "Three real users from the tenant. Proof that the token works end-to-end against a real downstream API."

---

## 7. Step 4 — Negative comparison  *(45 sec)*

**[CLICK]** "Get Blueprint Token (no AgentIdentity)".

> "Step 4 is a controlled experiment. I make the *exact same* call, with one tiny difference: I drop the `AgentIdentity` query parameter.
>
> Same sidecar. Same Blueprint secret. Same scope. Just no `AgentIdentity`."

**[ON SCREEN]** The new token's `appid` row.

> "Look — `appid` is now the **Blueprint**. Not the Agent. The whole token has flipped identity. This proves something subtle but important: the *only* thing distinguishing 'I'm the Blueprint' from 'I'm the Agent' is one query parameter. The credential is the same; the identity changes."

---

## 8. Where to verify in Entra Sign-in logs  *(30 sec)*

> "And finally, the audit trail. Every one of those token mints — Step 1, Step 4 — wrote a row to Entra's Sign-in logs under **Service principal sign-ins**."

**[ON SCREEN]** Switch to Entra admin center → Sign-in logs.

> "If I filter by Application = Demo Agent and open a row, I see the rich metadata: `Is Agent: Yes`, `Agent type: Agent Identity`, `Agent parent ID: <Blueprint>`, `Client credential type: ClientSecret`. This is the audit boundary that makes the feature operationally usable — you can prove which Agent did what, without losing track of which Blueprint authenticated on its behalf."

---

## 9. Wrap-up  *(20 sec)*

> "So to recap:
> - One Container App, two containers — agent and sidecar.
> - The agent never holds a credential. The sidecar holds the Blueprint's secret.
> - At runtime, the agent passes its own Agent App ID — read from an environment variable — to the sidecar. The sidecar uses the Blueprint to talk to Entra, but Entra mints a token *for* the Agent.
> - Microsoft Graph and any other Entra-protected API independently validates that token and treats the call as the Agent.
>
> The full source code is on GitHub at `github.com/vj926/entra-sdk-agent-id-demo`. Thanks for watching."

**[ON SCREEN]** GitHub URL + "End"

---

## Optional — Step 5 (security boundary, ~30 sec extra)

> "One bonus: I have a second Agent Identity that belongs to a *different* Blueprint. What happens if I ask my sidecar — which holds *my* Blueprint's secret — to mint a token for that foreign Agent?"

**[CLICK]** "Try to get token for FOREIGN Agent".

> "Entra rejects it: `AADSTS700213 — No matching federated identity record found`. Even though the sidecar authenticated successfully with my Blueprint's secret, Entra checks the parentage server-side. A Blueprint cannot mint tokens for Agents it doesn't own. *That's* the security model."

---

## Practical recording tips

- Pin the demo browser tab and the Entra portal tab side-by-side before recording.
- Have the JSON correlation panel pre-scrolled into view; copy buttons make great visual moments.
- Pre-clear the MSAL cache (deploy a fresh revision) right before recording so Step 1 actually hits Entra and shows up in Sign-in logs within a minute.
- For the Sign-in log demo, allow 5–10 minutes after a click — the portal indexes with delay. If recording in one take, do Steps 1–4 first, then loop back to the Entra portal for verification at the end.
- Keep your tenant ID and Blueprint App ID redacted on screen if this video will be public.
