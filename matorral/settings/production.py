# flake8: noqa: F405
from .settings import *  # noqa F401

# Override DEBUG via the environment variable rather than editing this file.
DEBUG = False
ENVIRONMENT = env("ENVIRONMENT", default="production")

# Required when running behind a reverse proxy that terminates SSL.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# SECURE_SSL_REDIRECT doubles as "are we actually being served over HTTPS
# right now" — Secure-flagged cookies are silently dropped by the browser
# over plain HTTP, which breaks CSRF/login until a real cert is in place.
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "True") == "True"
SESSION_COOKIE_SECURE = SECURE_SSL_REDIRECT
CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT

# HSTS — uncomment once you're confident SSL is stable.
# Ramp SECURE_HSTS_SECONDS up gradually (60 → 3600 → 86400 → 31536000).
# Only enable subdomains/preload if every subdomain also runs HTTPS.
# SECURE_HSTS_SECONDS = 60
# SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# SECURE_HSTS_PRELOAD = True

# Links in emails must match the scheme the site is actually served over.
USE_HTTPS_IN_ABSOLUTE_URLS = SECURE_SSL_REDIRECT

# Override in the environment or uncomment below to lock down allowed hosts.
# ALLOWED_HOSTS = ["example.com"]

# Mailgun via django-anymail. Set MAILGUN_API_KEY and MAILGUN_SENDER_DOMAIN in the environment.
EMAIL_BACKEND = "anymail.backends.mailgun.EmailBackend"

ANYMAIL = {
    "MAILGUN_API_KEY": env("MAILGUN_API_KEY", default=None),
    "MAILGUN_SENDER_DOMAIN": env("MAILGUN_SENDER_DOMAIN", default=None),
}

# Inherited from base settings; set the ADMINS env var (comma-separated addresses)
# to receive unhandled-exception mail in production.
