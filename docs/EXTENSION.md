# ctx-agent as a Context Switcher extension

## Repository ownership

- context_switcher is the main project: it owns ctx, context identity,
  lifecycle, generic plugin command discovery, and the Extension API v1.
- local-ai-agent is an OPTIONAL extension: it owns ctx-ext-agent, local
  Ollama task processing, resource modes, security policy, and video media.
- Neither repository writes private internal implementation details into
  the other repository. Both can update independently as long as the stable
  CLI environment contract is maintained.

ctx discovers any installed ctx-ext-NAME executable and runs it with
CTX_EXTENSION_API_VERSION=1, CTX_CONTEXT (possibly empty) and
CTX_CONFIG_ROOT. This extension reads the context and chooses a default
agent project for project-specific commands, while still allowing
--project NAME overrides.

Context selection never implicitly enables or cancels background jobs.
The agent decides which actions need approval and remains sandboxed even
when called via ctx. Neither the core nor the extension gains privileges
through the interface.

## Entry point and metadata

- Entrypoint: ctx-ext-agent (Python package script)
- API contract: Context Switcher Extension API v1
- Metadata: ctx_agent/extension.json
- Security model: no host shell from AI, managed imported project copies,
  owner-only review/apply, and no unattended desktop or voice operations
- Autonomous tasks require separate Ollama and a running ctx-agent service
- Compatible with Nix-installed contexts via PATH discovery

## How to make another extension

Copy the *interface*, not the agent implementation. In any new repo,
provide a ctx-ext-feature executable in its package; optionally add a
standalone service, docs and tests. Your code stays in that repo.
The main Context Switcher never needs a feature-specific case clause.

Sample interface invocation:

    ctx feature --help

and environment:

    CTX_EXTENSION_API_VERSION=1
    CTX_CONTEXT=research
    CTX_CONFIG_ROOT=/home/user/.config/ctx

Do not assume CTX_CONTEXT is a security credential or mount browser
profiles merely because ctx ran a plugin.
