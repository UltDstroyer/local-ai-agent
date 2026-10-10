# ctx-agent as a Context Switcher extension

## Repository ownership

context_switcher owns ctx, context identity, lifecycle, generic extension
discovery, and Extension API v1.

local-ai-agent is an optional extension. It owns:

- local Ollama administration
- bounded local jobs
- Codex worker integration
- ChatGPT sign-in delegation
- project plans and mutation budgets
- owner approval and deviation workflows
- optional media commands

The repositories remain independently versioned behind the stable extension
interface.

## Discovery

Context Switcher discovers ctx-ext-agent from PATH and sets:

    CTX_EXTENSION_API_VERSION=1
    CTX_CONTEXT=research
    CTX_CONFIG_ROOT=/home/user/.config/ctx

CTX_CONTEXT chooses the default managed project. It is not a credential or a
privilege boundary.

Use --project NAME when no context is active or when targeting another managed
project.

## Commands

Local bounded jobs:

    ctx agent submit "Fix the tests"
    ctx agent status JOB_ID
    ctx agent diff JOB_ID
    ctx agent apply JOB_ID

Codex and ChatGPT login:

    ctx agent codex login
    ctx agent codex status
    ctx agent chatgpt login

Long project workflow:

    ctx agent plan "Implement the feature"
    ctx agent plan show
    ctx agent approve
    ctx agent mode maximum
    ctx agent deviations
    ctx agent deviation approve DEVIATION_ID
    ctx agent plan diff
    ctx agent plan apply

Media remains explicitly owner initiated:

    ctx agent media ...

## Authority rules

Switching a ctx context does not approve a plan, expand a mutation budget, or
silently apply changes.

Plan creation is read-only. Codex and the local administrator must agree on the
contract before it is shown to the owner. The owner must then explicitly
approve it.

Autonomous project execution occurs only in maximum resource mode.

A material scope deviation pauses the project. The model cannot convert its own
request into an approval.

Final apply is an explicit local owner action.

## Authentication

ChatGPT/Codex login runs directly in the interactive user's session through the
official Codex CLI. The extension does not send the login credential through
the ctx-agent system socket.

The NixOS module provides a user-owned bridge so the system coordinator can ask
Codex to consult or work without owning the user's token.

## Security model

The extension gains no privilege merely because Context Switcher invoked it.
See docs/SECURITY.md for the system/user service boundary, known Codex process
visibility limitation, managed workspaces, and mutation-contract enforcement.
