# Database migrations

This directory is the canonical, product-facing migration history for
rag-favorite. New installations and future upgrade tooling must use only the
ordered migrations in `core/`.

The SQL and reports under `services/rag-app/migrations/` document migrations
from the original single-machine deployment. They are retained for audit and
data-recovery work, but they are not portable installation migrations and must
not be applied to a new rag-favorite database.

Rules for new migrations:

1. Never contain a user name, home directory, credential, or machine-specific
   path.
2. Be forward-only and safe to run inside a transaction unless explicitly
   documented otherwise.
3. Include an automated clean-install and upgrade-path test.
4. Treat collection keys and source roots as data, not schema constants.
