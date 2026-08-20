"""
DEMO ONLY — INTENTIONALLY VULNERABLE. DO NOT IMPORT, DO NOT DEPLOY.
=====================================================================

This module exists for one reason: to give GitHub code scanning something real
to find, so the Security tab, Copilot Autofix and security campaigns can be
demonstrated end to end.

Nothing here is called by app.py, processing.py, pvol_derotate.py or
ephem_cache.py. It is not part of the application.

WHY THE STRUCTURE LOOKS LIKE THIS
---------------------------------
The first version of this file was a flat list of vulnerable functions taking
plain parameters. CodeQL found exactly ONE of them — the MD5 hash — because
CodeQL is not pattern matching. Its dataflow queries need a recognized *source*
of untrusted input reaching a dangerous *sink*, and a bare function parameter is
not a source.

So every sink below is now wired to a real source: a command-line argument, an
environment variable, or stdin. Combined with `threat-models: local` in
.github/codeql/codeql-config.yml, that completes the source-to-sink path CodeQL
is looking for.

That difference — grep finds strings, CodeQL proves paths — is the single best
thing this file teaches.

If you are reading this outside of a demo: delete the file.
"""

import hashlib
import os
import pickle
import sqlite3
import subprocess
import sys
import urllib.request


# --- CWE-78: OS command injection -------------------------------------------
def convert_image(user_filename):
    """Shell out to a converter with unsanitized input."""
    os.system("convert " + user_filename + " /tmp/out.png")


def probe_file(user_filename):
    """Same flaw class, via subprocess with shell=True."""
    return subprocess.check_output("file " + user_filename, shell=True)


# --- CWE-22: path traversal --------------------------------------------------
def load_session(user_path):
    """Open a caller-controlled path with no normalization or containment."""
    with open("/var/data/sessions/" + user_path, "rb") as fh:
        return fh.read()


# --- CWE-89: SQL injection ---------------------------------------------------
def find_capture(conn, target_name):
    """String-built SQL instead of a parameterized query."""
    cur = conn.cursor()
    cur.execute("SELECT * FROM captures WHERE target = '" + target_name + "'")
    return cur.fetchall()


# --- CWE-94: code injection --------------------------------------------------
def apply_expression(expr):
    """Evaluate a caller-supplied expression."""
    return eval(expr)


# --- CWE-502: unsafe deserialization ----------------------------------------
def restore_profile(blob):
    """Unpickle untrusted bytes."""
    return pickle.loads(blob)


# --- CWE-327/328: weak hashing ----------------------------------------------
def fingerprint_credential(secret):
    """MD5 for a security-relevant value. This is the one that fired without a
    source, because it is a sensitive-data heuristic rather than a taint query."""
    return hashlib.md5(secret.encode()).hexdigest()


# --- CWE-918: server-side request forgery ------------------------------------
def fetch_ephemeris(user_supplied_url):
    """Request a caller-controlled URL with no allowlist."""
    with urllib.request.urlopen(user_supplied_url, timeout=10) as resp:
        return resp.read()


# --- CWE-798: hardcoded credentials -----------------------------------------
# Deliberately fake, and shaped so secret scanning has a pattern to consider.
API_TOKEN = "AKIAIOSFODNN7EXAMPLE"
DB_PASSWORD = "hunter2-not-a-real-password"


# =============================================================================
# The sources. Each line below hands untrusted data to one of the sinks above,
# which is what turns a suspicious-looking function into a provable data path.
# =============================================================================
def main():
    # Source: command-line arguments
    filename = sys.argv[1]
    convert_image(filename)
    probe_file(filename)
    load_session(sys.argv[2])

    # Source: environment variable
    target = os.environ["CAPTURE_TARGET"]
    conn = sqlite3.connect("/var/data/captures.db")
    find_capture(conn, target)

    # Source: stdin
    expression = sys.stdin.readline()
    apply_expression(expression)

    blob = sys.stdin.buffer.read()
    restore_profile(blob)

    # Source: command-line argument into an outbound request
    fetch_ephemeris(sys.argv[3])

    # No source needed - sensitive-data heuristic on the parameter name
    fingerprint_credential(os.environ["SESSION_SECRET"])


if __name__ == "__main__":
    main()
