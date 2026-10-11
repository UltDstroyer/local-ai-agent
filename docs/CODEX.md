# Codex and ChatGPT integration

## Authentication

ctx-agent uses the official Codex CLI for ChatGPT-backed engineering work.

Start the owner-initiated login flow with:

    ctx agent codex login

The alias below is equivalent:

    ctx agent chatgpt login

Check the stored Codex login with:

    ctx agent codex status

Logout and diagnostics are also exposed:

    ctx agent codex logout
    ctx agent codex doctor

The login flow is executed directly in the desktop user's session. The system
ctx-agent daemon never receives the ChatGPT or Codex credential.

## User-owned bridge

NixOS installs a user service named ctx-codex-bridge. It listens on a Unix
socket under:

    /run/ctx-codex/USERNAME.sock

The directory and socket are group-restricted to ctx-agent-users.

The bridge exposes only:

- health and authentication status
- read-only consultation
- workspace-scoped Codex work turns

There is no arbitrary shell RPC and no HTTP listener.

## Codex invocation policy

The bridge starts Codex with:

- JSON event output
- the selected managed project or workspace as its working root
- user Codex configuration disabled
- user Codex rules disabled
- explicit approval policy set to never
- read-only sandbox for planning and review
- workspace-write sandbox for approved implementation turns
- command network access disabled unless the approved mutation budget explicitly
  allows it

Codex thread IDs are persisted by ctx-agent so subsequent milestone guidance can
resume the same engineering conversation.

## Why credentials stay on the user side

The system ctx-agent service runs as the unprivileged ctx-agent account and has
no reason to possess the owner's ChatGPT token. Keeping authentication in the
desktop bridge means:

- a compromise of the project coordinator does not automatically reveal the
  user's normal Codex credential store
- owner login and logout remain normal Codex operations
- the worker can use the user's ChatGPT plan without placing a token in the
  NixOS system configuration
- token refresh remains Codex's responsibility

## Current trust note

The Codex process itself runs as the desktop user because that is where the
normal Codex login lives. ctx-agent therefore treats the managed workspace and
mutation contract as the authority boundary for project changes, but this is not
a claim that Codex is cryptographically unable to read every user-readable host
file.

For repositories containing adversarial prompt-injection content, use additional
OS isolation or a dedicated Codex account/session until the bridge gains a
stronger credential-separated execution sandbox.

The import layer already removes common secret-like files, .git, symlinks, and
private/cache directories before Codex sees a managed project.

## Plan usage

A project plan uses Codex twice before implementation:

1. read-only technical planning
2. later, milestone implementation after owner approval

The local administrator reconciles the initial technical proposal and narrows
the mutation budget before the owner sees it.

During execution, Codex is the primary worker. The local agent stores durable
context and sends corrective guidance when Codex is incomplete, stalled, or
drifting from the approved task.

## Usage limits

Codex usage follows the account and plan associated with the owner's Codex
login. Reaching a Codex usage limit may cause a worker turn to fail. ctx-agent
treats that as a stopped turn rather than silently switching to unrestricted
host execution.
