# bridge-util

Standalone scripts for pulling data out of the Sage Bionetworks **Bridge**
research platform (`webservices.sagebridge.org` / `ws.sagebridge.org`).

Each script is self-contained and independently runnable — there's no shared
library, no build step, and no test suite. Two subdirectories, split by what
they authenticate against — see each one's own README for requirements,
script descriptions, and usage:

- **[`util/`](util/README.md)** — scripts that sign in to Bridge itself and
  call its REST API. Dependency-free; configuration comes from environment
  variables.
- **[`aws/`](aws/README.md)** — scripts that go around the Bridge API and hit
  AWS directly (e.g. a DynamoDB export), for cases the REST API can't serve
  efficiently. Meant to be run manually by account admins using their
  SSO-assumed AWS role.

See `CONTRIBUTING.md` for the conventions shared across all of these scripts
(useful if you're adding a new one).
