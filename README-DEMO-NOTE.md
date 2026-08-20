# ghas-demo

A GitHub Advanced Security demonstration repository.

The application source (`app.py`, `processing.py`, `pvol_derotate.py`,
`ephem_cache.py`, `main.py`) is real code from the Kepler planetary
image-processing project.

`demo_findings.py` is **intentionally vulnerable** and exists only so that code
scanning has findings to surface. It is not imported by the application and is
not deployed anywhere. Delete it if you fork this for any purpose other than a
demo.

Configured:

- **CodeQL code scanning** — `.github/workflows/codeql.yml`, `security-extended` suite
- **Dependabot** — `.github/dependabot.yml`, watching both `pip` and `github-actions`
