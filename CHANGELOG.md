# Changelog

Notable changes are recorded here. Entries under Unreleased describe local source changes and do not imply a tagged or published release.

## Unreleased

### Security

- Redact machine-specific paths, account identifiers, executable locations, runtime inventory, and host-specific listening-port details from tracked documentation.
- Restrict the Codex CLI child process to an explicit environment allowlist; forward an API key only when a dedicated DX_CODEX_API_KEY is configured.
- Add sentinel regression tests for database URLs, the DX root key, ambient OpenAI and other provider keys, and unrelated environment variables.

### Documentation

- Clarify prototype status, implemented and gated capabilities, local setup, test isolation, security-reporting limits, and contribution guidance.
- Remove a machine-specific desktop-launcher claim from the setup instructions.
- Add initial contribution and security policies.
- Add the Owner-selected MIT license for project-authored candidate files.
- Exclude the two reports formerly under archive/ pending confirmation of quotation and redistribution rights.
