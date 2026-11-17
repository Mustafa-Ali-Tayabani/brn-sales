"""Tests run against a throwaway database and never talk to an outside service: Apollo, Claude and the mail server are
replaced by stand-ins. Run them from the repo root with:  python -m unittest discover -s tests -t . -v"""
import os
import tempfile

_dir = tempfile.mkdtemp(prefix="brn-tests-")
os.environ.update(BRN_DB_PATH=os.path.join(_dir, "test.db"), BRN_SCHEDULER="0", BRN_DNS_CHECK="0", BRN_SITE_CHECK="0",
                  SMTP_HOST="smtp.test", SMTP_USER="sales@brn.test", SMTP_PASSWORD="x", APOLLO_API_KEY="test-key")
for _k in ("DATABASE_URL", "BRN_ADMIN_PASSWORD", "RESEND_API_KEY", "IMAP_HOST", "ANTHROPIC_API_KEY", "HUNTER_API_KEY", "VAPI_API_KEY", "GOOGLE_PLACES_API_KEY"):
    os.environ.pop(_k, None)
