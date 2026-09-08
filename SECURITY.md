# Security reporting and deployment boundary

This release candidate is intended for a local, loopback-bound instance. It does not provide public-user authentication or authorization for its explicit collection actions. Public hosting requires a separate deployment review.

Report a suspected vulnerability through the repository's private vulnerability reporting interface if GitHub presents it. If that interface is unavailable, open an issue asking the maintainer for a private reporting channel without including exploit details, credentials or personal data. No monitored security email address is asserted here.

Include the affected commit, the smallest safe reproduction, expected and observed behavior, and redacted diagnostics. Do not include API keys, database connection strings, raw reviewer records, or a working exploit against somebody else's instance. Disable collection if a source contract or credential issue is suspected; preserve the attempt ledger and verified backups for investigation.

The application validates configuration, allowlists upstream hosts, bounds response sizes and source attempts, escapes displayed text, and exposes stored reads separately from explicit collection. Those controls do not replace host access controls. Keep local configuration and backup directories private. A backup contains retained application history and operations records; verify a scratch restore before changing primary storage.
