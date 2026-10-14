# Invitation-free onboarding

Screenshots captured from the deployed app at https://how-do-you-ai.vercel.app using a disposable Clerk development/test identity. The account was created with Clerk's test API, then its first authenticated visit verified automatic platform provisioning. The signup screenshot shows the real signup form; it does not depict a submitted personal signup.

| Step | Screenshot | Verified behavior |
| --- | --- | --- |
| Sign up | [01-sign-up.png](./01-sign-up.png) | No invitation required; Clerk signup form. |
| Choose agent | [02-choose-agent.png](./02-choose-agent.png) | Platform account automatically created with 2 analyses/day. |
| Copy setup message | [03-copy-setup-message.png](./03-copy-setup-message.png) | Real account-bound pairing message copied; private code stays collapsed. |
| Approve installation | [04-approve-installation.png](./04-approve-installation.png) | Web guidance waits for native agent installation/hook approval. This is not a fabricated native approval dialog. |
| Connected | [05-connected.png](./05-connected.png) | Actual collector paired and uploaded one synthetic session. |

The plugin was installed with native Codex CLI commands in a temporary isolated `CODEX_HOME`, downloaded from the live app, and checksum verified. No active user agent settings or real transcript files were used. No model calls were made. The temporary Clerk account and its Neon data are removed after verification.

Limits: 2 analyses/account/UTC day, 3 collector credentials, 5 browser sessions, 1 fresh active analysis/account, shared40/day platform cap. Repeated setup requests reuse the credential and do not reset quota or revive revoked access.
