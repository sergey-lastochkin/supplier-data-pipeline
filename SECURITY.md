# Security

Synthetic data only; no secrets or production credentials are accepted.

- Never commit tokens, passwords, real bank data, personal data, or client exports.
- Fixtures are synthetic.
- External side effects are represented by local adapters or deterministic files.
- The API has no authentication and is only for local demo use; do not expose it publicly.
- Supplier HTML/JSON is untrusted input and schema validation fails closed.
